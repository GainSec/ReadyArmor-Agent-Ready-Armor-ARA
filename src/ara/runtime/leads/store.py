"""LeadStore — append-only JSONL with latest-wins semantics by lead_id.

Each mutation appends a new full record. Reads collapse to the latest
entry per id.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ara.runtime.leads.record import Lead


class LeadStore:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "leads.jsonl"
        self._lock = threading.RLock()

    def append(self, lead: Lead) -> Lead:
        with self._lock:
            self._write_line(json.dumps(lead.to_jsonable(), sort_keys=True, default=str))
        return lead

    def all(self) -> list[Lead]:
        latest: dict[str, Lead] = {}
        if not self.path.exists():
            return []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                latest[d["id"]] = Lead.from_jsonable(d)
        return list(latest.values())

    def by_id(self, lead_id: str) -> Lead | None:
        for ld in self.all():
            if ld.id == lead_id:
                return ld
        return None

    def by_status(self, *statuses: str) -> list[Lead]:
        targets = set(statuses)
        return [ld for ld in self.all() if ld.status in targets]

    def open_leads(self) -> list[Lead]:
        return self.by_status("open", "investigating")

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
