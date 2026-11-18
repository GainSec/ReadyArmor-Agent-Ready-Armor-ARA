"""End-to-end: spawn Node with --import=guard, supervisor sees hello."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ara.supervisor import Supervisor

GUARD_MJS = Path(__file__).resolve().parent.parent / "src" / "guard" / "index.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


def test_guard_env_exports_state_root_for_mcp_servers(short_tmp: Path):
    async def scenario() -> dict[str, str]:
        state_root = short_tmp / ".ara"
        sup = await Supervisor.start(state_root=state_root)
        try:
            return sup.guard_env(GUARD_MJS)
        finally:
            await sup.stop()

    env = asyncio.run(scenario())

    assert env["ARA_STATE_ROOT"] == str(short_tmp / ".ara")


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_guard_handshake_and_armed_message(short_tmp: Path):
    async def scenario() -> tuple[int, str, dict[int, object]]:
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node",
                "-e",
                "",  # do nothing; guard should arm on --import
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode, stderr.decode(), dict(sup.guards)
        finally:
            await sup.stop()

    rc, stderr, guards = asyncio.run(scenario())

    assert rc == 0, f"node exited {rc}; stderr: {stderr}"
    assert "[ara-guard] armed" in stderr, f"no armed line in stderr: {stderr!r}"
    # Exactly one guard connected, and its pid is recorded.
    assert len(guards) == 1
    (conn,) = guards.values()
    assert conn.runtime.startswith("node v")


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_guard_rejects_bad_token(short_tmp: Path):
    async def scenario() -> tuple[int, str]:
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            env["ARA_SESSION_TOKEN"] = "wrong-token"
            proc = await asyncio.create_subprocess_exec(
                "node",
                "-e",
                "",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode, stderr.decode()
        finally:
            await sup.stop()

    rc, stderr = asyncio.run(scenario())
    assert rc == 99, f"expected rc=99 for rejected handshake; got {rc}"
    assert "rejected handshake" in stderr


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_guard_fails_closed_without_socket(short_tmp: Path):
    # Guard launched without a reachable supervisor should exit, not proceed.
    env = {
        **os.environ,
        "NODE_OPTIONS": f"--import={GUARD_MJS}",
        "ARA_SOCKET": str(short_tmp / "nonexistent.sock"),
        "ARA_SESSION_TOKEN": "any",
        "ARA_SESSION_ID": "any",
    }
    result = subprocess.run(
        ["node", "-e", ""], env=env, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 98
    assert b"failed to connect" in result.stderr


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_guard_requires_env(short_tmp: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARA_")}
    env["NODE_OPTIONS"] = f"--import={GUARD_MJS}"
    result = subprocess.run(
        ["node", "-e", ""], env=env, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 97
    assert b"refusing to arm" in result.stderr
