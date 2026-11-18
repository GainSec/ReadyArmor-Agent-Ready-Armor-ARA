"""Activation-id minting and persistence.

A new activation_id is minted at session start and at every chunk swap.
Tool calls within an activation share the same id, which downstream
items (1.1 spatial_anchor, 2.3 finding-freshness, 3.2 anomaly
attribution) use to bound "this is from the current cognitive context".

Persistence:
- ``activation.txt`` — single line, the current id (atomic write).
- ``activations.jsonl`` — append-only history of every mint.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class ActivationRecord:
    ts: float
    activation_id: str
    reason: str
    chunk_id: str | None


class ActivationManager:
    def __init__(self, session_dir: Path):
        self._dir = Path(session_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._current_path = self._dir / "activation.txt"
        self._history_path = self._dir / "activations.jsonl"
        self._lock = threading.Lock()
        self._current: str | None = self._load_current()

    def _load_current(self) -> str | None:
        try:
            txt = self._current_path.read_text().strip()
            return txt or None
        except FileNotFoundError:
            return None

    def current(self) -> str:
        if self._current is None:
            return self.mint(reason="initial")
        return self._current

    def peek(self) -> str | None:
        """Return current id without minting if unset."""
        return self._current

    def mint(self, *, reason: str, chunk_id: str | None = None) -> str:
        new_id = uuid4().hex[:16]
        ts = time.time()
        with self._lock:
            self._current = new_id
            tmp = self._current_path.with_suffix(".txt.tmp")
            tmp.write_text(new_id)
            os.replace(tmp, self._current_path)
            with self._history_path.open("a") as f:
                f.write(json.dumps({
                    "ts": ts,
                    "activation_id": new_id,
                    "reason": reason,
                    "chunk_id": chunk_id,
                }) + "\n")
        return new_id

    def history(self) -> list[ActivationRecord]:
        if not self._history_path.exists():
            return []
        out: list[ActivationRecord] = []
        with self._history_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                out.append(ActivationRecord(
                    ts=d["ts"],
                    activation_id=d["activation_id"],
                    reason=d["reason"],
                    chunk_id=d.get("chunk_id"),
                ))
        return out
