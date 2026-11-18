"""Audit #6 — delete a single archived session and bulk-purge old ones."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ara.armory.app import _delete_archived_session, _cleanup_archived_sessions


def _mk_session(state: Path, sid: str, *, pid: int | None, mtime_age_days: float) -> Path:
    sdir = state / "sessions" / sid
    sdir.mkdir(parents=True)
    (sdir / "events.jsonl").write_text("{}\n")
    meta = {"session_id": sid}
    if pid is not None:
        meta["pid"] = pid
    (sdir / "session.json").write_text(json.dumps(meta))
    past = time.time() - mtime_age_days * 86400.0
    os.utime(sdir, (past, past))
    return sdir


def test_delete_archived_session_removes_dir(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    sid = "a" * 32
    sdir = _mk_session(state, sid, pid=None, mtime_age_days=0)
    ok, msg = _delete_archived_session(state, sid)
    assert ok is True
    assert "Deleted" in msg
    assert not sdir.exists()


def test_delete_refuses_when_session_unknown(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    ok, msg = _delete_archived_session(state, "nope")
    assert ok is False
    assert "not found" in msg.lower()


def test_delete_refuses_live_session(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    sid = "b" * 32
    # Our own pid is guaranteed live.
    sdir = _mk_session(state, sid, pid=os.getpid(), mtime_age_days=0)
    ok, msg = _delete_archived_session(state, sid)
    assert ok is False
    assert sdir.exists()
    assert "running" in msg.lower()


def test_cleanup_removes_old_and_keeps_recent(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    old = _mk_session(state, "o" * 32, pid=None, mtime_age_days=60)
    mid = _mk_session(state, "m" * 32, pid=None, mtime_age_days=45)
    new = _mk_session(state, "n" * 32, pid=None, mtime_age_days=1)
    removed, skipped = _cleanup_archived_sessions(state, max_age_days=30)
    assert removed == 2
    assert skipped == 0
    assert not old.exists()
    assert not mid.exists()
    assert new.exists()


def test_cleanup_skips_live_sessions(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    live = _mk_session(state, "l" * 32, pid=os.getpid(), mtime_age_days=60)
    dead = _mk_session(state, "d" * 32, pid=None, mtime_age_days=60)
    removed, skipped = _cleanup_archived_sessions(state, max_age_days=30)
    assert removed == 1
    assert skipped == 1
    assert live.exists()
    assert not dead.exists()
