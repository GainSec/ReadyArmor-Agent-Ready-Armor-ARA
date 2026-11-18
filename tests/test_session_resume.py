"""Session resumption: adopt an existing session dir, preserve history."""

from __future__ import annotations

from pathlib import Path

import pytest

from ara.session import Session, create_session, find_resumable_sessions


def test_create_then_resume_preserves_id_and_history(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    s1 = create_session(state_root=state)
    (s1.events_path).write_text('{"kind":"turn","ts":1}\n')

    s2 = create_session(state_root=state, resume_id=s1.id)
    assert s2.id == s1.id
    # History is preserved + a session_resumed marker was appended.
    content = s2.events_path.read_text()
    assert '"kind":"turn"' in content
    assert '"kind": "session_resumed"' in content


def test_resume_missing_session_raises(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    with pytest.raises(FileNotFoundError):
        create_session(state_root=state, resume_id="nonexistent")


def test_resume_generates_new_token(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    s1 = create_session(state_root=state)
    s2 = create_session(state_root=state, resume_id=s1.id)
    assert s2.token != s1.token  # new run gets fresh auth


def test_find_resumable_sessions(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    s1 = create_session(state_root=state)
    import time as _t
    _t.sleep(0.01)
    s2 = create_session(state_root=state)
    sessions = find_resumable_sessions(state)
    assert len(sessions) >= 2
    # Newest first.
    assert sessions[0].id == s2.id
    assert sessions[1].id == s1.id


def test_resume_uses_existing_dir_not_new(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    s1 = create_session(state_root=state)
    s2 = create_session(state_root=state, resume_id=s1.id)
    assert s1.root == s2.root
