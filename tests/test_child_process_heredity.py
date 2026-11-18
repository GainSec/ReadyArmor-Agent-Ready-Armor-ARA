from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from ara.runtime.binder.api import EvidenceBinder
from ara.supervisor import Supervisor
from ara.walls import parse

GUARD_MJS = Path(__file__).resolve().parent.parent / "src" / "guard" / "index.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


async def _spawn_node(script: str, env: dict[str, str]) -> tuple[int | None, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "node",
        "-e",
        script,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    return proc.returncode, stdout.decode(), stderr.decode()


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_spawned_node_child_is_also_guarded(short_tmp: Path):
    """Parent node spawns child node; child must hello to same supervisor."""
    marker = short_tmp / "child.marker"
    child_script = short_tmp / "child.js"
    child_script.write_text(
        f"require('node:fs').writeFileSync({json.dumps(str(marker))}, 'ok');\n"
    )
    parent_script = (
        "const cp = require('node:child_process');"
        f"const child = cp.spawn(process.execPath, [{json.dumps(str(child_script))}], "
        "{ stdio: 'inherit' });"
        "child.on('exit', (code) => process.exit(code || 0));"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            rc, _, stderr = await _spawn_node(parent_script, env)
            return rc, stderr, dict(sup.guards)
        finally:
            await sup.stop()

    rc, stderr, guards = asyncio.run(scenario())
    assert rc == 0, f"rc={rc}; stderr={stderr}"
    assert marker.exists()
    armed_count = stderr.count("[ara-guard] armed")
    assert armed_count == 2, f"expected 2 armed lines, got {armed_count}; stderr={stderr}"
    assert len(guards) == 2, f"expected 2 guards, got {guards}"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_subprocess_read_is_walled(short_tmp: Path):
    """A child process spawned from parent must also be blocked by WALLS."""
    blocked_dir = short_tmp / "vault"
    blocked_dir.mkdir()
    blocked_file = blocked_dir / "secret.txt"
    blocked_file.write_text("nope")

    walls = parse(f"## deny_path\n{blocked_dir}/**\n", home=short_tmp)

    result_file = short_tmp / "result.txt"
    child_code = (
        "const fs = require('node:fs');"
        f"fs.readFile({json.dumps(str(blocked_file))}, 'utf8', (err, data) => {{"
        f"  fs.writeFileSync({json.dumps(str(result_file))}, err ? ('DENY:' + (err.code || err.message)) : ('LEAK:' + data));"
        "  process.exit(err ? 0 : 1);"
        "});"
    )
    parent_script = (
        "const cp = require('node:child_process');"
        f"const child = cp.spawn(process.execPath, ['-e', {json.dumps(child_code)}], "
        "{ stdio: 'inherit' });"
        "child.on('exit', (code) => process.exit(code || 0));"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            rc, _, stderr = await _spawn_node(parent_script, env)
            return rc, stderr
        finally:
            await sup.stop()

    rc, stderr = asyncio.run(scenario())
    assert rc == 0, f"rc={rc}; stderr={stderr}"
    content = result_file.read_text()
    assert content.startswith("DENY:"), f"content={content!r}"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_sanitized_env_is_overwritten(short_tmp: Path):
    """Even if parent sanitizes the env before spawn, heredity is reinstated."""
    marker = short_tmp / "grandchild.marker"
    grandchild_script = short_tmp / "grandchild.js"
    grandchild_script.write_text(
        f"require('node:fs').writeFileSync({json.dumps(str(marker))}, 'ok');\n"
    )
    parent_script = (
        "const cp = require('node:child_process');"
        "const sanitized = { ...process.env };"
        "delete sanitized.NODE_OPTIONS;"
        "delete sanitized.ARA_SOCKET;"
        "delete sanitized.ARA_SESSION_TOKEN;"
        "delete sanitized.ARA_SESSION_ID;"
        f"const child = cp.spawn(process.execPath, [{json.dumps(str(grandchild_script))}], "
        "{ env: sanitized, stdio: 'inherit' });"
        "child.on('exit', (code) => process.exit(code || 0));"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            rc, _, stderr = await _spawn_node(parent_script, env)
            return rc, stderr, dict(sup.guards)
        finally:
            await sup.stop()

    rc, stderr, guards = asyncio.run(scenario())
    assert rc == 0, f"rc={rc}; stderr={stderr}"
    assert marker.exists(), "grandchild did not actually run"
    assert stderr.count("[ara-guard] armed") == 2
    assert len(guards) == 2


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_target_url_child_output_is_bound_as_evidence(short_tmp: Path):
    """Node child-process output with a target URL should land in the binder."""
    child_script = short_tmp / "target-output.js"
    child_script.write_text("console.log('HTTP/2 200\\nserver: cloudflare');\n")
    parent_script = (
        "const cp = require('node:child_process');"
        "const child = cp.spawn(process.execPath, "
        f"[{json.dumps(str(child_script))}, 'https://example.test/']);"
        "child.on('exit', (code) => process.exit(code || 0));"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara")
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            rc, _, stderr = await _spawn_node(parent_script, env)
            await asyncio.sleep(0.1)
            return rc, stderr, sup.session.root
        finally:
            await sup.stop()

    rc, stderr, session_root = asyncio.run(scenario())
    assert rc == 0, f"rc={rc}; stderr={stderr}"
    rows = EvidenceBinder(session_root).search("cloudflare", k=5)
    assert rows, "expected target subprocess stdout in binder evidence"
    rec = rows[0].record
    assert rec.source == "shim:spawn_output"
    assert rec.source_metadata["url"] == "https://example.test/"
