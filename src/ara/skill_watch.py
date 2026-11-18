"""OpenClaw skill + extension change detection.

Agents can install, upgrade, or swap skills (``~/.openclaw/skills/*``)
and extensions (``dist/extensions/*`` under the npm install) at any
time. The runtime behaviour of those skills is covered by ARA's
existing libc/Node/MITM enforcement — but the *install event* and
any *integrity change* are invisible today.

This module takes a SHA256 snapshot of every top-level skill +
extension directory's file tree, persists it under
``<state_root>/skill_index.json``, and re-scans on demand. Diffs
surface as ``skill_changed`` events written to the supervisor's
events.jsonl stream.

Intentionally narrow: we only watch two directories ARA knows about.
Operators who install openclaw in a non-standard location can pass
their own ``roots`` list.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# Default roots to scan. First-match-wins — if a root doesn't exist we
# silently skip it. Operators with custom openclaw installs set
# ``ARA_SKILL_ROOTS`` to a colon-separated override list.
_DEFAULT_ROOTS = (
    Path.home() / ".openclaw" / "skills",
    # Extensions live inside the npm-global tree; resolved lazily.
    Path.home() / ".npm-global" / "lib" / "node_modules" / "openclaw" / "dist" / "extensions",
)


@dataclass
class EntryFingerprint:
    name: str                 # dir name, e.g. "local-whisper"
    root: str                 # absolute path to the containing root
    files_count: int
    bytes_total: int
    sha256: str               # rolled hash of sorted (relpath, filehash)
    mtime_latest: float       # epoch

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Snapshot:
    taken_at: float
    entries: dict[str, EntryFingerprint] = field(default_factory=dict)
    # key format: "<root-absolute-path>:<entry-name>"

    def to_dict(self) -> dict:
        return {
            "taken_at": self.taken_at,
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        out = cls(taken_at=float(d.get("taken_at") or 0))
        for k, v in (d.get("entries") or {}).items():
            try:
                out.entries[k] = EntryFingerprint(**v)
            except TypeError:
                continue
        return out


@dataclass
class Change:
    kind: str                 # "added" | "removed" | "modified"
    entry_key: str
    name: str
    root: str
    before: Optional[EntryFingerprint] = None
    after: Optional[EntryFingerprint] = None


# ---- hashing ---------------------------------------------------------

def _hash_file(p: Path, bufsize: int = 1 << 18) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    try:
        with open(p, "rb") as f:
            while True:
                chunk = f.read(bufsize)
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
    except OSError:
        return "", 0
    return h.hexdigest(), n


def _fingerprint_dir(entry_dir: Path) -> EntryFingerprint:
    """Fingerprint a skill/extension directory.

    Reads every regular file up to ~5 MB per file (larger files
    hash their size+mtime only — true big binaries are out of
    scope). The rolled hash is order-stable so cross-filesystem
    comparisons work."""
    entries: list[tuple[str, str, int]] = []
    bytes_total = 0
    files_count = 0
    mtime_latest = 0.0
    max_file = 5 * 1024 * 1024
    if not entry_dir.is_dir():
        return EntryFingerprint(
            name=entry_dir.name, root=str(entry_dir.parent),
            files_count=0, bytes_total=0,
            sha256="", mtime_latest=0.0,
        )
    for dirpath, _dirnames, filenames in os.walk(entry_dir):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                st = fp.lstat()
            except OSError:
                continue
            if st.st_mtime > mtime_latest:
                mtime_latest = st.st_mtime
            if not fp.is_file() or fp.is_symlink():
                # Symlink shape: hash the target string, not its content.
                try:
                    target = os.readlink(fp)
                except OSError:
                    target = ""
                digest = hashlib.sha256(
                    f"SYMLINK:{target}".encode()
                ).hexdigest()
                rel = str(fp.relative_to(entry_dir))
                entries.append((rel, digest, 0))
                files_count += 1
                continue
            if st.st_size > max_file:
                digest = hashlib.sha256(
                    f"BIGFILE:{st.st_size}:{st.st_mtime}".encode()
                ).hexdigest()
            else:
                digest, _n = _hash_file(fp)
            rel = str(fp.relative_to(entry_dir))
            entries.append((rel, digest, st.st_size))
            files_count += 1
            bytes_total += st.st_size
    entries.sort(key=lambda t: t[0])
    rolling = hashlib.sha256()
    for rel, digest, size in entries:
        rolling.update(f"{rel}\0{digest}\0{size}\n".encode())
    return EntryFingerprint(
        name=entry_dir.name,
        root=str(entry_dir.parent),
        files_count=files_count,
        bytes_total=bytes_total,
        sha256=rolling.hexdigest(),
        mtime_latest=mtime_latest,
    )


# ---- snapshot / index -----------------------------------------------

def resolve_roots(override: Optional[Iterable[Path]] = None) -> list[Path]:
    if override is not None:
        return [Path(p).expanduser() for p in override]
    env = os.environ.get("ARA_SKILL_ROOTS")
    if env:
        return [Path(p).expanduser() for p in env.split(":") if p]
    return [Path(p).expanduser() for p in _DEFAULT_ROOTS]


def snapshot(roots: Optional[Iterable[Path]] = None) -> Snapshot:
    roots_list = resolve_roots(roots)
    snap = Snapshot(taken_at=time.time())
    for root in roots_list:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            fp = _fingerprint_dir(entry)
            key = f"{root}:{entry.name}"
            snap.entries[key] = fp
    return snap


def index_path(state_root: Path) -> Path:
    return state_root / "skill_index.json"


def load_index(state_root: Path) -> Optional[Snapshot]:
    p = index_path(state_root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return Snapshot.from_dict(data)


def save_index(state_root: Path, snap: Snapshot) -> None:
    p = index_path(state_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap.to_dict(), indent=2, sort_keys=True))


# ---- diff ------------------------------------------------------------

def diff(old: Snapshot, new: Snapshot) -> list[Change]:
    changes: list[Change] = []
    old_keys = set(old.entries)
    new_keys = set(new.entries)
    for key in sorted(new_keys - old_keys):
        fp = new.entries[key]
        changes.append(Change(
            kind="added", entry_key=key, name=fp.name,
            root=fp.root, after=fp,
        ))
    for key in sorted(old_keys - new_keys):
        fp = old.entries[key]
        changes.append(Change(
            kind="removed", entry_key=key, name=fp.name,
            root=fp.root, before=fp,
        ))
    for key in sorted(old_keys & new_keys):
        b, a = old.entries[key], new.entries[key]
        if b.sha256 != a.sha256:
            changes.append(Change(
                kind="modified", entry_key=key, name=a.name,
                root=a.root, before=b, after=a,
            ))
    return changes


# ---- public scan entrypoint -----------------------------------------

def scan(
    state_root: Path,
    roots: Optional[Iterable[Path]] = None,
) -> tuple[Snapshot, list[Change]]:
    """Take a fresh snapshot, diff against the persisted one, persist
    the new snapshot. Returns ``(snapshot, changes)``."""
    new = snapshot(roots)
    old = load_index(state_root)
    changes = diff(old, new) if old is not None else []
    save_index(state_root, new)
    return new, changes


def changes_to_events(changes: list[Change]) -> list[dict]:
    """Map ``Change`` records to ``skill_changed`` event dicts suitable
    for ``events.jsonl`` ingestion."""
    out: list[dict] = []
    for c in changes:
        ev = {
            "kind": "skill_changed",
            "change": c.kind,
            "name": c.name,
            "root": c.root,
            "entry_key": c.entry_key,
            "ts": time.time(),
        }
        if c.after:
            ev["sha256"] = c.after.sha256
            ev["files_count"] = c.after.files_count
            ev["bytes_total"] = c.after.bytes_total
        elif c.before:
            ev["sha256"] = c.before.sha256
        out.append(ev)
    return out
