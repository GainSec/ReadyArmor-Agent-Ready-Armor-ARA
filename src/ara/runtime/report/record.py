"""Report record + storage."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Report:
    id: str
    engagement_id: str
    weapon_id: str
    template_version: str
    findings_included: list[str]
    body_markdown: str
    rendered_at: float
    signed_off_by: str | None = None
    content_hash: str | None = None
    section_diagnostics: list[dict] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict) -> "Report":
        return cls(
            id=d["id"], engagement_id=d["engagement_id"],
            weapon_id=d["weapon_id"], template_version=d["template_version"],
            findings_included=list(d.get("findings_included") or []),
            body_markdown=d["body_markdown"],
            rendered_at=d["rendered_at"],
            signed_off_by=d.get("signed_off_by"),
            content_hash=d.get("content_hash"),
            section_diagnostics=list(d.get("section_diagnostics") or []),
        )


def mint_report_id() -> str:
    h = hashlib.sha256(uuid.uuid4().bytes + str(time.time()).encode()).hexdigest()
    return "R:" + h[:10]


def compute_content_hash(r: Report) -> str:
    canon = json.dumps(r.to_jsonable(), sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(canon).hexdigest()


class ReportStore:
    """Append-only JSONL store of rendered reports for one engagement."""
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "reports.jsonl"
        self._lock = threading.RLock()

    def append(self, r: Report) -> Report:
        with self._lock:
            self._write_line(json.dumps(r.to_jsonable(), sort_keys=True, default=str))
        return r

    def all(self) -> list[Report]:
        latest: dict[str, Report] = {}
        if not self.path.exists():
            return []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                latest[d["id"]] = Report.from_jsonable(d)
        return list(latest.values())

    def by_id(self, rid: str) -> Report | None:
        for r in self.all():
            if r.id == rid:
                return r
        return None

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
