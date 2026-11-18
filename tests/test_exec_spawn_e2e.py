from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy policy evaluator retired — see test_bulwark*.py")


import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from ara.supervisor import Supervisor
from ara.walls import parse

GUARD_MJS = Path(__file__).resolve().parent.parent / "src" / "guard" / "index.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_spawn_of_walled_command_is_blocked(short_tmp: Path):
    walls = parse("## deny_command\nls\n", home=short_tmp)

    result_file = short_tmp / "result.txt"
    script = (
        "const cp = require('node:child_process');"
        "const fs = require('node:fs');"
        "const child = cp.spawn('ls', ['/tmp']);"
        "let out = '';"
        "child.stdout.on('data', (c) => { out += c; });"
        "child.on('error', (err) => { "
        f"  fs.writeFileSync({json.dumps(str(result_file))}, 'ERROR:' + err.code + ':' + err.message);"
        "  process.exit(0);"
        "});"
        "child.on('exit', (code) => {"
        "  if (out) {"
        f"    fs.appendFileSync({json.dumps(str(result_file))}, 'STDOUT:' + out.substring(0, 50));"
        "  }"
        "  process.exit(code || 0);"
        "});"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
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
            hits = []
            hits_path = sup.session.policy_hits_path
            if hits_path.exists():
                hits = [
                    json.loads(line) for line in hits_path.read_text().splitlines() if line
                ]
            return hits
        finally:
            await sup.stop()

    hits = asyncio.run(scenario())
    content = result_file.read_text()
    assert content.startswith("ERROR:"), f"expected block error, got {content!r}"
    assert "DENY_COMMAND_BLOCKED" in content or "ARA_DENY" in content
    assert "STDOUT:" not in content, "no command output should have leaked"

    deny_hits = [
        h for h in hits
        if h.get("op") == "exec.spawn" and h.get("verdict") == "deny"
    ]
    assert deny_hits, f"no exec.spawn deny in policy_hits: {hits}"
    assert deny_hits[0]["rule_id"] == "deny_command:1"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_spawn_of_allowed_command_succeeds(short_tmp: Path):
    walls = parse("## deny_command\nls\n", home=short_tmp)

    result_file = short_tmp / "result.txt"
    script = (
        "const cp = require('node:child_process');"
        "const fs = require('node:fs');"
        # echo is allowed.
        "const child = cp.spawn('echo', ['hello-from-echo']);"
        "let out = '';"
        "child.stdout.on('data', (c) => { out += c; });"
        "child.on('exit', (code) => {"
        f"  fs.writeFileSync({json.dumps(str(result_file))}, 'OUT:' + out.trim());"
        "  process.exit(code || 0);"
        "});"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", "-e", script,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
        finally:
            await sup.stop()

    asyncio.run(scenario())
    content = result_file.read_text()
    assert content == "OUT:hello-from-echo", f"got {content!r}"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_exec_callback_receives_deny_error(short_tmp: Path):
    walls = parse("## deny_command\nls\n", home=short_tmp)

    result_file = short_tmp / "result.txt"
    script = (
        "const cp = require('node:child_process');"
        "const fs = require('node:fs');"
        "cp.exec('ls /tmp', (err, stdout, stderr) => {"
        f"  fs.writeFileSync({json.dumps(str(result_file))},"
        "    err ? 'ERROR:' + (err.code || err.message) : 'OUT:' + stdout);"
        "  process.exit(0);"
        "});"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", "-e", script,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
        finally:
            await sup.stop()

    asyncio.run(scenario())
    content = result_file.read_text()
    assert content.startswith("ERROR:"), f"got {content!r}"
    assert "DENY_COMMAND_BLOCKED" in content or "ARA_DENY" in content
