"""Claude Code worker adapter.

Minimal skeleton analog to ``workers/openclaw.py``. Claude Code is a
markedly smaller integration target than OpenClaw — it doesn't require
disabling a Docker sandbox or widening the heartbeat watchdog — but the
adapter surface is the same so the supervisor + MITM stay
runtime-agnostic.

The adapter owns:
- Where Claude Code's state lives for a given ARA profile
  (``~/.claude-ara-<profile>/``).
- The env the Claude Code CLI honors for that isolation
  (``CLAUDE_HOME`` / ``CLAUDE_CONFIG_PATH``).
- Locating the ``claude`` binary on PATH (with override).

Contained launches have ``HOME`` / ``XDG_*`` rewritten by
``containment.make_env``; this adapter sets ``CLAUDE_HOME`` directly
because the Claude Code CLI reads it before falling back to ``HOME``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional


DEFAULT_PROFILE = "default"


def state_dir_for(profile: str) -> Path:
    if not profile:
        profile = DEFAULT_PROFILE
    return Path.home() / f".claude-ara-{profile}"


def env_for(profile: str, *, contained: bool = False) -> dict[str, str]:
    state = state_dir_for(profile)
    if not contained:
        state.mkdir(parents=True, exist_ok=True)
    config_path = state / "claude.json"
    return {
        "CLAUDE_HOME": str(state),
        "CLAUDE_CONFIG_PATH": str(config_path),
    }


def resolve_binary(override: Optional[str] = None) -> str:
    if override:
        return override
    found = shutil.which("claude")
    if not found:
        raise RuntimeError(
            "could not find `claude` on PATH; set ARA_CLAUDE_BIN to override"
        )
    return found


def build_command(
    profile: str,
    claude_args: list[str],
    *,
    binary_override: Optional[str] = None,
    contained: bool = False,
) -> tuple[list[str], dict[str, str]]:
    cmd = [resolve_binary(binary_override), *claude_args]
    return cmd, env_for(profile, contained=contained)
