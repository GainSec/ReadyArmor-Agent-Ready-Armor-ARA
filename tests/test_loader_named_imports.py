"""Verify the ESM module loader catches named imports that property-mutation
on the module namespace cannot reach.

Named imports: `import { spawn } from "node:child_process"` create a live
binding to the module's original export. Reassigning the namespace property
after the fact does nothing to that binding. The loader hook fixes this by
replacing the whole module at resolution time.
"""

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
def test_named_import_readFileSync_is_intercepted(short_tmp: Path):
    blocked_dir = short_tmp / "vault"
    blocked_dir.mkdir()
    blocked_file = blocked_dir / "secret.txt"
    blocked_file.write_text("nope")

    walls = parse(f"## deny_path\n{blocked_dir}/**\n", home=short_tmp)

    # ES module using a named import. This is the case the loader must cover.
    esm_file = short_tmp / "script.mjs"
    esm_file.write_text(
        "import { readFileSync } from 'node:fs';\n"
        f"try {{ const d = readFileSync({json.dumps(str(blocked_file))}, 'utf8'); "
        "  process.stdout.write('LEAK:' + d); process.exit(1); "
        "} catch (err) { "
        "  process.stdout.write('DENY:' + (err.code || err.message)); process.exit(0); "
        "}\n"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", str(esm_file),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode, stdout.decode(), stderr.decode()
        finally:
            await sup.stop()

    rc, stdout, stderr = asyncio.run(scenario())
    assert rc == 0, f"rc={rc} stdout={stdout!r} stderr={stderr!r}"
    assert stdout.startswith("DENY:"), f"stdout={stdout!r}; stderr={stderr!r}"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_named_import_spawn_is_intercepted(short_tmp: Path):
    walls = parse("## deny_command\nls\n", home=short_tmp)

    esm_file = short_tmp / "script.mjs"
    result_file = short_tmp / "result.txt"
    esm_file.write_text(
        "import { spawn } from 'node:child_process';\n"
        "import fs from 'node:fs';\n"
        "const child = spawn('ls', ['/tmp']);\n"
        "let out = '';\n"
        "child.stdout.on('data', (c) => { out += c; });\n"
        "child.on('error', (err) => { "
        f"  fs.writeFileSync({json.dumps(str(result_file))}, 'ERROR:' + (err.code || err.message)); "
        "  process.exit(0); "
        "});\n"
        "child.on('exit', (code) => { "
        "  if (out) { "
        f"    fs.appendFileSync({json.dumps(str(result_file))}, 'STDOUT:' + out.substring(0, 50)); "
        "  } "
        "  process.exit(code || 0); "
        "});\n"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", str(esm_file),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
        finally:
            await sup.stop()

    asyncio.run(scenario())
    content = result_file.read_text()
    assert content.startswith("ERROR:"), f"content={content!r}"
    assert "STDOUT:" not in content, "no stdout should have leaked"
    assert "DENY_COMMAND_BLOCKED" in content or "ARA_DENY" in content


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_named_import_readdirSync_is_intercepted(short_tmp: Path):
    blocked_dir = short_tmp / "vault"
    blocked_dir.mkdir()

    walls = parse(f"## deny_path\n{blocked_dir}\n", home=short_tmp)

    esm_file = short_tmp / "script.mjs"
    esm_file.write_text(
        "import { readdirSync } from 'node:fs';\n"
        f"try {{ readdirSync({json.dumps(str(blocked_dir))}); "
        "  process.stdout.write('LEAK'); process.exit(1); "
        "} catch (err) { "
        "  process.stdout.write('DENY:' + (err.code || err.message)); process.exit(0); "
        "}\n"
    )

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node", str(esm_file),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return stdout.decode()
        finally:
            await sup.stop()

    stdout = asyncio.run(scenario())
    assert stdout.startswith("DENY:"), f"stdout={stdout!r}"
