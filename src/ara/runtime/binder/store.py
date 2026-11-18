"""Content-addressed evidence storage on disk.

Layout::

    <session_dir>/evidence/sha256/ab/cd/<rest>.bin

Content is keyed by sha256; identical bytes are stored once. Two-level
directory split avoids huge directories at scale.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def evidence_id_for(sha: str) -> str:
    return f"e:{sha[:10]}"


class ContentStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, sha: str) -> Path:
        return self.root / sha[:2] / sha[2:4] / f"{sha[4:]}.bin"

    def put(self, content: bytes) -> tuple[str, Path]:
        sha = sha256_hex(content)
        target = self._path_for(sha)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".bin.tmp")
            with tmp.open("wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        return sha, target

    def get(self, sha: str, range: tuple[int, int] | None = None) -> bytes:
        target = self._path_for(sha)
        if not target.exists():
            raise FileNotFoundError(f"evidence content missing for {sha[:10]}")
        if range is None:
            return target.read_bytes()
        offset, length = range
        with target.open("rb") as f:
            f.seek(offset)
            return f.read(length)

    def head(self, sha: str, kb: int = 2) -> bytes:
        return self.get(sha, range=(0, kb * 1024))

    def exists(self, sha: str) -> bool:
        return self._path_for(sha).exists()

    def size(self, sha: str) -> int:
        return self._path_for(sha).stat().st_size
