from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


def default_state_root(cwd: Path | None = None) -> Path:
    """Location of the .ara/ tree. Sits under the invocation cwd for now."""
    return (cwd or Path.cwd()) / ".ara"


@dataclass(frozen=True)
class Session:
    id: str
    token: str
    root: Path
    started_at: float
    pid: int

    @property
    def socket_path(self) -> Path:
        return self.root / "guard.sock"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def policy_hits_path(self) -> Path:
        return self.root / "policy_hits.jsonl"

    @property
    def metadata_path(self) -> Path:
        return self.root / "session.json"

    def to_metadata(self) -> dict:
        return {
            "id": self.id,
            "token": self.token,
            "started_at": self.started_at,
            "pid": self.pid,
            "socket": str(self.socket_path),
        }


def create_session(
    state_root: Path | None = None, *, resume_id: str | None = None,
) -> Session:
    """Create a fresh session directory under ``state_root/.ara/sessions/<id>/``.

    If ``resume_id`` is given, adopt the existing session directory
    instead of creating a new one. The transcript + events + policy_hits
    files are appended to (no truncation), preserving the full history
    of the original session across supervisor restarts. A new guard
    token is generated since the old one may have leaked or expired;
    ``started_at`` and ``pid`` are updated to reflect the new run, but
    the ID stays stable.
    """
    root = state_root or default_state_root()
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    if resume_id:
        session_id = resume_id
        session_root = sessions_dir / session_id
        if not session_root.is_dir():
            raise FileNotFoundError(
                f"cannot resume: session {session_id!r} not found under "
                f"{sessions_dir}"
            )
    else:
        session_id = uuid.uuid4().hex
        session_root = sessions_dir / session_id
        session_root.mkdir(parents=True, exist_ok=False)

    session = Session(
        id=session_id,
        token=secrets.token_hex(16),
        root=session_root,
        started_at=time.time(),
        pid=os.getpid(),
    )
    # Record a resume marker so operators + audit can see where one
    # run ended and another began. Append-only; preserves history.
    if resume_id:
        resume_marker = {
            "kind": "session_resumed",
            "ts": session.started_at,
            "pid": session.pid,
        }
        with open(session.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(resume_marker) + "\n")
    _write_json(session.metadata_path, session.to_metadata())
    return session


def find_resumable_sessions(state_root: Path | None = None) -> list[Session]:
    """List sessions on disk that can be resumed (have a metadata file).

    Sorted newest-first by ``started_at``. Doesn't check liveness — a
    session whose supervisor process is still running can't be
    resumed safely; caller should verify.
    """
    root = state_root or default_state_root()
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return []
    out: list[Session] = []
    for p in sessions_dir.iterdir():
        if not p.is_dir():
            continue
        meta_path = p / "session.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        try:
            out.append(Session(
                id=meta.get("id") or p.name,
                token=meta.get("token") or "",
                root=p,
                started_at=float(meta.get("started_at") or 0),
                pid=int(meta.get("pid") or 0),
            ))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda s: s.started_at, reverse=True)
    return out


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)
