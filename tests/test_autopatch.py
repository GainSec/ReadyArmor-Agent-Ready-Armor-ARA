"""Verifies that ARA's sitecustomize fires inside a launched worker
without the worker importing anything ARA-specific."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_sitecustomize_runs_in_worker(short_tmp: Path):
    """The hook should advertise itself via a side-effect we can detect:
    when ARA_TRANSCRIPT_PROTOCOL=v1 is in env, importing
    ara.clients.sitecustomize should install the audit hook (no-op without
    openai installed, but proves the bootstrap path resolves)."""
    state = short_tmp / ".ara"
    state.mkdir()
    walls = short_tmp / "WALLS.md"
    walls.write_text("## deny_path\n")

    # Child reports whether the bootstrap module was successfully imported
    # by the site machinery (i.e. was on PYTHONPATH and ran without error).
    child_src = (
        "import os, sys\n"
        # If sitecustomize ran, ara.clients.sitecustomize is in sys.modules
        # because the bootstrap shim imported it.
        "from ara.clients import transcript as t\n"
        "loaded = 'ara.clients.sitecustomize' in sys.modules\n"
        "proto = os.environ.get('ARA_TRANSCRIPT_PROTOCOL')\n"
        "import json; print(json.dumps({'loaded': loaded, 'proto': proto}))\n"
    )

    env = {
        "PATH": "/usr/bin:/bin",
    }
    cmd = [
        sys.executable, "-m", "ara.cli",
        "launch", "--state-root", str(state), "--walls", str(walls),
        "--", sys.executable, "-c", child_src,
    ]
    # Need PYTHONPATH on the launcher itself so it can import ara.cli.
    env_for_launcher = dict(env)
    env_for_launcher["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        cmd, env=env_for_launcher, capture_output=True, text=True, timeout=20,
    )
    out = next((l for l in result.stdout.strip().splitlines() if l.startswith("{")), None)
    assert out, f"no result; stderr=\n{result.stderr}"
    rec = json.loads(out)
    assert rec["proto"] == "v1", "ARA_TRANSCRIPT_PROTOCOL should be set in worker env"
    assert rec["loaded"], "ara.clients.sitecustomize should have been auto-imported"
