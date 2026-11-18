"""Minimal in-tree replacement for the deleted ``ara.overrides`` module.

Overrides/blackholes are now Bulwark ``allow``/``deny`` rules on the
fs/exec layers. Legacy ``overrides.json`` files were renamed to
``.bulwark-migrated.bak`` on first armory startup, so the data layer
is empty — every read returns ``Store()`` and every write is a no-op.

Kept only as an import stub; new code must not depend on it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Entry:
    id: str
    kind: str
    pattern: str
    added_at: float
    added_by: str = "operator"
    reason: Optional[str] = None
    scope: str = "always"
    expires_at: Optional[float] = None

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


def new_entry_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class Store:
    overrides: list[Entry] = field(default_factory=list)
    blackholes: list[Entry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "overrides": [e.to_dict() for e in self.overrides],
            "blackholes": [e.to_dict() for e in self.blackholes],
        }

    def merge(self, other: "Store") -> "Store":
        seen = {e.id for e in self.overrides} | {e.id for e in self.blackholes}
        ov = list(self.overrides) + [e for e in other.overrides if e.id not in seen]
        bh = list(self.blackholes) + [e for e in other.blackholes if e.id not in seen]
        return Store(overrides=ov, blackholes=bh)


def path_for(state_root: Path) -> Path:
    return state_root / "overrides.json"


# ---- no-op reads / writes ------------------------------------------

def load(state_root: Path) -> Store:
    return Store()


def load_at(path: Path) -> Store:
    return Store()


def save(state_root: Path, store: Store) -> None:  # pragma: no cover
    pass


def save_at(path: Path, store: Store) -> None:  # pragma: no cover
    pass


# ---- no-op modify helpers (add_override / add_blackhole / remove) ---

def _stub_entry(kind: str, pattern: str) -> Entry:
    return Entry(
        id=new_entry_id(), kind=kind, pattern=pattern,
        added_at=time.time(),
    )


def add_override(state_root: Path, **kw) -> Entry:  # pragma: no cover
    return _stub_entry(kw.get("kind") or "path", kw.get("pattern") or "")


def add_blackhole(state_root: Path, **kw) -> Entry:  # pragma: no cover
    return _stub_entry(kw.get("kind") or "path", kw.get("pattern") or "")


def add_override_at(path: Path, **kw) -> Entry:  # pragma: no cover
    return _stub_entry(kw.get("kind") or "path", kw.get("pattern") or "")


def add_blackhole_at(path: Path, **kw) -> Entry:  # pragma: no cover
    return _stub_entry(kw.get("kind") or "path", kw.get("pattern") or "")


def remove(state_root: Path, entry_id: str) -> bool:  # pragma: no cover
    return False


def remove_at(path: Path, entry_id: str) -> bool:  # pragma: no cover
    return False


def find(state_root: Path, entry_id: str) -> Optional[Entry]:  # pragma: no cover
    return None


def parse_ttl(spec: str) -> float:
    """Parse a duration like ``5m`` / ``2h`` / ``1d`` into seconds."""
    if not spec:
        return 0.0
    s = spec.strip().lower()
    if not s:
        return 0.0
    unit = s[-1]
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    if mult is None:
        try:
            return float(s)
        except ValueError:
            return 0.0
    try:
        return float(s[:-1]) * mult
    except ValueError:
        return 0.0
