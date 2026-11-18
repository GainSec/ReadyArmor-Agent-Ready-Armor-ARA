from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from ara.supervisor import Supervisor

GUARD_MJS = Path(__file__).resolve().parent.parent / "src" / "guard" / "index.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_process_kill_of_supervisor_is_blocked(short_tmp: Path):
    script = (
        "try {"
        "  process.kill(Number(process.env.ARA_SUPERVISOR_PID), 'SIGTERM');"
        "  process.stdout.write('LEAK');"
        "} catch (err) {"
        "  process.stdout.write('BLOCKED:' + (err.code || err.message));"
        "}"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", "-e", script,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            await asyncio.sleep(0.05)
            events_path = sup.session.events_path
            events = []
            if events_path.exists():
                events = [
                    json.loads(line) for line in events_path.read_text().splitlines() if line
                ]
            return proc.returncode, stdout.decode(), events
        finally:
            await sup.stop()

    rc, stdout, events = asyncio.run(scenario())
    assert rc == 0
    assert stdout.startswith("BLOCKED:"), f"stdout={stdout!r}"
    assert "ARA_KILL_BLOCKED" in stdout
    assert any(e.get("kind") == "kill_blocked" for e in events)


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_http_request_is_logged(short_tmp: Path):
    # No real network; just the call site. The http hook logs before dispatch.
    script = (
        "const http = require('node:http');"
        "const req = http.request({ host: '127.0.0.1', port: 1, path: '/', method: 'GET' });"
        "req.on('error', () => process.exit(0));"
        "req.end();"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", "-e", script,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            await asyncio.sleep(0.05)
            events_path = sup.session.events_path
            events = [
                json.loads(line) for line in events_path.read_text().splitlines() if line
            ]
            return events
        finally:
            await sup.stop()

    events = asyncio.run(scenario())
    http_events = [e for e in events if e.get("kind") == "http_request"]
    assert http_events, f"no http_request logged: {events}"
    assert http_events[0].get("proto") == "http"
    assert http_events[0].get("host") == "127.0.0.1"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_heartbeat_emitted(short_tmp: Path):
    # Keep the process alive long enough for >=2 heartbeats, then exit.
    script = "setTimeout(() => process.exit(0), 2500);"

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", "-e", script,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            await asyncio.sleep(0.1)
            events_path = sup.session.events_path
            events = [
                json.loads(line) for line in events_path.read_text().splitlines() if line
            ]
            return events
        finally:
            await sup.stop()

    events = asyncio.run(scenario())
    heartbeats = [e for e in events if e.get("kind") == "heartbeat"]
    assert len(heartbeats) >= 2, f"expected >=2 heartbeats, got {len(heartbeats)}: {events}"
