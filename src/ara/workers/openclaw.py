"""OpenClaw worker adapter.

Translates ARA's worker-agnostic concepts (profile, isolation, WALLS) into
whatever environment and command shape OpenClaw happens to need. OpenClaw
has no idea this adapter exists — it just receives an env and argv.

The adapter owns:
- Where OpenClaw's state lives for a given ARA profile
  (``~/.openclaw-ara-<profile>/``, namespaced to make ARA's ownership clear).
- The env vars OpenClaw honors for that isolation
  (``OPENCLAW_STATE_DIR``, ``OPENCLAW_CONFIG_PATH``).
- Disabling OpenClaw's Docker sandbox (``OPENCLAW_SANDBOX=0``).
- Locating the OpenClaw binary on PATH.

It deliberately does not pass OpenClaw's own ``--profile`` flag; ARA owns
the profile concept, not OpenClaw.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional


DEFAULT_PROFILE = "default"


def state_dir_for(profile: str) -> Path:
    if not profile:
        profile = DEFAULT_PROFILE
    return Path.home() / f".openclaw-ara-{profile}"


def env_for(profile: str, *, contained: bool = False) -> dict[str, str]:
    state = state_dir_for(profile)
    # With ``--contain`` on, ``containment.make_env`` clobbers
    # OPENCLAW_STATE_DIR to point at the per-session contained dir. Skip
    # creating the global ``~/.openclaw-ara-<profile>/`` in that case —
    # it would just sit empty and confuse operators looking for state.
    if not contained:
        state.mkdir(parents=True, exist_ok=True)
    config_path = state / "openclaw.json"
    return {
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_CONFIG_PATH": str(config_path),
        "OPENCLAW_SANDBOX": "0",
        # OpenClaw's gateway blocks the event loop for ~20-30s during
        # bundled-module ingest before heartbeats can fire. Loosen the
        # watchdog for this worker only — other runtimes keep the tight
        # 10s default.
        "ARA_HEARTBEAT_TIMEOUT_S": "60",
    }


def resolve_binary(override: Optional[str] = None) -> str:
    if override:
        return override
    found = shutil.which("openclaw")
    if not found:
        raise RuntimeError(
            "could not find `openclaw` on PATH; set ARA_OPENCLAW_BIN to override"
        )
    return found


def build_command(
    profile: str,
    openclaw_args: list[str],
    *,
    binary_override: Optional[str] = None,
    contained: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Return (argv, extra_env) for launching OpenClaw under ARA profile.

    The caller (supervisor launch path) merges ``extra_env`` into its own
    guard-env before spawning. OpenClaw sees the merged env and has no
    awareness of the profile concept; to OpenClaw it's just a state dir.
    """
    cmd = [resolve_binary(binary_override), *openclaw_args]
    return cmd, env_for(profile, contained=contained)
