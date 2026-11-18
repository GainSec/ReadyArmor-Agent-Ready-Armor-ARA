"""AnomalyRecord + storage."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from ara.runtime.anomaly.divergence import DivergenceVector


AnomalyStatus = Literal[
    "pending", "promoted_to_lead", "dismissed_operator", "auto_dismissed_dup",
]


@dataclass(frozen=True)
class AnomalyRecord:
    id: str
    evidence_id: str
    crumb_id: str
    expected_shape_hash: str
    divergence: dict     # serialized DivergenceVector
    threshold_at_emission: float
    activation_id: str
    detected_at: float
    status: AnomalyStatus = "pending"
    promoted_lead_id: str | None = None
    dismissed_at: float | None = None
    dismissed_by: str | None = None
    dismiss_reason: str | None = None

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict) -> "AnomalyRecord":
        return cls(
            id=d["id"], evidence_id=d["evidence_id"], crumb_id=d["crumb_id"],
            expected_shape_hash=d["expected_shape_hash"],
            divergence=dict(d.get("divergence") or {}),
            threshold_at_emission=d["threshold_at_emission"],
            activation_id=d["activation_id"],
            detected_at=d["detected_at"],
            status=d.get("status", "pending"),
            promoted_lead_id=d.get("promoted_lead_id"),
            dismissed_at=d.get("dismissed_at"),
            dismissed_by=d.get("dismissed_by"),
            dismiss_reason=d.get("dismiss_reason"),
        )


def mint_anomaly_id() -> str:
    h = hashlib.sha256(uuid.uuid4().bytes + str(time.time()).encode()).hexdigest()
    return "A:" + h[:10]


def divergence_to_dict(dv: DivergenceVector) -> dict:
    return {
        "missing_required": list(dv.missing_required),
        "pattern_mismatches": dict(dv.pattern_mismatches),
        "unexpected_fields": list(dv.unexpected_fields),
        "structural_violations": list(dv.structural_violations),
        "line_matcher_failures": list(dv.line_matcher_failures),
        "section_matcher_failures": list(dv.section_matcher_failures),
        "raw_score": dv.raw_score,
    }


class AnomalyStore:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "anomalies.jsonl"
        self._lock = threading.RLock()

    def append(self, rec: AnomalyRecord) -> AnomalyRecord:
        with self._lock:
            self._write_line(json.dumps(rec.to_jsonable(), sort_keys=True, default=str))
        return rec

    def all(self) -> list[AnomalyRecord]:
        latest: dict[str, AnomalyRecord] = {}
        if not self.path.exists():
            return []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                latest[d["id"]] = AnomalyRecord.from_jsonable(d)
        return list(latest.values())

    def by_id(self, aid: str) -> AnomalyRecord | None:
        for r in self.all():
            if r.id == aid:
                return r
        return None

    def by_status(self, *statuses: str) -> list[AnomalyRecord]:
        targets = set(statuses)
        return [r for r in self.all() if r.status in targets]

    def count_for_crumb_in_activation(
        self, crumb_id: str, activation_id: str,
    ) -> int:
        return sum(
            1 for r in self.all()
            if r.crumb_id == crumb_id and r.activation_id == activation_id
        )

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
