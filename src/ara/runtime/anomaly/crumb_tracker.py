"""Per-engagement active-crumb tracker.

Minimal predecessor to the full ChunkRuntime (2.3). Supervisor /
agent calls :meth:`set_active(crumb_id)` before each tool call; the
auto-firer reads the current crumb when divergence checking new
evidence.

Thread-safe via ``threading.Lock``. One tracker instance per
engagement; the bootstrap helper installs a singleton for the
current ``sessions_root + engagement_id`` pair.
"""
from __future__ import annotations

import threading
from pathlib import Path


class CrumbTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: str | None = None
        self._history: list[tuple[float, str]] = []

    def set_active(self, crumb_id: str | None) -> None:
        with self._lock:
            self._current = crumb_id
            if crumb_id is not None:
                import time
                self._history.append((time.time(), crumb_id))

    def current(self) -> str | None:
        with self._lock:
            return self._current

    def clear(self) -> None:
        with self._lock:
            self._current = None


# Per-engagement registry — keyed by absolute session_dir path.
_REGISTRY: dict[Path, CrumbTracker] = {}
_REGISTRY_LOCK = threading.Lock()


def for_session(session_dir: Path) -> CrumbTracker:
    """Get-or-create the tracker for an engagement's session dir."""
    p = Path(session_dir).resolve()
    with _REGISTRY_LOCK:
        t = _REGISTRY.get(p)
        if t is None:
            t = CrumbTracker()
            _REGISTRY[p] = t
        return t


def reset_for_tests() -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
