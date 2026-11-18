"""AnomalyAutoFirer — divergence check on every new evidence capture.

Hooks into :func:`EvidenceBinder.put` (Q1-A from the 3.2 design). For
each capture:

1. Resolve the active crumb via :class:`CrumbTracker`. None → skip.
2. Look up the crumb's :class:`ExpectedEvidenceShape` via
   :class:`WeaponShapes`. None → skip silently (Q7-A).
3. Run :func:`compute_divergence`. Crash → catch + audit + skip
   (Q5-A); engagement uninterrupted.
4. Below threshold → write a per-engagement
   ``divergence_telemetry.jsonl`` line (Q4-B); no operator surface.
5. At-or-above threshold → call ``anomaly.emit`` channel (Q3-A) so
   the cap, audit log, and caller-restriction all flow uniformly.

Engagement-time only in v1 (Q8-A).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ara.runtime.anomaly.crumb_tracker import CrumbTracker
from ara.runtime.anomaly.divergence import compute_divergence
from ara.runtime.anomaly.record import divergence_to_dict
from ara.runtime.anomaly.weapon_shapes import WeaponShapes
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.binder.index import EvidenceRecord
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext


@dataclass
class AutoFirerStats:
    """Counters for diagnostics; not persisted, reset per-instance."""
    checks: int = 0
    skipped_no_crumb: int = 0
    skipped_no_shape: int = 0
    sub_threshold: int = 0
    fired: int = 0
    errors: int = 0


class AnomalyAutoFirer:
    def __init__(
        self,
        *,
        sessions_root: Path,
        engagement_id: str,
        crumb_tracker: CrumbTracker,
        weapon_shapes: WeaponShapes,
        ctx_factory: Callable[..., ChannelContext] | None = None,
    ) -> None:
        self.sessions_root = Path(sessions_root)
        self.engagement_id = engagement_id
        self.crumb_tracker = crumb_tracker
        self.weapon_shapes = weapon_shapes
        self.ctx_factory = ctx_factory
        self.stats = AutoFirerStats()
        self._lock = threading.Lock()

    @property
    def session_dir(self) -> Path:
        return self.sessions_root / self.engagement_id

    @property
    def telemetry_path(self) -> Path:
        return self.session_dir / "divergence_telemetry.jsonl"

    @property
    def audit_sink(self) -> Path:
        return self.session_dir / "events.jsonl"

    def on_evidence(self, rec: EvidenceRecord) -> None:
        """Called by EvidenceBinder.put after a successful capture."""
        with self._lock:
            self.stats.checks += 1

        crumb_id = self.crumb_tracker.current()
        if crumb_id is None:
            with self._lock:
                self.stats.skipped_no_crumb += 1
            return

        shape = self.weapon_shapes.get(crumb_id)
        if shape is None:
            with self._lock:
                self.stats.skipped_no_shape += 1
            return

        # Reload content for the divergence check.
        try:
            binder = EvidenceBinder(self.session_dir)
            content = binder.get(rec.id)
        except Exception as e:
            self._audit_error(crumb_id, rec.id, f"binder.get: {e}")
            return

        meta = rec.source_metadata or {}
        try:
            dv = compute_divergence(
                content,
                shape,
                content_type=rec.content_type,
                http_status=meta.get("status"),
                headers=meta.get("headers") or None,
            )
        except Exception as e:
            self._audit_error(crumb_id, rec.id, f"compute_divergence: {e}")
            return

        if dv.raw_score < shape.divergence_threshold:
            self._record_telemetry(crumb_id, rec, dv, shape.divergence_threshold)
            with self._lock:
                self.stats.sub_threshold += 1
            return

        # Above threshold — fire via anomaly.emit
        self._fire(crumb_id, rec, dv, shape)

    def _fire(
        self,
        crumb_id: str,
        rec: EvidenceRecord,
        dv,
        shape,
    ) -> None:
        try:
            ch = registry.get("anomaly.emit")
        except KeyError:
            self._audit_error(crumb_id, rec.id, "anomaly.emit not registered")
            return
        ctx = self._build_ctx(rec)
        try:
            ch.call({
                "evidence_id": rec.id,
                "crumb_id": crumb_id,
                "expected_shape_hash": shape.hash(),
                "divergence": divergence_to_dict(dv),
                "threshold_at_emission": shape.divergence_threshold,
            }, ctx)
            with self._lock:
                self.stats.fired += 1
        except Exception as e:
            self._audit_error(crumb_id, rec.id, f"anomaly.emit: {e}")

    def _build_ctx(self, rec: EvidenceRecord | None = None) -> ChannelContext:
        if self.ctx_factory is not None:
            return self.ctx_factory("system")
        # anomaly.emit requires an activation_id. Use the activation
        # under which the evidence was captured; fall back to a
        # synthetic "_autofire" marker so the field is non-empty.
        activation = (
            rec.captured_during_activation_id if rec is not None else None
        ) or "_autofire"
        return ChannelContext(
            engagement_id=self.engagement_id,
            activation_id=activation,
            caller="system",
            audit_sink=self.audit_sink,
        )

    def _record_telemetry(
        self, crumb_id: str, rec: EvidenceRecord, dv, threshold: float,
    ) -> None:
        line = {
            "ts": time.time(),
            "evidence_id": rec.id,
            "crumb_id": crumb_id,
            "raw_score": dv.raw_score,
            "threshold": threshold,
            "summary": {
                "missing_required": len(dv.missing_required),
                "pattern_mismatches": len(dv.pattern_mismatches),
                "unexpected_fields": len(dv.unexpected_fields),
                "structural_violations": len(dv.structural_violations),
                "line_matcher_failures": len(dv.line_matcher_failures),
                "section_matcher_failures": len(dv.section_matcher_failures),
            },
        }
        self._append_jsonl(self.telemetry_path, line)

    def _audit_error(self, crumb_id: str, evidence_id: str, reason: str) -> None:
        with self._lock:
            self.stats.errors += 1
        self._append_jsonl(self.audit_sink, {
            "ts": time.time(),
            "kind": "auto_firer.error",
            "body": {
                "crumb_id": crumb_id,
                "evidence_id": evidence_id,
                "reason": reason,
            },
        })

    def _append_jsonl(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, sort_keys=True, default=str)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)


# ---------- per-binder hook installation ----------

# Module-level: list of (binder_session_dir, callback) so the same
# binder instance fires all registered hooks on each put. We attach to
# the EvidenceBinder class itself; the callback inspects the binder's
# session_dir to decide whether the firing applies to it.

_HOOKS: dict[Path, list[Callable[[EvidenceRecord], None]]] = {}
_HOOKS_LOCK = threading.Lock()


def register_hook(
    session_dir: Path,
    callback: Callable[[EvidenceRecord], None],
) -> None:
    p = Path(session_dir).resolve()
    with _HOOKS_LOCK:
        _HOOKS.setdefault(p, []).append(callback)
    _ensure_binder_patched()


def hooks_for(session_dir: Path) -> list[Callable[[EvidenceRecord], None]]:
    p = Path(session_dir).resolve()
    with _HOOKS_LOCK:
        return list(_HOOKS.get(p, []))


def reset_hooks_for_tests() -> None:
    with _HOOKS_LOCK:
        _HOOKS.clear()


_PATCHED = False


def _ensure_binder_patched() -> None:
    """Monkey-patch :meth:`EvidenceBinder.put` once to invoke registered
    hooks for the binder's session_dir after each successful put."""
    global _PATCHED
    if _PATCHED:
        return
    original_put = EvidenceBinder.put

    def patched_put(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        rec = original_put(self, *args, **kwargs)
        for cb in hooks_for(self.session_dir):
            try:
                cb(rec)
            except Exception:
                # Hooks must not break the capture path. The auto-firer
                # itself is internally crash-safe; this is the second
                # line of defense for non-auto-firer hooks.
                pass
        return rec

    EvidenceBinder.put = patched_put  # type: ignore[method-assign]
    _PATCHED = True
