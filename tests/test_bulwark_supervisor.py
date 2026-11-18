"""Bulwark enforcement at the supervisor level.

Proves that a bulwark.md deny rule short-circuits the legacy WALLS path
when a matching guard query arrives, and that hot-reload picks up edits.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ara import task_session
from ara.supervisor import Supervisor


_DENY_FS = """\
## global
deny fs /forbidden/**
"""

_DENY_NET = """\
## global
deny net *.evil.com
"""

_DENY_EXEC = """\
## global
deny exec curl
"""


def _write_task_loadout(state: Path) -> None:
    (state / "armors" / "blacksmith" / "absolutes").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "absolutes" / "default.md").write_text("armor\n")
    (state / "weapons" / "w-task" / "chunks").mkdir(parents=True)
    (state / "weapons" / "w-task" / "weapon.json").write_text("{}")
    (state / "weapons" / "w-task" / "seal.json").write_text("{}")
    (state / "armors" / "blacksmith" / "trims" / "trim-a").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state / "armors" / "blacksmith" / "temper" / "proposals").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "temper" / "proposals" / "temp-a.json").write_text("{}")


@pytest.mark.asyncio
async def test_bulwark_fs_deny_short_circuits(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(_DENY_FS)

    sup = await Supervisor.start(state_root=state)
    try:
        # Wait up to 2s for the bulwark watcher to pick up the file.
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        assert sup.bulwark.rules, "bulwark should have loaded /forbidden rule"

        # Synthetic guard query for a denied path.
        msg = {"type": "query", "seq": 1, "op": "fs.read", "path": "/forbidden/x"}
        out = await sup._on_query(msg)
        assert out["verdict"] == "deny"
        assert "BULWARK_DENY" in out["reason"]

        # A non-matching path falls through to walls (empty) → allow.
        msg2 = {"type": "query", "seq": 2, "op": "fs.read", "path": "/allowed/x"}
        out2 = await sup._on_query(msg2)
        assert out2["verdict"] == "allow"
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_bulwark_net_deny(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(_DENY_NET)

    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        msg = {
            "type": "query", "seq": 1, "op": "net.connect",
            "host": "api.evil.com", "port": 443,
        }
        out = await sup._on_query(msg)
        assert out["verdict"] == "deny"
        assert "BULWARK_DENY" in out["reason"]
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_bulwark_net_deny_still_applies_when_task_commit_required(
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
):
    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(_DENY_NET)
    _write_task_loadout(state)
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    assert run.record_action("exec.spawn", {"command": "python3"}).allowed is True
    monkeypatch.setenv("ARA_TASK_RUN_ID", run.task_id)

    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        msg = {
            "type": "query", "seq": 1, "op": "net.connect",
            "host": "api.evil.com", "port": 443,
        }
        out = await sup._on_query(msg)
        assert out["verdict"] == "deny"
        assert "BULWARK_DENY" in out["reason"]
        loaded = task_session.TaskRunSession.load(state, run.task_id)
        assert loaded.actions_since_commit == 1
        assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_bulwark_exec_deny_matches_resolved_binary(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(_DENY_EXEC)

    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        msg = {
            "type": "query", "seq": 1, "op": "exec.spawn",
            "command": "/usr/bin/curl", "args": ["https://example.test"],
        }
        out = await sup._on_query(msg)
        assert out["verdict"] == "deny"
        assert "BULWARK_DENY" in out["reason"]
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_bulwark_exec_rules_project_to_sync_deny_command(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(_DENY_EXEC)

    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        payload = sup._sync_rules_payload()
        assert {"pattern": "curl", "rule_id": "r1"} in payload["deny_command"]
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_guard_env_enables_task_channel_gate_for_armory_loop(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
):
    state = short_tmp / ".ara"
    state.mkdir()
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    monkeypatch.delenv("ARA_TASK_CHANNEL_EXEC_BUDGET", raising=False)
    sup = await Supervisor.start(state_root=state)
    try:
        env = sup.guard_env(Path("/tmp/guard.mjs"))
        assert env["ARA_TASK_CHANNEL_REQUIRED"] == "1"
        assert env["ARA_SESSION_ROOT"] == str(sup.session.root)
        assert env["ARA_TASK_CHANNEL_EXEC_BUDGET"] == "12"
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_guard_env_respects_explicit_task_channel_budget(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
):
    state = short_tmp / ".ara"
    state.mkdir()
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    monkeypatch.setenv("ARA_TASK_CHANNEL_EXEC_BUDGET", "7")
    sup = await Supervisor.start(state_root=state)
    try:
        env = sup.guard_env(Path("/tmp/guard.mjs"))
        assert env["ARA_TASK_CHANNEL_REQUIRED"] == "1"
        assert env["ARA_TASK_CHANNEL_EXEC_BUDGET"] == "7"
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_oc_tool_call_deny(short_tmp: Path):
    """oc.tool_call handshake: supervisor evaluates oc:<tool> rules."""
    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(
        "## global\ndeny oc:bash regex:rm\\s+-rf\n"
    )
    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        msg = {
            "type": "query", "seq": 1, "op": "oc.tool_call",
            "tool": "bash", "target": "rm -rf /tmp/oops",
        }
        out = await sup._on_query(msg)
        assert out["verdict"] == "deny"
        assert "BULWARK_DENY" in out["reason"]

        # A different tool slot should fall through (no rule matches).
        msg2 = {
            "type": "query", "seq": 2, "op": "oc.tool_call",
            "tool": "read", "target": "/etc/hosts",
        }
        out2 = await sup._on_query(msg2)
        assert out2["verdict"] == "allow"
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_oc_family_catchall(short_tmp: Path):
    """Bare `oc` layer matches any tool."""
    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(
        "## global\ndeny oc regex:secret\n"
    )
    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        for tool in ("bash", "write", "webfetch"):
            msg = {
                "type": "query", "seq": 1, "op": "oc.tool_call",
                "tool": tool, "target": "leak the secret key",
            }
            out = await sup._on_query(msg)
            assert out["verdict"] == "deny", tool
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_bulwark_hot_reload(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    bw_file = state / "bulwark.md"
    bw_file.write_text("## global\n# empty\n")

    sup = await Supervisor.start(state_root=state)
    try:
        # Initial: no rules.
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup._bulwark_mtime is not None:
                break
        assert sup.bulwark.rules == ()

        # Edit and bump mtime forward; watcher should re-parse.
        import os, time as _t
        bw_file.write_text(_DENY_FS)
        future = _t.time() + 5
        os.utime(bw_file, (future, future))

        for _ in range(30):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        assert sup.bulwark.rules, "hot reload should have picked up the edit"
        msg = {"type": "query", "seq": 1, "op": "fs.read", "path": "/forbidden/y"}
        out = await sup._on_query(msg)
        assert out["verdict"] == "deny"
    finally:
        await sup.stop()


_DENY_EXEC_REGEX = (
    "## global\n"
    r"deny exec regex:(?i)(\bwrite_flash\b|factory[_-]?reset|\bmkfs\b|\breboot\b)"
    "\n"
)


@pytest.mark.asyncio
async def test_exec_deny_queues_agent_notice(short_tmp: Path):
    """A guard exec.spawn deny queues a one-shot notice naming the matched
    term, for the MITM proxy to inject on the agent's next turn."""
    from ara import exec_deny_notices as edn

    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(_DENY_EXEC_REGEX)

    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        # Agent directory-scan wordlist in a bash -c command; only "/reboot"
        # trips the destructive-op guard.
        msg = {
            "type": "query", "seq": 1, "op": "exec.spawn",
            "command": "/usr/bin/bash",
            "args": ["-c", "for p in /admin /reboot /status; do get $p; done"],
        }
        out = await sup._on_query(msg)
        assert out["verdict"] == "deny"

        notices = edn.drain(state)
        assert len(notices) == 1
        assert notices[0]["matched_token"] == "reboot"
        assert notices[0]["rule_id"] == out["rule_id"]
        msg_txt = edn.compose_message(notices)
        assert "`reboot`" in msg_txt and "skip" in msg_txt.lower()
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_exec_allow_queues_no_notice(short_tmp: Path):
    """An allowed exec.spawn must not queue a notice."""
    from ara import exec_deny_notices as edn

    state = short_tmp / ".ara"
    state.mkdir()
    (state / "bulwark.md").write_text(_DENY_EXEC_REGEX)

    sup = await Supervisor.start(state_root=state)
    try:
        for _ in range(20):
            await asyncio.sleep(0.1)
            if sup.bulwark.rules:
                break
        msg = {
            "type": "query", "seq": 1, "op": "exec.spawn",
            "command": "/usr/bin/curl", "args": ["http://192.168.18.9/status"],
        }
        await sup._on_query(msg)
        assert edn.drain(state) == []
    finally:
        await sup.stop()
