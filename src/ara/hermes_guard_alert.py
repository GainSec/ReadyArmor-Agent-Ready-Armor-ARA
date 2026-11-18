"""Operator alert for the hermes guard's hold-off state.

When the circuit breaker holds hermes off (repeated rogue), we must reach the
operator out-of-band — hermes/Discord is down at that point. Today that means:
a durable event file (read by the Courtyard signal card + ``guard_events``) and
a ``debug_log`` line (surfaced on the ``/debug`` page's Hermes-guard stream).

Future work (tracked on the workbench): an alert banner component + an
independent ARA->Discord webhook so hold-off alerts reach the operator's phone.
"""
from __future__ import annotations

import json
from pathlib import Path

_EVENTS_FILE = "hermes_guard_events.jsonl"
_KIND = "hermes_guard_holdoff"


def raise_alert(state_root: Path, detail: str, now: float = 0.0) -> None:
    """Record a hold-off alert: a durable event (always) + a debug_log line
    (best-effort, for /debug)."""
    sr = Path(state_root)
    sr.mkdir(parents=True, exist_ok=True)
    with open(sr / _EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": _KIND, "detail": detail, "ts": now}) + "\n")
    try:
        from . import debug_log
        debug_log.emit("hermes-guard", "holdoff", state_root=sr,
                       level="error", detail=detail)
    except Exception:
        pass


def guard_events(state_root: Path, limit: int = 50) -> list:
    """Return recent hold-off events (newest last), for Courtyard + /debug."""
    p = Path(state_root) / _EVENTS_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("kind") == _KIND:
            out.append(d)
    return out[-limit:]
