"""Append-only JSONL writers for session audit.

Each append is fsync'd. Writes are serialized via an asyncio.Lock so
concurrent guards can't interleave partial lines.

Optional hash-chaining: when ``chain=True`` is passed, every record carries
a ``prev_hash`` field (sha256 of the prior record's serialized content
combined with its own prev_hash). Tampering with any line later breaks
the chain — verifiable via ``verify_chain(path)``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .hashchain import GENESIS_HASH as _GENESIS_HASH, hash_line as _hash_line


async def _to_thread(fn, *args):
    to_thread = getattr(asyncio, "to_thread", None)
    if to_thread is not None:
        return await to_thread(fn, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


class JsonlWriter:
    def __init__(self, path: Path, *, chain: bool = False) -> None:
        self.path = path
        self.chain = chain
        self._lock: asyncio.Lock | None = None
        self._last_hash: str | None = None  # resolved on first append

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _resume_chain(self) -> str:
        """Recover the most recent hash from disk so a restart doesn't
        break the chain. Returns the genesis hash if the file is empty
        or missing."""
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

    async def append(self, record: dict) -> None:
        payload = dict(record)
        payload.setdefault("ts", time.time())
        if self.chain:
            async with self._get_lock():
                if self._last_hash is None:
                    self._last_hash = await _to_thread(self._resume_chain)
                payload["prev_hash"] = self._last_hash
                line = json.dumps(payload, sort_keys=True)
                self._last_hash = _hash_line(self._last_hash, line)
                await _to_thread(self._write_sync, line + "\n")
            return
        line = json.dumps(payload, sort_keys=True) + "\n"
        async with self._get_lock():
            await _to_thread(self._write_sync, line)

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


def verify_chain(path: Path) -> tuple[bool, int, str | None]:
    """Walk a chained JSONL file, recomputing each line's hash.

    Returns ``(ok, last_good_line_number, error_message)``. On success,
    ``ok=True`` and ``error_message=None``. On the first mismatch or
    structural break, returns ``ok=False`` plus the line number that
    couldn't be verified and a short reason.
    """
    if not path.exists():
        return (True, 0, None)
    prev = _GENESIS_HASH
    lineno = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                lineno += 1
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    return (False, lineno, f"json parse: {exc}")
                claimed = rec.get("prev_hash")
                if claimed is None:
                    return (False, lineno, "missing prev_hash (file not chained?)")
                if claimed != prev:
                    return (False, lineno, "prev_hash mismatch — chain broken")
                prev = _hash_line(prev, line)
    except OSError as exc:
        return (False, lineno, f"io: {exc}")
    return (True, lineno, None)
