from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ara.audit import JsonlWriter
from ara.runtime.binder.api import EvidenceBinder
from ara.session import create_session
from ara.supervisor import Supervisor
from ara.walls import Walls


def test_target_url_spawn_event_is_bound_as_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / ".ara"
    session = create_session(state_root=state_root)
    sup = Supervisor(
        session=session,
        walls=Walls(path_rules=()),
        server=None,  # type: ignore[arg-type]
        events=JsonlWriter(session.events_path),
        policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
        state_root=state_root,
    )

    asyncio.run(sup._on_event({
        "type": "event",
        "kind": "spawn",
        "source": "libc-shim",
        "pid": 123,
        "command": "/usr/bin/curl",
        "args": ["curl", "-sI", "https://example.test/admin"],
    }))

    rows = EvidenceBinder(session.root).search("example", k=5)
    assert len(rows) == 1
    rec = rows[0].record
    assert rec.source == "shim:spawn"
    assert rec.source_metadata["command"] == "/usr/bin/curl"
    assert rec.source_metadata["url"] == "https://example.test/admin"
    assert json.loads(EvidenceBinder(session.root).get(rec.id))["kind"] == "spawn"


def test_ara_hermes_launcher_spawn_is_not_target_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / ".ara"
    session = create_session(state_root=state_root)
    sup = Supervisor(
        session=session,
        walls=Walls(path_rules=()),
        server=None,  # type: ignore[arg-type]
        events=JsonlWriter(session.events_path),
        policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
        state_root=state_root,
    )

    prompt = (
        "Target/scope: https://example.test/.\n"
        "Trim stop time: 2026-06-19T22:52:48Z."
    )
    asyncio.run(sup._on_event({
        "type": "event",
        "kind": "spawn",
        "source": "libc-shim",
        "pid": 123,
        "command": "/usr/bin/timeout",
        "args": [
            "timeout", "330", str(tmp_path / ".local/bin/hermes"),
            "--accept-hooks", "--continue", "ara-task-20260619-215321",
            "-z", prompt,
        ],
    }))
    asyncio.run(sup._on_event({
        "type": "event",
        "kind": "spawn",
        "source": "libc-shim",
        "pid": 124,
        "command": str(tmp_path / ".local/bin/hermes"),
        "args": [
            str(tmp_path / ".local/bin/hermes"),
            "--accept-hooks", "--continue", "ara-task-20260619-215321",
            "-z", prompt,
        ],
    }))

    rows = EvidenceBinder(session.root).search("example", k=5)
    assert rows == []


def test_target_url_spawn_output_is_bound_as_searchable_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / ".ara"
    session = create_session(state_root=state_root)
    sup = Supervisor(
        session=session,
        walls=Walls(path_rules=()),
        server=None,  # type: ignore[arg-type]
        events=JsonlWriter(session.events_path),
        policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
        state_root=state_root,
    )

    asyncio.run(sup._on_event({
        "type": "event",
        "kind": "spawn_output",
        "source": "node-child-process",
        "pid": 456,
        "command": "/usr/bin/curl",
        "args": ["-sI", "https://example.test/"],
        "url": "https://example.test/",
        "exit_code": 0,
        "signal": None,
        "stdout_text": "HTTP/2 200\r\nserver: cloudflare\r\n",
        "stderr_text": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }))

    rows = EvidenceBinder(session.root).search("cloudflare", k=5)
    assert len(rows) == 1
    rec = rows[0].record
    assert rec.source == "shim:spawn_output"
    assert rec.source_metadata["command"] == "/usr/bin/curl"
    assert rec.source_metadata["url"] == "https://example.test/"
    assert rec.source_metadata["exit_code"] == 0
    content = json.loads(EvidenceBinder(session.root).get(rec.id))
    assert content["stdout_text"].startswith("HTTP/2 200")


def test_ara_hermes_launcher_output_is_not_target_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / ".ara"
    session = create_session(state_root=state_root)
    sup = Supervisor(
        session=session,
        walls=Walls(path_rules=()),
        server=None,  # type: ignore[arg-type]
        events=JsonlWriter(session.events_path),
        policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
        state_root=state_root,
    )

    asyncio.run(sup._on_event({
        "type": "event",
        "kind": "spawn_output",
        "source": "python-subprocess",
        "pid": 456,
        "command": str(tmp_path / ".local/bin/hermes"),
        "args": [
            str(tmp_path / ".local/bin/hermes"),
            "--continue", "ara-task-20260619-215321",
            "-z", "Target/scope: https://example.test/.",
        ],
        "url": "https://example.test/",
        "exit_code": 0,
        "signal": None,
        "stdout_text": "cycle complete",
        "stderr_text": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }))

    rows = EvidenceBinder(session.root).search("example", k=5)
    assert rows == []
