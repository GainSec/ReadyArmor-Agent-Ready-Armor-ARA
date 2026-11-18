"""FindingStore: append-only JSONL with latest-wins semantics by id."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ara.runtime.findings.record import Finding


class FindingStore:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "findings.jsonl"
        self._lock = threading.RLock()

    def append(self, f: Finding) -> Finding:
        with self._lock:
            self._write_line(json.dumps(f.to_jsonable(), sort_keys=True, default=str))
        return f

    def all(self) -> list[Finding]:
        latest: dict[str, Finding] = {}
        if not self.path.exists():
            return []
        with self.path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                latest[d["id"]] = Finding.from_jsonable(d)
        return list(latest.values())

    def by_id(self, fid: str) -> Finding | None:
        for f in self.all():
            if f.id == fid:
                return f
        return None

    def by_status(self, *statuses: str) -> list[Finding]:
        targets = set(statuses)
        return [f for f in self.all() if f.status in targets]

    def signed_off(self) -> list[Finding]:
        return self.by_status("signed_off")

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
