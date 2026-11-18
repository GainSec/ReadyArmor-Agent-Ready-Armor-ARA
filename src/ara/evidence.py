"""Evidence provenance index.

Every artifact an ARA-supervised tool call produces (file written,
command output captured, URL fetched) can be hashed and recorded in a
tamper-evident JSONL log:

    <session_root>/evidence_index.jsonl

Each line is a ``CaptureRecord`` as JSON. Subsequent ``verify`` can
re-hash the on-disk artifact and confirm it matches the record.

Ported from BattleReadyArmor's ``capture_provenance.py`` approach —
the key feature is that you can prove *later* that the file the agent
claims it saw is bit-for-bit the file still on disk.

Usage::

    from ara import evidence
    rec = evidence.record_artifact(
        session_root,
        tool_use_id="t_abc",
        tool_name="oc:write",
        action="write",
        artifact_path="/tmp/session/out.txt",
    )
    # rec.sha256 is the hash; rec.capture_id is the index ID.

    # later:
    for r in evidence.load(session_root):
        ok = evidence.verify(r)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class CaptureRecord:
    capture_id: str
    recorded_at: float          # unix ts
    session_id: str
    tool_use_id: str
    tool_name: str              # e.g. "oc:write", "oc:bash"
    action: str                 # "write" | "read" | "fetch" | "exec" | "capture"
    artifact_path: str          # absolute path on disk
    artifact_sha256: str        # hex digest
    size_bytes: int
    hint: Optional[str] = None  # free-form context (URL, command, ...)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def _index_path(session_root: Path) -> Path:
    return session_root / "evidence_index.jsonl"


def new_capture_id() -> str:
    return "cap_" + uuid.uuid4().hex[:12]


def _sha256_file(path: Path, *, bufsize: int = 1 << 20) -> tuple[str, int]:
    """Streaming SHA256 + byte count. Returns (hex_digest, size)."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def record_artifact(
    session_root: Path,
    *,
    tool_use_id: str,
    tool_name: str,
    action: str,
    artifact_path: str,
    hint: str | None = None,
    session_id: str | None = None,
) -> CaptureRecord:
    """Hash the artifact, append a ``CaptureRecord`` to the index.

    ``session_root`` is the per-session dir (``<state_root>/sessions/<id>``).
    If ``session_id`` is None it's derived from ``session_root.name``.
    """
    p = Path(artifact_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"artifact missing or not a regular file: {p}")
    digest, size = _sha256_file(p)
    rec = CaptureRecord(
        capture_id=new_capture_id(),
        recorded_at=time.time(),
        session_id=session_id or session_root.name,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        action=action,
        artifact_path=str(p.resolve()),
        artifact_sha256=digest,
        size_bytes=size,
        hint=hint,
    )
    _append(session_root, rec)
    return rec


def _append(session_root: Path, rec: CaptureRecord) -> None:
    session_root.mkdir(parents=True, exist_ok=True)
    path = _index_path(session_root)
    with open(path, "a", encoding="utf-8") as f:
        f.write(rec.to_json())
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def load(session_root: Path) -> list[CaptureRecord]:
    path = _index_path(session_root)
    if not path.exists():
        return []
    out: list[CaptureRecord] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(CaptureRecord(**d))
            except (json.JSONDecodeError, TypeError):
                continue
    return out


def verify(rec: CaptureRecord) -> bool:
    """Re-hash the on-disk artifact and confirm it matches the record.

    Returns False if the file is gone, truncated, or has been modified.
    """
    p = Path(rec.artifact_path)
    if not p.exists() or not p.is_file():
        return False
    try:
        digest, size = _sha256_file(p)
    except OSError:
        return False
    return digest == rec.artifact_sha256 and size == rec.size_bytes


def verify_session(session_root: Path) -> dict:
    """Verify every record in the session's index; return a summary."""
    records = load(session_root)
    ok: list[str] = []
    missing: list[str] = []
    tampered: list[str] = []
    for r in records:
        p = Path(r.artifact_path)
        if not p.exists():
            missing.append(r.capture_id)
            continue
        if verify(r):
            ok.append(r.capture_id)
        else:
            tampered.append(r.capture_id)
    return {
        "total": len(records),
        "ok": ok,
        "missing": missing,
        "tampered": tampered,
    }


def find_by_tool_use(
    session_root: Path, tool_use_id: str,
) -> Iterable[CaptureRecord]:
    for r in load(session_root):
        if r.tool_use_id == tool_use_id:
            yield r
