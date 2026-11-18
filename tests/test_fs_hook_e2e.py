"""End-to-end fs hook test: real Node process with guard arms, real WALLS rule,
real fs.readFile calls, verify allow + deny + audit trail."""

from __future__ import annotations

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
def test_fs_readfile_async_denies_blocked_path(short_tmp: Path):
    """Blocked path: fs.readFile callback receives ARA_DENY error; policy_hits logged."""
    blocked_dir = short_tmp / "vault"
    blocked_dir.mkdir()
    blocked_file = blocked_dir / "secret.txt"
    blocked_file.write_text("this should never be readable")

    walls = parse(f"## deny_path\n{blocked_dir}/**\n", home=short_tmp)

    script = f"""
const fs = require('node:fs');
fs.readFile({json.dumps(str(blocked_file))}, 'utf8', (err, data) => {{
  if (err) {{
    process.stdout.write('DENY:' + err.code + ':' + err.message + '\\n');
    process.exit(0);
  }}
  process.stdout.write('LEAK:' + data + '\\n');
  process.exit(1);
}});
"""

    async def scenario() -> tuple[int, str, str, list[dict]]:
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node",
                "-e",
                script,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            # Allow audit flushes to settle.
            await asyncio.sleep(0.05)
            hits_path = sup.session.policy_hits_path
            hits = []
            if hits_path.exists():
                hits = [
                    json.loads(line) for line in hits_path.read_text().splitlines() if line
                ]
            return proc.returncode, stdout.decode(), stderr.decode(), hits
        finally:
            await sup.stop()

    rc, stdout, stderr, hits = asyncio.run(scenario())

    assert rc == 0, f"node rc={rc}; stderr={stderr}"
    assert stdout.startswith("DENY:"), f"expected DENY, got stdout={stdout!r}"
    assert "ARA_DENY" in stdout or "DENY_FILE_PATH_BLOCKED" in stdout
    # Audit trail
    assert any(
        h.get("verdict") == "deny" and h.get("op") == "fs.read" for h in hits
    ), f"no deny in policy_hits: {hits}"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_fs_readfile_async_allows_unblocked_path(short_tmp: Path):
    allowed = short_tmp / "public.txt"
    allowed.write_text("hello world")

    walls = parse("## deny_path\n/nonexistent/**\n", home=short_tmp)

    script = f"""
const fs = require('node:fs');
fs.readFile({json.dumps(str(allowed))}, 'utf8', (err, data) => {{
  if (err) {{ process.stderr.write('FAIL:' + err.message); process.exit(1); }}
  process.stdout.write('READ:' + data);
}});
"""

    async def scenario() -> tuple[int, str, str]:
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
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
        finally:
            await sup.stop()

    rc, stdout, stderr = asyncio.run(scenario())
    assert rc == 0, f"node rc={rc}; stderr={stderr}"
    assert stdout == "READ:hello world"


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_wall_holds_even_when_supervisor_gone(short_tmp: Path):
    """Guard evaluates against cached rules, so a walled read still denies
    even if the supervisor crashed mid-session.

    (Allowed reads DO pass through after a supervisor outage — audit events
    are lost for those, but enforcement integrity is preserved.)
    """
    blocked_dir = short_tmp / "vault"
    blocked_dir.mkdir()
    blocked_file = blocked_dir / "secret.txt"
    blocked_file.write_text("no peeking")

    walls = parse(f"## deny_path\n{blocked_dir}/**\n", home=short_tmp)
    ready_flag = short_tmp / "first-done.flag"

    script = f"""
const fs = require('node:fs');
const blocked = {json.dumps(str(blocked_file))};
const flag = {json.dumps(str(ready_flag))};
// First attempt: supervisor alive; should deny.
fs.readFile(blocked, 'utf8', (err1) => {{
  if (!err1) {{ process.stdout.write('LEAK1'); process.exit(1); }}
  fs.writeFileSync(flag, '1');
  setTimeout(() => {{
    // Second attempt: supervisor gone; guard should still deny locally.
    fs.readFile(blocked, 'utf8', (err2) => {{
      if (err2) {{
        process.stdout.write('DENY2:' + (err2.code || err2.message));
        process.exit(0);
      }}
      process.stdout.write('LEAK2');
      process.exit(1);
    }});
  }}, 500);
}});
"""

    async def scenario():
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        env = {**os.environ, **sup.guard_env(GUARD_MJS)}
        proc = await asyncio.create_subprocess_exec(
            "node", "-e", script,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(100):
            if ready_flag.exists():
                break
            await asyncio.sleep(0.02)
        await sup.stop()
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return proc.returncode, stdout.decode()

    rc, stdout = asyncio.run(scenario())
    assert rc == 0, f"rc={rc} stdout={stdout!r}"
    assert stdout.startswith("DENY2:"), f"wall did not hold post-supervisor: {stdout!r}"
    assert "DENY_FILE_PATH_BLOCKED" in stdout or "ARA_DENY" in stdout


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_fs_promises_readfile_denied(short_tmp: Path):
    blocked_dir = short_tmp / "vault"
    blocked_dir.mkdir()
    blocked_file = blocked_dir / "secret.txt"
    blocked_file.write_text("nope")

    walls = parse(f"## deny_path\n{blocked_dir}/**\n", home=short_tmp)

    script = f"""
const fs = require('node:fs/promises');
(async () => {{
  try {{
    const data = await fs.readFile({json.dumps(str(blocked_file))}, 'utf8');
    process.stdout.write('LEAK:' + data);
    process.exit(1);
  }} catch (err) {{
    process.stdout.write('DENY:' + err.message);
    process.exit(0);
  }}
}})();
"""

    async def scenario() -> tuple[int, str]:
        sup = await Supervisor.start(state_root=short_tmp / ".ara", walls=walls)
        try:
            env = {**os.environ, **sup.guard_env(GUARD_MJS)}
            proc = await asyncio.create_subprocess_exec(
                "node",
                "-e",
                script,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode, stdout.decode()
        finally:
            await sup.stop()

    rc, stdout = asyncio.run(scenario())
    assert rc == 0
    assert stdout.startswith("DENY:")
    assert "ARA_DENY" in stdout or "DENY_FILE_PATH_BLOCKED" in stdout
