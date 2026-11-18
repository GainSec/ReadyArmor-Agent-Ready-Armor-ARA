"""Time-of-day enforcement primitives.

Per spec ``armor-trim-v0.1-amendment.md`` §7. The supervisor consults
``in_any_window(tod_allowed, now)`` on every ``oc:*`` call. When out
of window, the call is held and a 4-way modal (stop / extend / pivot /
interactive) is presented to the operator.

This module owns:

- Pure evaluation: ``in_any_window``, ``next_window_open``,
  ``current_local_time``.
- Decision state: where the operator's decision (extend / pivot /
  resume) is persisted between window-open events.

State file: ``<state>/tod_state/<engagement_id>.json``::

    {
      "engagement_id": "acme-2026-04",
      "current_state": "in_window" | "blocked" | "extended" | "pivot_readonly",
      "active_decision": { decision_kind, granted_at, granted_by,
                            ttl_seconds, expires_at, reason } | null,
      "last_evaluation_at": "...Z",
      "history": [ ...past decisions... ]
    }

The actual modal UX lives in the wizard (Step 6). This module is
the substrate.

Wrap-around windows (e.g. ``"22:00"–"06:00"``) are supported by
splitting the window into two segments at midnight.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as _time, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError    # py3.9+
except ImportError:                                          # pragma: no cover
    ZoneInfo = None                                          # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception                        # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Day / time helpers
# ---------------------------------------------------------------------------

_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(s: str) -> _time:
    m = _HHMM_RE.match(s.strip())
    if not m:
        raise ValueError(f"bad time format: {s!r} (expected HH:MM)")
    return _time(hour=int(m.group(1)), minute=int(m.group(2)))


def _normalize_day(d: str) -> int:
    key = d.strip().lower()
    if key not in _DAY_MAP:
        raise ValueError(f"unknown day: {d!r} (expected mon/tue/.../sun)")
    return _DAY_MAP[key]


def _zone(tz_name: str | None):
    if not tz_name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _local_dt(now_epoch: float, tz_name: str | None) -> datetime:
    return datetime.fromtimestamp(now_epoch, tz=_zone(tz_name))


# ---------------------------------------------------------------------------
# Window matching (pure)
# ---------------------------------------------------------------------------

def _split_window(start: _time, end: _time) -> list[tuple[_time, _time]]:
    """Split a window into one or two non-wrapping segments. End time
    is treated as exclusive — ``09:00``–``17:00`` matches up to but not
    including 17:00 (a contract running ``until 17:00`` shouldn't allow
    ops at 17:00 sharp).
    """
    if end > start:
        return [(start, end)]
    if end == start:                          # zero-length window — never matches
        return []
    # Wraps midnight — split into [start..24:00) and [00:00..end)
    return [(start, _time(23, 59, 59)), (_time(0, 0), end)]


def _window_matches(window: dict, now_epoch: float) -> bool:
    days_raw = window.get("days") or []
    start_raw = window.get("start")
    end_raw = window.get("end")
    if not days_raw or not start_raw or not end_raw:
        return False
    try:
        days = {_normalize_day(d) for d in days_raw}
        start = _parse_hhmm(start_raw)
        end = _parse_hhmm(end_raw)
    except ValueError:
        return False
    tz_name = window.get("tz") or "UTC"
    local = _local_dt(now_epoch, tz_name)
    segments = _split_window(start, end)
    if not segments:
        return False
    if end > start:
        # Non-wrap: today must be in `days` AND time in [start, end).
        if local.weekday() not in days:
            return False
        return start <= local.time() < end
    # Wrap-around: the *first* segment is owned by today's day; the
    # *second* (post-midnight) is owned by tomorrow's day. We need to
    # check today's day for the first half and yesterday's day for the
    # second half (because the window opened yesterday).
    seg1_start, seg1_end = segments[0]
    seg2_start, seg2_end = segments[1]
    cur_t = local.time()
    if local.weekday() in days and seg1_start <= cur_t <= seg1_end:
        return True
    yesterday = (local.weekday() - 1) % 7
    if yesterday in days and seg2_start <= cur_t < seg2_end:
        return True
    return False


def in_any_window(tod_allowed: list[dict], now_epoch: float) -> bool:
    """True if the trim has no TOD restriction or current time falls
    inside one of the allowed windows. Pure."""
    if not tod_allowed:
        return True
    for w in tod_allowed:
        if _window_matches(w, now_epoch):
            return True
    return False


def next_window_open(
    tod_allowed: list[dict], now_epoch: float, *,
    lookahead_seconds: int = 14 * 86400,
) -> float | None:
    """Return the epoch of the next window-open after ``now_epoch``,
    or None if no allowed window falls within ``lookahead_seconds``.

    The lookahead defaults to 2 weeks — enough for any sane engagement
    contract; a year-out contract with sparse windows is a degenerate
    case the operator can ask about explicitly.
    """
    if not tod_allowed:
        return None
    if in_any_window(tod_allowed, now_epoch):
        return None
    # Step minute-by-minute — coarse but sufficient since TOD windows
    # are minute-aligned. Keep lookahead bounded so we don't loop forever
    # on a misconfigured trim.
    horizon = now_epoch + lookahead_seconds
    cursor = now_epoch + 60.0           # check the next minute
    while cursor <= horizon:
        if in_any_window(tod_allowed, cursor):
            return cursor
        cursor += 60.0
    return None


# ---------------------------------------------------------------------------
# Decision state (operator's modal outcome)
# ---------------------------------------------------------------------------

DECISION_STOP = "stop_until_window"
DECISION_EXTEND = "continue_for_ttl"
DECISION_PIVOT = "pivot_readonly"
DECISION_INTERACTIVE = "interactive"

VALID_DECISIONS = frozenset({
    DECISION_STOP, DECISION_EXTEND, DECISION_PIVOT, DECISION_INTERACTIVE,
})


@dataclass
class TODDecision:
    decision: str
    granted_at: str                            # ISO-8601 UTC
    granted_by: str = ""
    ttl_seconds: int | None = None
    expires_at: str | None = None              # ISO-8601 UTC; None for stop/interactive
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TODDecision":
        return cls(
            decision=d["decision"],
            granted_at=d.get("granted_at") or "",
            granted_by=d.get("granted_by") or "",
            ttl_seconds=d.get("ttl_seconds"),
            expires_at=d.get("expires_at"),
            reason=d.get("reason") or "",
        )


@dataclass
class TODState:
    engagement_id: str
    current_state: str = "in_window"           # in_window | blocked | extended | pivot_readonly
    active_decision: TODDecision | None = None
    last_evaluation_at: str = ""
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["active_decision"] = (
            self.active_decision.to_dict()
            if self.active_decision is not None else None
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TODState":
        ad = d.get("active_decision")
        return cls(
            engagement_id=d["engagement_id"],
            current_state=d.get("current_state") or "in_window",
            active_decision=TODDecision.from_dict(ad) if ad else None,
            last_evaluation_at=d.get("last_evaluation_at") or "",
            history=list(d.get("history") or []),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _state_dir(state_root: Path) -> Path:
    return Path(state_root) / "tod_state"


def state_path(state_root: Path, engagement_id: str) -> Path:
    return _state_dir(state_root) / f"{engagement_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def load_state(state_root: Path, engagement_id: str) -> TODState:
    p = state_path(Path(state_root), engagement_id)
    if not p.is_file():
        return TODState(engagement_id=engagement_id)
    try:
        return TODState.from_dict(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError, KeyError):
        return TODState(engagement_id=engagement_id)


def _save_state(state_root: Path, state: TODState) -> None:
    state.last_evaluation_at = _now_iso()
    p = state_path(Path(state_root), state.engagement_id)
    _atomic_write(p, json.dumps(state.to_dict(), indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Decision actions
# ---------------------------------------------------------------------------

def grant_decision(
    state_root: Path, engagement_id: str, decision: str, *,
    granted_by: str = "", ttl_seconds: int | None = None,
    reason: str = "", now_epoch: float | None = None,
) -> TODDecision:
    """Persist an operator decision. ``decision`` must be one of the
    four valid kinds. ``ttl_seconds`` is required for ``extend`` and
    ``pivot_readonly``; ignored for ``stop`` / ``interactive``.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"invalid TOD decision: {decision!r} "
            f"(expected one of {', '.join(sorted(VALID_DECISIONS))})"
        )
    if decision in (DECISION_EXTEND, DECISION_PIVOT) and not ttl_seconds:
        raise ValueError(
            f"{decision} requires a positive ttl_seconds"
        )

    now_epoch_real = (
        now_epoch
        if now_epoch is not None
        else datetime.now(timezone.utc).timestamp()
    )
    granted_at = datetime.fromtimestamp(
        now_epoch_real, tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at: str | None = None
    if ttl_seconds:
        expires_at = datetime.fromtimestamp(
            now_epoch_real + ttl_seconds, tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    rec = TODDecision(
        decision=decision,
        granted_at=granted_at,
        granted_by=granted_by,
        ttl_seconds=ttl_seconds,
        expires_at=expires_at,
        reason=reason,
    )

    st = load_state(state_root, engagement_id)
    if st.active_decision is not None:
        st.history.append(st.active_decision.to_dict())
    st.active_decision = rec
    if decision == DECISION_STOP:
        st.current_state = "blocked"
    elif decision == DECISION_EXTEND:
        st.current_state = "extended"
    elif decision == DECISION_PIVOT:
        st.current_state = "pivot_readonly"
    else:
        st.current_state = "blocked"           # interactive doesn't unblock either
    _save_state(state_root, st)
    return rec


def reset_on_window_open(
    state_root: Path, engagement_id: str,
) -> None:
    """Called by the supervisor when a new window opens. Archives the
    active decision (if any) and returns the trim to ``in_window``."""
    st = load_state(state_root, engagement_id)
    if st.active_decision is not None:
        st.history.append(st.active_decision.to_dict())
        st.active_decision = None
    st.current_state = "in_window"
    _save_state(state_root, st)


# ---------------------------------------------------------------------------
# Composite gate (the actual call the supervisor makes)
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    allow: bool
    reason: str
    state: str                                 # mirrors TODState.current_state
    next_window_open_epoch: float | None = None
    requires_decision: bool = False
    active_decision: TODDecision | None = None


def evaluate_gate(
    state_root: Path, engagement_id: str, *,
    tod_allowed: list[dict],
    now_epoch: float,
    op_kind: str = "active",                   # "active" | "read" | "any"
) -> GateResult:
    """Combine raw TOD evaluation with persisted operator decisions.

    Decision precedence (from highest):
      1. ``stop`` (blocked) — deny everything.
      2. ``pivot_readonly`` — allow `read`, deny otherwise.
      3. ``extended`` (within TTL) — allow everything.
      4. Plain TOD: allow if in any window, deny if outside.

    Pivot/extend decisions auto-expire when their TTL passes; this
    function transparently sweeps expired decisions to keep state in
    sync without requiring a separate cron.
    """
    st = load_state(state_root, engagement_id)
    # Sweep expired pivot/extend decisions
    if st.active_decision is not None and st.active_decision.expires_at:
        try:
            exp = datetime.strptime(
                st.active_decision.expires_at, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            exp = None
        if exp is not None and exp <= now_epoch:
            st.history.append(st.active_decision.to_dict())
            st.active_decision = None
            st.current_state = "in_window"
            _save_state(state_root, st)

    # Hard decisions override raw TOD.
    if st.active_decision is not None:
        d = st.active_decision.decision
        if d == DECISION_STOP:
            return GateResult(
                allow=False,
                reason="operator stopped engagement until next window",
                state=st.current_state,
                next_window_open_epoch=next_window_open(tod_allowed, now_epoch),
                active_decision=st.active_decision,
            )
        if d == DECISION_PIVOT:
            allow = op_kind == "read"
            return GateResult(
                allow=allow,
                reason=(
                    "pivot to artifact review — read ops only"
                    if allow else
                    "pivot to artifact review denies non-read ops"
                ),
                state=st.current_state,
                active_decision=st.active_decision,
            )
        if d == DECISION_EXTEND:
            return GateResult(
                allow=True,
                reason="operator extended past TOD window",
                state=st.current_state,
                active_decision=st.active_decision,
            )
        # interactive — deny by default; operator gates each call.
        return GateResult(
            allow=False,
            reason="interactive mode — each call needs explicit operator approval",
            state=st.current_state,
            requires_decision=True,
            active_decision=st.active_decision,
        )

    # No active decision — fall back to plain TOD.
    if in_any_window(tod_allowed, now_epoch):
        return GateResult(allow=True, reason="within TOD window", state="in_window")
    return GateResult(
        allow=False,
        reason="outside TOD window",
        state="blocked",
        next_window_open_epoch=next_window_open(tod_allowed, now_epoch),
        requires_decision=True,
    )


__all__ = [
    "DECISION_EXTEND", "DECISION_INTERACTIVE", "DECISION_PIVOT", "DECISION_STOP",
    "GateResult", "TODDecision", "TODState",
    "VALID_DECISIONS",
    "evaluate_gate", "grant_decision", "in_any_window",
    "load_state", "next_window_open", "reset_on_window_open",
    "state_path",
]
