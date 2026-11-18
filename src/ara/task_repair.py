"""Mechanical repair for Armory task channel debt.

This module is intentionally narrow. It is only for the durable Hermes task
loop when the agent has consumed its action budget and then failed to emit a
valid structured ARA channel after bounded retries.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import debug_log
from . import task_session
from . import weapons as weapons_mod
from .runtime.bootstrap import install_engagement, install_engagement_channels
from .runtime.channels.base import ChannelContext, ChannelRejection


DEFAULT_MAX_REPAIRS = 3
DEFAULT_MAX_CHUNK_TRANSITION_REPAIRS = 2


@dataclass(frozen=True)
class RepairResult:
    ok: bool
    reason: str
    repaired: bool = False
    system_repair_count: int = 0
    system_chunk_transition_count: int = 0
    tag: str | None = None
    from_chunk: str | None = None
    to_chunk: str | None = None


def repair_commit_debt(
    state_root: Path,
    *,
    task_id: str,
    session_id: str,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
) -> RepairResult:
    state_root = Path(state_root)
    debug_log.emit(
        "task-repair", "start",
        state_root=state_root,
        task_id=task_id,
        session_id=session_id,
        max_repairs=max_repairs,
    )
    if not task_id:
        debug_log.emit(
            "task-repair", "reject",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            reason="TASK_RUN_ID_REQUIRED",
        )
        return RepairResult(False, "TASK_RUN_ID_REQUIRED")
    if not session_id:
        debug_log.emit(
            "task-repair", "reject",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            reason="ARA_SESSION_ID_REQUIRED",
        )
        return RepairResult(False, "ARA_SESSION_ID_REQUIRED")
    run = task_session.TaskRunSession.load(state_root, task_id)
    if run.phase != task_session.TaskPhase.COMMIT_REQUIRED:
        debug_log.emit(
            "task-repair", "noop",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            phase=run.phase.value,
            system_repair_count=run.system_repair_count,
        )
        return RepairResult(
            True,
            f"NO_REPAIR_NEEDED:{run.phase.value}",
            system_repair_count=run.system_repair_count,
        )
    max_repairs = max(0, int(max_repairs))
    if run.system_repair_count >= max_repairs:
        debug_log.emit(
            "task-repair", "reject",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            reason="REPAIR_LIMIT_EXCEEDED",
            system_repair_count=run.system_repair_count,
        )
        return RepairResult(
            False,
            "REPAIR_LIMIT_EXCEEDED",
            system_repair_count=run.system_repair_count,
        )

    session_dir = state_root / "sessions" / session_id
    if not session_dir.is_dir():
        debug_log.emit(
            "task-repair", "reject",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            reason="SESSION_DIR_MISSING",
            session_dir=str(session_dir),
        )
        return RepairResult(False, "SESSION_DIR_MISSING")

    weapon_dir = None
    if run.weapon_id:
        candidate = weapons_mod.weapon_dir(state_root, run.weapon_id)
        if candidate.is_dir():
            weapon_dir = candidate
    install_engagement_channels(
        sessions_root=state_root / "sessions",
        weapon_dir=weapon_dir,
        engagement_id=session_id,
    )
    from .runtime.channels import registry as _registry

    ctx = ChannelContext(
        engagement_id=session_id,
        activation_id="_task_system_repair",
        caller="system",
        audit_sink=session_dir / "events.jsonl",
    )
    evidence_refs = _latest_session_evidence_refs(session_dir, limit=3)
    body = {
        "title": "system_repair: commit debt cleared",
        "reasoning": (
            "phase=commit_required; Hermes exhausted commit retries without "
            "filing a valid structured channel; repair origin=system_repair"
        ),
        "origin": "system_repair",
        "evidence_refs": evidence_refs,
        "not_mechanically_testable": not bool(evidence_refs),
        "affected_assets": [run.trim_id] if run.trim_id else [],
        "notes": json.dumps({
            "task_id": task_id,
            "armor_id": run.armor_id,
            "weapon_id": run.weapon_id,
            "trim_id": run.trim_id,
            "temper_id": run.temper_id,
            "system_repair_count_before": run.system_repair_count,
        }, sort_keys=True),
    }
    try:
        _registry.get("lead.file").call(body, ctx)
    except ChannelRejection as exc:
        debug_log.emit(
            "task-repair", "channel_rejected",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            body=body,
            error=str(exc),
        )
        return RepairResult(False, f"CHANNEL_REJECTED:{exc}")
    except Exception as exc:  # noqa: BLE001
        debug_log.emit_exception(
            "task-repair", "channel_error",
            exc,
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            body=body,
        )
        return RepairResult(False, f"CHANNEL_ERROR:{type(exc).__name__}:{exc}")

    run = task_session.TaskRunSession.load(state_root, task_id)
    run.record_system_repair("lead.file")
    debug_log.emit(
        "task-repair", "repaired",
        state_root=state_root,
        task_id=task_id,
        session_id=session_id,
        body=body,
        system_repair_count=run.system_repair_count,
    )
    return RepairResult(
        True,
        "REPAIRED",
        repaired=True,
        system_repair_count=run.system_repair_count,
        system_chunk_transition_count=run.system_chunk_transition_count,
    )


def repair_chunk_transition_debt(
    state_root: Path,
    *,
    task_id: str,
    session_id: str,
    max_repairs: int = DEFAULT_MAX_CHUNK_TRANSITION_REPAIRS,
) -> RepairResult:
    state_root = Path(state_root)
    debug_log.emit(
        "task-chunk-transition-repair", "start",
        state_root=state_root,
        task_id=task_id,
        session_id=session_id,
        max_repairs=max_repairs,
    )
    if not task_id:
        return RepairResult(False, "TASK_RUN_ID_REQUIRED")
    if not session_id:
        return RepairResult(False, "ARA_SESSION_ID_REQUIRED")
    run = task_session.TaskRunSession.load(state_root, task_id)
    if run.phase != task_session.TaskPhase.BRANCH_REQUIRED or not run.chunk_gated:
        debug_log.emit(
            "task-chunk-transition-repair", "noop",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            phase=run.phase.value,
            chunk_gated=run.chunk_gated,
            system_chunk_transition_count=run.system_chunk_transition_count,
        )
        return RepairResult(
            True,
            f"NO_REPAIR_NEEDED:{run.phase.value}:chunk_gated={int(run.chunk_gated)}",
            system_repair_count=run.system_repair_count,
            system_chunk_transition_count=run.system_chunk_transition_count,
        )
    max_repairs = max(0, int(max_repairs))
    if run.system_chunk_transition_count >= max_repairs:
        debug_log.emit(
            "task-chunk-transition-repair", "reject",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            reason="CHUNK_TRANSITION_REPAIR_LIMIT_EXCEEDED",
            system_chunk_transition_count=run.system_chunk_transition_count,
        )
        return RepairResult(
            False,
            "CHUNK_TRANSITION_REPAIR_LIMIT_EXCEEDED",
            system_repair_count=run.system_repair_count,
            system_chunk_transition_count=run.system_chunk_transition_count,
        )

    session_dir = state_root / "sessions" / session_id
    if not session_dir.is_dir():
        return RepairResult(False, "SESSION_DIR_MISSING")
    if not run.weapon_id:
        return RepairResult(False, "WEAPON_ID_REQUIRED")
    weapon_dir = weapons_mod.weapon_dir(state_root, run.weapon_id)
    if not weapon_dir.is_dir():
        return RepairResult(False, "WEAPON_DIR_MISSING")

    current_chunk = _current_chunk_from_history(session_dir)
    if not current_chunk:
        return RepairResult(False, "CURRENT_CHUNK_MISSING")
    selected = _select_auto_chunk_exit(weapon_dir, current_chunk)
    if selected is None:
        debug_log.emit(
            "task-chunk-transition-repair", "reject",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            reason="NO_AUTO_EXIT_AVAILABLE",
            current_chunk=current_chunk,
        )
        return RepairResult(
            False,
            "NO_AUTO_EXIT_AVAILABLE",
            system_repair_count=run.system_repair_count,
            system_chunk_transition_count=run.system_chunk_transition_count,
            from_chunk=current_chunk,
        )
    tag, target_chunk = selected

    wiring = install_engagement(
        sessions_root=state_root / "sessions",
        engagement_id=session_id,
        weapon_dir=weapon_dir,
        install_anomaly_autofirer=False,
        auto_initialize_chunks=False,
    )
    chunk_runtime = wiring.chunk_runtime
    if chunk_runtime is None:
        return RepairResult(False, "CHUNK_RUNTIME_MISSING")
    mounted = chunk_runtime.mount_existing_chunk(current_chunk)
    if not mounted.ok:
        return RepairResult(False, f"MOUNT_FAILED:{mounted.error_kind}:{mounted.error}")

    from .runtime.channels import registry as _registry

    ctx = ChannelContext(
        engagement_id=session_id,
        activation_id="_task_system_chunk_transition",
        caller="agent",
        audit_sink=session_dir / "events.jsonl",
    )
    body = {"tag": tag}
    try:
        result = _registry.get("chunks.emit_tag").call(body, ctx)
    except ChannelRejection as exc:
        debug_log.emit(
            "task-chunk-transition-repair", "channel_rejected",
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            body=body,
            error=str(exc),
        )
        return RepairResult(False, f"CHANNEL_REJECTED:{exc}")
    except Exception as exc:  # noqa: BLE001
        debug_log.emit_exception(
            "task-chunk-transition-repair", "channel_error",
            exc,
            state_root=state_root,
            task_id=task_id,
            session_id=session_id,
            body=body,
        )
        return RepairResult(False, f"CHANNEL_ERROR:{type(exc).__name__}:{exc}")

    from_chunk = str(result.get("from_chunk") or current_chunk)
    to_chunk = str(result.get("to_chunk") or target_chunk)
    run = task_session.TaskRunSession.load(state_root, task_id)
    run.record_system_chunk_transition(
        tag=tag,
        from_chunk=from_chunk,
        to_chunk=to_chunk,
    )
    debug_log.emit(
        "task-chunk-transition-repair", "repaired",
        state_root=state_root,
        task_id=task_id,
        session_id=session_id,
        tag=tag,
        from_chunk=from_chunk,
        to_chunk=to_chunk,
        system_chunk_transition_count=run.system_chunk_transition_count,
    )
    return RepairResult(
        True,
        "CHUNK_TRANSITION_REPAIRED",
        repaired=True,
        system_repair_count=run.system_repair_count,
        system_chunk_transition_count=run.system_chunk_transition_count,
        tag=tag,
        from_chunk=from_chunk,
        to_chunk=to_chunk,
    )


def repair_from_env() -> int:
    state_root = Path(os.environ.get("ARA_STATE_ROOT") or ".ara")
    task_id = os.environ.get("ARA_TASK_RUN_ID") or ""
    session_id = os.environ.get("ARA_SESSION_ID") or ""
    try:
        max_repairs = int(os.environ.get("ARA_TASK_MAX_REPAIRS") or DEFAULT_MAX_REPAIRS)
    except ValueError:
        max_repairs = DEFAULT_MAX_REPAIRS
    try:
        result = repair_commit_debt(
            state_root,
            task_id=task_id,
            session_id=session_id,
            max_repairs=max_repairs,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ara-task-repair] fatal: {type(exc).__name__}: {exc}")
        return 1
    print(
        f"[ara-task-repair] {result.reason} "
        f"repaired={int(result.repaired)} "
        f"system_repair_count={result.system_repair_count}"
    )
    if result.ok:
        return 0
    if result.reason == "REPAIR_LIMIT_EXCEEDED":
        return 3
    return 1


def repair_chunk_transition_from_env() -> int:
    state_root = Path(os.environ.get("ARA_STATE_ROOT") or ".ara")
    task_id = os.environ.get("ARA_TASK_RUN_ID") or ""
    session_id = os.environ.get("ARA_SESSION_ID") or ""
    try:
        max_repairs = int(
            os.environ.get("ARA_TASK_MAX_CHUNK_TRANSITION_REPAIRS")
            or DEFAULT_MAX_CHUNK_TRANSITION_REPAIRS
        )
    except ValueError:
        max_repairs = DEFAULT_MAX_CHUNK_TRANSITION_REPAIRS
    try:
        result = repair_chunk_transition_debt(
            state_root,
            task_id=task_id,
            session_id=session_id,
            max_repairs=max_repairs,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ara-task-chunk-transition-repair] fatal: {type(exc).__name__}: {exc}")
        return 1
    print(
        f"[ara-task-chunk-transition-repair] {result.reason} "
        f"repaired={int(result.repaired)} "
        f"system_chunk_transition_count={result.system_chunk_transition_count} "
        f"tag={result.tag or ''} from={result.from_chunk or ''} "
        f"to={result.to_chunk or ''}"
    )
    if result.ok:
        return 0
    if result.reason in {
        "CHUNK_TRANSITION_REPAIR_LIMIT_EXCEEDED",
        "NO_AUTO_EXIT_AVAILABLE",
    }:
        return 3
    return 1


def force_chunk_advance_from_env() -> int:
    """Time-forcer: mechanically advance the active weapon chunk one hop toward
    a finding-capable chunk, independent of ``BRANCH_REQUIRED`` / ``chunk_gated``.

    Used by the durable task loop's time-based progression forcers (50% out of
    the entry chunk, 85% drive toward writeup) so a slow agent that will not
    advance chunks voluntarily still reaches the chunk where ``finding.file``
    unlocks before the trim window closes. ``_select_auto_chunk_exit`` always
    routes toward the nearest finding-capable chunk; this no-ops once the
    current chunk is itself finding-capable.
    """
    state_root = Path(os.environ.get("ARA_STATE_ROOT") or ".ara")
    task_id = os.environ.get("ARA_TASK_RUN_ID") or ""
    session_id = os.environ.get("ARA_SESSION_ID") or ""
    if not task_id or not session_id:
        print("[ara-task-time-forcer] MISSING_IDS")
        return 1
    try:
        run = task_session.TaskRunSession.load(state_root, task_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[ara-task-time-forcer] LOAD_FAILED:{type(exc).__name__}:{exc}")
        return 1
    if not run.weapon_id:
        print("[ara-task-time-forcer] WEAPON_ID_REQUIRED")
        return 1
    weapon_dir = weapons_mod.weapon_dir(state_root, run.weapon_id)
    if not weapon_dir.is_dir():
        print("[ara-task-time-forcer] WEAPON_DIR_MISSING")
        return 1
    session_dir = state_root / "sessions" / session_id
    current_chunk = _current_chunk_from_history(session_dir)
    if not current_chunk:
        print("[ara-task-time-forcer] CURRENT_CHUNK_MISSING")
        return 1
    chunks = _load_chunk_graph(weapon_dir)
    if _is_finding_capable_chunk(chunks.get(current_chunk) or {}):
        print(f"[ara-task-time-forcer] ALREADY_FINDING_CAPABLE:{current_chunk}")
        return 0
    selected = _select_auto_chunk_exit(weapon_dir, current_chunk)
    if selected is None:
        print(f"[ara-task-time-forcer] NO_FORWARD_EXIT:{current_chunk}")
        return 1
    tag, target_chunk = selected
    wiring = install_engagement(
        sessions_root=state_root / "sessions",
        engagement_id=session_id,
        weapon_dir=weapon_dir,
        install_anomaly_autofirer=False,
        auto_initialize_chunks=False,
    )
    chunk_runtime = wiring.chunk_runtime
    if chunk_runtime is None:
        print("[ara-task-time-forcer] CHUNK_RUNTIME_MISSING")
        return 1
    mounted = chunk_runtime.mount_existing_chunk(current_chunk)
    if not mounted.ok:
        print(f"[ara-task-time-forcer] MOUNT_FAILED:{mounted.error_kind}:{mounted.error}")
        return 1
    from .runtime.channels import registry as _registry

    ctx = ChannelContext(
        engagement_id=session_id,
        activation_id="_task_time_forcer",
        caller="agent",
        audit_sink=session_dir / "events.jsonl",
    )
    try:
        result = _registry.get("chunks.emit_tag").call({"tag": tag}, ctx)
    except ChannelRejection as exc:
        print(f"[ara-task-time-forcer] CHANNEL_REJECTED:{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[ara-task-time-forcer] CHANNEL_ERROR:{type(exc).__name__}:{exc}")
        return 1
    from_chunk = str(result.get("from_chunk") or current_chunk)
    to_chunk = str(result.get("to_chunk") or target_chunk)
    run = task_session.TaskRunSession.load(state_root, task_id)
    run.record_system_chunk_transition(
        tag=tag,
        from_chunk=from_chunk,
        to_chunk=to_chunk,
    )
    print(
        f"[ara-task-time-forcer] FORCED tag={tag} from={from_chunk} to={to_chunk} "
        f"system_chunk_transition_count={run.system_chunk_transition_count}"
    )
    return 0


def verify_findings_from_env() -> int:
    """Verification checkpoint — the VERIFY_REQUIRED resolver.

    Called by the durable loop when a task trips ``VERIFY_REQUIRED`` (after
    filing ``verify_budget`` findings). Re-runs the *exact same*
    CLAIM_VERIFICATION rule the commit-time gate enforces against each
    accumulated draft finding's persisted markers and still-retrievable
    evidence. A finding that can no longer reproduce its own claim — evidence
    gone/unreadable, a required marker absent, or a disqualifying marker present
    (e.g. a login form on a page claimed unauthenticated) — is mechanically
    downgraded (``auto_dismissed``). Then clears the task's verification debt via
    ``record_verify_pass`` so work can resume. Mechanical, no agent turn — this
    is the periodic re-verify pass that makes filed findings reproducible, not
    merely filed.
    """
    from dataclasses import replace as _replace
    state_root = Path(os.environ.get("ARA_STATE_ROOT") or ".ara")
    task_id = os.environ.get("ARA_TASK_RUN_ID") or ""
    session_id = os.environ.get("ARA_SESSION_ID") or ""
    if not task_id or not session_id:
        print("[ara-task-verify] MISSING_IDS")
        return 1
    session_dir = Path(state_root) / "sessions" / session_id
    try:
        from ara.runtime.findings.store import FindingStore
        from ara.runtime.binder.api import EvidenceBinder
    except Exception as exc:  # noqa: BLE001
        print(f"[ara-task-verify] IMPORT_FAILED:{type(exc).__name__}:{exc}")
        return 1
    downgraded = 0
    kept = 0
    try:
        store = FindingStore(session_dir)
        binder = EvidenceBinder(session_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[ara-task-verify] STORE_OPEN_FAILED:{type(exc).__name__}:{exc}")
        binder = None
        store = None
    if store is not None:
        # Re-run the SAME rule the commit-time gate enforces, now against the
        # persisted markers and still-retrievable evidence. A finding is
        # downgraded only if it can no longer reproduce its own claim (evidence
        # gone/unreadable, required marker absent, disqualifying marker present).
        try:
            from ara.runtime.findings.validator import (
                _rule_claim_verification as _claim_rule,
            )
        except Exception:  # noqa: BLE001
            _claim_rule = None

        class _VerifyCtx:
            # the only ctx attribute _rule_claim_verification reads
            def __init__(self, binder):
                self.binder = binder

        vctx = _VerifyCtx(binder)
        for f in store.by_status("draft", "operator_review"):
            body = {
                "verification": getattr(f, "verification", None),
                "not_mechanically_testable": bool(
                    getattr(f, "not_mechanically_testable", False)),
                "evidence_refs": list(getattr(f, "evidence_refs", None) or []),
            }
            rejection = None
            if _claim_rule is not None:
                try:
                    rejection = _claim_rule(body, vctx)
                except Exception:  # noqa: BLE001
                    # Never destroy a finding because the checkpoint itself
                    # failed — fail-open on infra errors, fail-closed only on a
                    # genuine rule rejection.
                    rejection = None
            if rejection is None:
                kept += 1
            else:
                # Don't hide it — flag it with a disposition and keep it visible
                # in the operator's "Not Confirmed" section for manual review.
                rule = str(getattr(rejection, "rule", "") or "")
                if "REFUTED" in rule:
                    disp = "false_positive"
                elif "UNREADABLE" in rule or "NO_EVIDENCE" in rule:
                    disp = "non_reproducible"
                else:
                    disp = "unsure"
                try:
                    store.append(_replace(
                        f, disposition=disp,
                        disposition_reason=str(getattr(rejection, "message", "")
                                               or rule)[:300],
                        disposition_by="mechanical",
                        disposition_at=time.time()))
                    downgraded += 1
                    print(f"[ara-task-verify] FLAG {f.id} -> {disp} ({rule})")
                except Exception:  # noqa: BLE001
                    kept += 1
        if binder is not None:
            try:
                binder.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        run = task_session.TaskRunSession.load(state_root, task_id)
        run.record_verify_pass(downgraded=downgraded, kept=kept)
    except Exception as exc:  # noqa: BLE001
        print(f"[ara-task-verify] RECORD_FAILED:{type(exc).__name__}:{exc}")
        return 1
    print(f"[ara-task-verify] VERIFIED kept={kept} downgraded={downgraded}")
    return 0


def _latest_session_evidence_refs(session_dir: Path, *, limit: int) -> list[str]:
    db_path = Path(session_dir) / "evidence" / "index.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id FROM evidence ORDER BY captured_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        str(row[0])
        for row in rows
        if row and isinstance(row[0], str) and row[0]
    ]


def _current_chunk_from_history(session_dir: Path) -> str | None:
    path = Path(session_dir) / "chunk_history.jsonl"
    if not path.exists():
        return None
    current = None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        kind = row.get("kind")
        if kind in {"initialize", "swap", "jump", "rewind"} and row.get("to_chunk"):
            current = str(row.get("to_chunk"))
        elif kind == "kill":
            current = None
    return current


def _select_auto_chunk_exit(weapon_dir: Path, current_chunk: str) -> tuple[str, str] | None:
    chunks = _load_chunk_graph(Path(weapon_dir))
    current = chunks.get(current_chunk)
    if not current:
        return None
    finding_chunks = {
        chunk_id
        for chunk_id, data in chunks.items()
        if _is_finding_capable_chunk(data)
    }
    if not finding_chunks:
        return None
    exits = current.get("exits") or {}
    if not isinstance(exits, dict):
        return None
    candidates: list[tuple[int, int, str, str]] = []
    for order, (tag, target) in enumerate(exits.items()):
        if not isinstance(tag, str) or not isinstance(target, dict):
            continue
        next_chunk = target.get("next_chunk_id")
        if not isinstance(next_chunk, str) or not next_chunk:
            continue
        if next_chunk == current_chunk and current_chunk not in finding_chunks:
            continue
        if next_chunk == "operator_review":
            continue
        if target.get("requires_caller") == "operator":
            continue
        distance = _distance_to_finding_chunk(chunks, next_chunk, finding_chunks)
        if distance is None:
            continue
        candidates.append((distance, order, tag, next_chunk))
    if not candidates:
        return None
    _, _, tag, next_chunk = min(candidates, key=lambda item: (item[0], item[1]))
    return tag, next_chunk


def _load_chunk_graph(weapon_dir: Path) -> dict[str, dict]:
    chunks_dir = Path(weapon_dir) / "chunks"
    if not chunks_dir.exists():
        return {}
    chunks: dict[str, dict] = {}
    for path in sorted(chunks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        chunk_id = data.get("id")
        if isinstance(chunk_id, str) and chunk_id:
            chunks[chunk_id] = data
    return chunks


def _is_finding_capable_chunk(data: dict) -> bool:
    if data.get("can_emit_finding") is True:
        return True
    tool_surface = data.get("tool_surface") or []
    return isinstance(tool_surface, list) and "finding.file" in tool_surface


def _distance_to_finding_chunk(
    chunks: dict[str, dict],
    start_chunk: str,
    finding_chunks: set[str],
) -> int | None:
    if start_chunk not in chunks:
        return None
    queue: deque[tuple[str, int]] = deque([(start_chunk, 0)])
    seen: set[str] = set()
    while queue:
        chunk_id, distance = queue.popleft()
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        if chunk_id in finding_chunks:
            return distance
        data = chunks.get(chunk_id) or {}
        exits = data.get("exits") or {}
        if not isinstance(exits, dict):
            continue
        for target in exits.values():
            if not isinstance(target, dict):
                continue
            if target.get("requires_caller") == "operator":
                continue
            next_chunk = target.get("next_chunk_id")
            if not isinstance(next_chunk, str) or not next_chunk:
                continue
            if next_chunk == "operator_review" or next_chunk not in chunks:
                continue
            if next_chunk not in seen:
                queue.append((next_chunk, distance + 1))
    return None


__all__ = [
    "DEFAULT_MAX_REPAIRS",
    "DEFAULT_MAX_CHUNK_TRANSITION_REPAIRS",
    "RepairResult",
    "repair_commit_debt",
    "repair_chunk_transition_debt",
    "repair_from_env",
    "repair_chunk_transition_from_env",
    "force_chunk_advance_from_env",
    "verify_findings_from_env",
]
