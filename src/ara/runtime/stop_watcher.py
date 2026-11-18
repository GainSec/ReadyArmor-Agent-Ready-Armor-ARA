"""Stop-condition evaluator.

Per spec ``armor-trim-v0.1-amendment.md`` §10. The trim's
``stop_conditions`` list is consulted by the finding-stream subscriber
on every new finding (and by the supervisor on a wall-clock tick for
``duration_exceeded``). When any condition trips, new crumb dispatch
halts; in-flight crumbs finish; operator decides resume vs close.

Conditions supported (spec table):

- ``first_critical`` — halt on first ``severity==critical`` finding.
- ``first_p1`` — halt on first ``severity==p1`` finding (priority 1).
- ``max_findings:N`` — halt at N total findings.
- ``duration_exceeded:Nh`` — halt when ``Nh`` past ``test_window.start``
  (or trim apply time if no window).

This module is a pure evaluator + a small persistent latch for
"already-tripped" so re-entrant ticks don't fire the modal twice.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------

@dataclass
class FindingsSummary:
    """Aggregate the watcher needs from the finding stream. The caller
    builds this from the live store; the evaluator stays pure."""
    total: int = 0
    by_severity: dict[str, int] = None         # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_severity is None:
            self.by_severity = {}


@dataclass
class StopVerdict:
    tripped: bool
    condition: str | None                      # which condition fired
    reason: str = ""


_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_DURATION_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_duration(s: str) -> int | None:
    m = _DURATION_RE.match(s.strip())
    if not m:
        return None
    return int(m.group(1)) * _DURATION_MULT[m.group(2)]


def evaluate(
    conditions: list[str],
    summary: FindingsSummary,
    *,
    now_epoch: float,
    window_start_epoch: float | None = None,
) -> StopVerdict:
    """Pure. First condition to trip wins (operators read stop reasons
    in declaration order — first-match keeps behavior intuitive)."""
    if not conditions:
        return StopVerdict(tripped=False, condition=None)
    for cond in conditions:
        cond = cond.strip()
        if cond == "first_critical":
            if (summary.by_severity or {}).get("critical", 0) > 0:
                return StopVerdict(
                    tripped=True, condition=cond,
                    reason="first critical finding",
                )
        elif cond == "first_p1":
            if (summary.by_severity or {}).get("p1", 0) > 0:
                return StopVerdict(
                    tripped=True, condition=cond,
                    reason="first p1 finding",
                )
        elif cond.startswith("max_findings:"):
            try:
                n = int(cond.split(":", 1)[1])
            except (ValueError, IndexError):
                continue
            if summary.total >= n:
                return StopVerdict(
                    tripped=True, condition=cond,
                    reason=f"reached {n} total findings",
                )
        elif cond.startswith("duration_exceeded:"):
            dur = _parse_duration(cond.split(":", 1)[1])
            if dur is None or window_start_epoch is None:
                continue
            if now_epoch >= window_start_epoch + dur:
                return StopVerdict(
                    tripped=True, condition=cond,
                    reason=f"engagement duration exceeded {cond.split(':',1)[1]}",
                )
        # Unknown conditions are skipped silently — forward-compatible
        # with future expansion.
    return StopVerdict(tripped=False, condition=None)


# ---------------------------------------------------------------------------
# Latch (persistent already-tripped flag, per engagement)
# ---------------------------------------------------------------------------

@dataclass
class TripLatch:
    engagement_id: str
    tripped: bool = False
    condition: str | None = None
    tripped_at: str | None = None              # ISO-8601 UTC
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TripLatch":
        return cls(
            engagement_id=d["engagement_id"],
            tripped=bool(d.get("tripped", False)),
            condition=d.get("condition"),
            tripped_at=d.get("tripped_at"),
            reason=d.get("reason") or "",
        )


def latch_path(state_root: Path, engagement_id: str) -> Path:
    return Path(state_root) / "stop_state" / f"{engagement_id}.json"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def load_latch(state_root: Path, engagement_id: str) -> TripLatch:
    p = latch_path(Path(state_root), engagement_id)
    if not p.is_file():
        return TripLatch(engagement_id=engagement_id)
    try:
        return TripLatch.from_dict(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError, KeyError):
        return TripLatch(engagement_id=engagement_id)


def save_latch(state_root: Path, latch: TripLatch) -> None:
    p = latch_path(Path(state_root), latch.engagement_id)
    _atomic_write(p, json.dumps(latch.to_dict(), indent=2, sort_keys=True))


def reset_latch(state_root: Path, engagement_id: str) -> None:
    """Operator consumed RESUME-AFTER-STOP — clear the latch so future
    findings can trip new conditions independently."""
    save_latch(Path(state_root), TripLatch(engagement_id=engagement_id))


def evaluate_and_latch(
    state_root: Path, engagement_id: str, *,
    conditions: list[str],
    summary: FindingsSummary,
    now_epoch: float,
    window_start_epoch: float | None = None,
) -> StopVerdict:
    """Composite: evaluate + persist latch on first trip. Idempotent —
    once latched, returns the original trip verdict on every call until
    ``reset_latch`` is called.
    """
    latch = load_latch(state_root, engagement_id)
    if latch.tripped:
        return StopVerdict(
            tripped=True, condition=latch.condition,
            reason=f"latched: {latch.reason}" if latch.reason else "latched",
        )
    verdict = evaluate(
        conditions, summary,
        now_epoch=now_epoch, window_start_epoch=window_start_epoch,
    )
    if verdict.tripped:
        latch.tripped = True
        latch.condition = verdict.condition
        latch.reason = verdict.reason
        latch.tripped_at = datetime.fromtimestamp(
            now_epoch, tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_latch(state_root, latch)
    return verdict


__all__ = [
    "FindingsSummary", "StopVerdict", "TripLatch",
    "evaluate", "evaluate_and_latch",
    "latch_path", "load_latch", "reset_latch", "save_latch",
]
