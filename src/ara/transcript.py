"""Transcript writer for the prompt-layer channel.

Records each turn of the LLM conversation between an ARA-aware agent and
its model: the request payload (system + messages + tools) and the
response payload (assistant content blocks, tool calls, usage).

File: ``<session>/transcript.jsonl``

Hash-chained the same way ``policy_hits.jsonl`` is — verifiable via
``ara verify-audit`` once the chain switch is enabled here too.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .hashchain import GENESIS_HASH as _GENESIS_HASH, hash_line as _hash_line


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    resolved = path.resolve(strict=False)
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[resolved] = lock
        return lock


class TranscriptWriter:
    """Append-only writer for transcript records. Synchronous (single
    supervisor task owns it; no asyncio lock needed)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = _lock_for(path)

    def _resume_chain(self) -> str:
        if not self.path.exists():
            return _GENESIS_HASH
        try:
            with self.path.open("rb") as f:
                try:
                    f.seek(-8192, os.SEEK_END)
                except OSError:
                    f.seek(0)
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return _GENESIS_HASH
        last_body = ""
        last_prev = _GENESIS_HASH
        for line in tail.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            last_prev = str(rec.get("prev_hash") or _GENESIS_HASH)
            last_body = line
        if not last_body:
            return _GENESIS_HASH
        return _hash_line(last_prev, last_body)

    def append(self, record: dict) -> None:
        with self._lock:
            last_hash = self._resume_chain()
            payload = dict(record)
            payload.setdefault("ts", time.time())
            payload["prev_hash"] = last_hash
            line = json.dumps(payload, sort_keys=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
