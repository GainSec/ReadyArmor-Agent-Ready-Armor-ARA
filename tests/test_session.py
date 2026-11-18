import json
from pathlib import Path

from ara.session import create_session


def test_create_session_writes_metadata(tmp_path: Path):
    session = create_session(state_root=tmp_path / ".ara")

    assert session.root.is_dir()
    assert session.root.parent.name == "sessions"
    assert session.metadata_path.is_file()

    meta = json.loads(session.metadata_path.read_text())
    assert meta["id"] == session.id
    assert meta["token"] == session.token
    assert meta["pid"] == session.pid
    assert meta["started_at"] == session.started_at
    assert meta["socket"].endswith("guard.sock")


def test_sessions_are_unique(tmp_path: Path):
    a = create_session(state_root=tmp_path / ".ara")
    b = create_session(state_root=tmp_path / ".ara")
    assert a.id != b.id
    assert a.token != b.token
    assert a.root != b.root


def test_session_token_has_entropy(tmp_path: Path):
    session = create_session(state_root=tmp_path / ".ara")
    # 16 bytes hex-encoded
    assert len(session.token) == 32
    int(session.token, 16)  # valid hex


def test_expected_paths(tmp_path: Path):
    session = create_session(state_root=tmp_path / ".ara")
    for attr in (
        "socket_path",
        "events_path",
        "policy_hits_path",
        "metadata_path",
    ):
        assert str(getattr(session, attr)).startswith(str(session.root))
