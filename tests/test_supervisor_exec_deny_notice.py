"""The exec-deny feedback must fire for BOTH deny paths:
- supervisor-queried denials (_on_query, kind=query)
- shim-enforced static command blocks (_on_event, kind=policy_hit)

The second path was previously uncovered: static blocks like chmod/curl/rm
are decided locally by the guard and logged as kind=policy_hit without a
supervisor query, so they bypassed the _on_query notice hook.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ara import exec_deny_notices as edn
from ara.audit import JsonlWriter
from ara.session import create_session
from ara.supervisor import Supervisor
from ara.walls import Walls


def _sup(state_root: Path) -> Supervisor:
    session = create_session(state_root=state_root)
    return Supervisor(
        session=session,
        walls=Walls(path_rules=()),
        server=None,  # type: ignore[arg-type]
        events=JsonlWriter(session.events_path),
        policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
        state_root=state_root,
    )


def test_shim_policy_hit_exec_deny_queues_notice(tmp_path: Path) -> None:
    state_root = tmp_path / ".ara"
    sup = _sup(state_root)
    # shim-enforced static command block (the chmod case) — kind=policy_hit,
    # command carried in `path`, no supervisor query.
    asyncio.run(sup._on_event({
        "type": "event",
        "kind": "policy_hit",
        "op": "exec.spawn",
        "verdict": "deny",
        "reason": "DENY_COMMAND_BLOCKED",
        "rule_id": "r92",
        "path": "/usr/bin/chmod",
        "source": "libc-shim",
        "pid": 123,
    }))
    got = edn.drain(state_root)
    assert len(got) == 1
    assert got[0]["rule_id"] == "r92"
    assert "chmod" in got[0]["cmd_summary"]


def test_shim_policy_hit_non_deny_does_not_queue(tmp_path: Path) -> None:
    state_root = tmp_path / ".ara"
    sup = _sup(state_root)
    asyncio.run(sup._on_event({
        "type": "event", "kind": "policy_hit", "op": "exec.spawn",
        "verdict": "allow", "path": "/usr/bin/cat", "pid": 1,
    }))
    # non-exec policy_hit deny also must not queue an exec-deny notice
    asyncio.run(sup._on_event({
        "type": "event", "kind": "policy_hit", "op": "net.connect",
        "verdict": "deny", "path": "10.0.0.1:443", "pid": 1,
    }))
    assert edn.drain(state_root) == []
