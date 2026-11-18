"""Synchronous hash-chained JSONL audit writer for forge + runtime sinks.

Mirrors :class:`ara.audit.JsonlWriter` (async) but exposes a synchronous
API. Same on-disk format: each line is JSON with a ``prev_hash`` field
referencing the prior line's hash, computed via the shared
:mod:`ara.hashchain` primitives so :func:`ara.audit.verify_chain` works
against either writer's output.

Used by:
- ``<forge>/<forge_id>/audit.jsonl`` — forge phase events
- ``<sessions>/<sid>/events.jsonl`` — generic typed-channel audit
- (existing ``policy_hits.jsonl`` keeps using async writer; format compatible)
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ara.audit import verify_chain as _verify_chain
from ara.hashchain import GENESIS_HASH, hash_line


@dataclass(frozen=True)
class AuditEvent:
    ts: float
    kind: str
    body: dict
    prev_hash: str
    self_hash: str


class SyncAuditWriter:
    """Per-path append-only chained writer. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._last_hash: str | None = None

    def _resume_chain(self) -> str:
        if not self.path.exists():
            return GENESIS_HASH
        try:
            with self.path.open("rb") as f:
                try:
                    f.seek(-8192, os.SEEK_END)
                except OSError:
                    f.seek(0)
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return GENESIS_HASH
        last_body = ""
        last_prev = GENESIS_HASH
        for line in tail.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            last_prev = str(rec.get("prev_hash") or GENESIS_HASH)
            last_body = line
        if not last_body:
            return GENESIS_HASH
        return hash_line(last_prev, last_body)

    def append(self, body: dict, kind: str) -> AuditEvent:
        ts = time.time()
        with self._lock:
            if self._last_hash is None:
                self._last_hash = self._resume_chain()
            payload = {"ts": ts, "kind": kind, "body": body, "prev_hash": self._last_hash}
            line = json.dumps(payload, sort_keys=True)
            new_hash = hash_line(self._last_hash, line)
            self._write_sync(line + "\n")
            event = AuditEvent(
                ts=ts,
                kind=kind,
                body=body,
                prev_hash=self._last_hash,
                self_hash=new_hash,
            )
            self._last_hash = new_hash
            return event

    def _write_sync(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)


_REGISTRY: dict[Path, SyncAuditWriter] = {}
_REGISTRY_LOCK = threading.Lock()


def writer_for(path: Path) -> SyncAuditWriter:
    """Get-or-create the writer instance for a given sink path.

    Reusing instances preserves the in-memory chain head and avoids
    re-reading the file tail on every append.
    """
    p = Path(path).resolve()
    with _REGISTRY_LOCK:
        w = _REGISTRY.get(p)
        if w is None:
            w = SyncAuditWriter(p)
            _REGISTRY[p] = w
        return w


def append(sink_path: Path, body: dict, kind: str) -> AuditEvent:
    """Convenience: get-or-create writer and append one event."""
    return writer_for(sink_path).append(body, kind)


def verify_chain(sink_path: Path) -> tuple[bool, int, str | None]:
    """Re-export of :func:`ara.audit.verify_chain` for symmetry."""
    return _verify_chain(Path(sink_path))


def reset_registry_for_tests() -> None:
    """Test-only: clear the writer cache so tmp paths don't leak between tests."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
