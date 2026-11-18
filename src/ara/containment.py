"""Session-scoped containment for worker processes (OpenClaw-focused).

Goal: everything the worker reads or writes lives under the session root
unless we deliberately mount it in. Same axes as a chroot, done with env
remapping instead of kernel features — cheap, userspace, reversible.

The three things this module provides:

- ``build_contained_env(session_root, *, template_home=None)`` returns a dict
  of env overrides (HOME, TMPDIR, XDG_*, OPENCLAW_STATE_DIR, CODEX_HOME, the
  npm + node caches, shell history locations). The caller merges this on top
  of the guard/launch env.

- ``bootstrap_home(session_home, real_home, *, include=...)`` copies a
  controlled set of files from the real HOME into the contained HOME so
  OAuth / known-good tokens survive. Anything not explicitly included is
  absent — that's the point.

- ``instrumentation_env()`` returns the "tell me everything" env: NODE_DEBUG,
  DEBUG patterns, ARA_HOOK_DEBUG. Opt-in, because it's noisy.

Nothing here runs unless the caller (cli.py) asks for it. No existing launch
path changes behavior by accident.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import debug_log


# Files we copy into the contained HOME so the worker can actually reach a
# working model backend. Kept minimal on purpose — every entry is a surface
# that could leak state. Path is relative to real HOME.
DEFAULT_HOME_TEMPLATE: tuple[str, ...] = (
    ".codex/auth.json",
    ".codex/config.toml",
    ".codex/config.yaml",   # tolerated; present on some installs
    ".hermes/config.yaml",
    ".hermes/.env",
    # OpenClaw state as a whole — gateway, identity, devices, credentials,
    # channel configs. Trailing '/' means "copy the directory recursively"
    # (see bootstrap_home). Two-file allowlist wasn't enough: the gateway
    # needs at least identity/ and devices/ to come up without re-pairing.
    ".openclaw/",
)


REPO_ROOT = Path(__file__).resolve().parents[2]
ARA_SRC_DIR = REPO_ROOT / "src"
ARMORY_TASK_PROFILE_SEED = (
    ARA_SRC_DIR / "ara" / "seed" / "hermes-profiles" / "ara-task"
)
FORGE_PROFILE_SEED = (
    ARA_SRC_DIR / "ara" / "seed" / "hermes-profiles" / "forge"
)


def _mkdirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def contained_root(session_root: Path) -> Path:
    """Returns the ``<session>/contain/`` directory (created on access)."""
    root = session_root / "contain"
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_contained_env(
    session_root: Path,
    *,
    profile: str = "default",
    template_home: Path | None = None,
) -> dict[str, str]:
    """Return env overrides that redirect HOME, caches, and tool-specific
    state into ``<session>/contain/``.

    Side-effect: creates the sandbox directory tree. The caller is expected
    to pass this dict into the launch env merge; we don't mutate
    ``os.environ`` ourselves.
    """
    root = contained_root(session_root)
    home = root / "home"
    tmp = root / "tmp"
    xdg_config = root / "xdg" / "config"
    xdg_cache = root / "xdg" / "cache"
    xdg_data = root / "xdg" / "data"
    xdg_state = root / "xdg" / "state"
    npm_cache = root / "cache" / "npm"
    node_repl = root / "state" / "node-repl-history"
    # Point OpenClaw state at ``<home>/.openclaw`` so the adapter's env
    # vars and the bootstrap's copy target agree — otherwise Discord (and
    # everything else in openclaw.json) gets ignored because env beats HOME.
    openclaw_state = home / ".openclaw"
    codex_home = home / ".codex"
    hermes_home = home / ".hermes"
    _mkdirs(
        home, tmp, xdg_config, xdg_cache, xdg_data, xdg_state,
        npm_cache, openclaw_state, codex_home, hermes_home,
    )
    # Touch the repl history file so Node doesn't try to create it in the
    # real HOME on first spawn.
    node_repl.parent.mkdir(parents=True, exist_ok=True)
    if not node_repl.exists():
        node_repl.touch()

    copied = bootstrap_home(home, template_home or Path.home())
    if profile == "armory-task":
        hermes_home = build_armory_task_hermes_home(home)
    elif profile == "forge":
        # Forge/pack agents must use the ara-forge MCP bridge, not whatever
        # the operator's live ~/.hermes happens to point at (bootstrap_home
        # copies that in, and it may be a stale temper config). Install the
        # forge profile so the contained agent gets the forge tools.
        hermes_home = _install_hermes_profile(home, FORGE_PROFILE_SEED)
    # NOTE: ARA does not modify openclaw config files on disk — even in
    # the contained HOME copy. All gateway-WS-tap and exec-approvals
    # redirection happens at the LD_PRELOAD libc shim layer
    # (``libara_shim.so``) and via env vars (``OPENCLAW_*``,
    # ``ARA_GATEWAY_WS_*``). The shim rewrites ``connect()`` sockaddrs
    # before the real syscall, so the agent's view of the config is
    # unchanged but its actual sockets land on ARA's tap. Touching
    # openclaw config is forbidden by design — newer openclaw versions
    # validate strictly, and audit clarity demands the agent's config
    # remain authoritative.

    env = {
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "npm_config_cache": str(npm_cache),
        "NODE_REPL_HISTORY": str(node_repl),
        # OpenClaw picks these up via its env_for() adapter, but we override
        # here so containment wins when both are set.
        "OPENCLAW_STATE_DIR": str(openclaw_state),
        "OPENCLAW_CONFIG_PATH": str(openclaw_state / "openclaw.json"),
        "OPENCLAW_SANDBOX": "0",
        "CODEX_HOME": str(codex_home),
        "HERMES_HOME": str(hermes_home),
        # Mark the process so code that cares can tell it's running under
        # ARA containment (useful for stderr prefixes, log tagging).
        "ARA_CONTAINED": "1",
        "ARA_CONTAIN_ROOT": str(root),
        "ARA_CONTAIN_PROFILE": profile,
    }
    debug_log.emit(
        "containment", "env.built",
        state_root=session_root.parents[1] if len(session_root.parents) > 1 else None,
        session_id=session_root.name,
        profile=profile,
        session_root=str(session_root),
        home=str(home),
        hermes_home=str(hermes_home),
        openclaw_state=str(openclaw_state),
        copied_home_entries=copied,
        env=env,
    )
    return env


def _install_hermes_profile(session_home: Path, seed_dir: Path) -> Path:
    """Install a named Hermes profile into the contained ``$HOME/.hermes``,
    replacing config.yaml / SOUL.md with the seed's while preserving the
    bootstrapped ``.env`` credentials. Hermes reads ``$HOME/.hermes`` directly
    (it does not consistently honor a separate ``HERMES_HOME``), so the profile
    must land there to take effect."""
    dst = session_home / ".hermes"
    dst.mkdir(parents=True, exist_ok=True)
    if not seed_dir.is_dir():
        raise FileNotFoundError(f"missing Hermes profile seed: {seed_dir}")
    generic_env = dst / ".env"
    env_text = generic_env.read_text() if generic_env.is_file() else None
    for src in seed_dir.iterdir():
        if not src.is_file():
            continue
        target = dst / src.name
        if src.suffix in {".yaml", ".yml", ".md"}:
            text = src.read_text().replace("__ARA_SRC_DIR__", str(ARA_SRC_DIR))
            target.write_text(text)
        else:
            shutil.copy2(src, target)
    if env_text is not None:
        (dst / ".env").write_text(env_text)
    return dst


def build_armory_task_hermes_home(session_home: Path) -> Path:
    """Create the Hermes profile used by contained Armory task runs.

    The generic Hermes config can still be bootstrapped for credentials, but
    ARA task identity must replace the contained ``$HOME/.hermes`` profile.
    Hermes installations do not consistently honor a separate ``HERMES_HOME``
    path, so task mode writes the controlled profile where Hermes naturally
    looks while preserving the .env secrets it needs.
    """
    dst = _install_hermes_profile(session_home, ARMORY_TASK_PROFILE_SEED)
    debug_log.emit(
        "containment", "armory_task_hermes_profile.seeded",
        state_root=None,
        session_home=str(session_home),
        hermes_home=str(dst),
        seed=str(ARMORY_TASK_PROFILE_SEED),
        files=[p.name for p in dst.iterdir() if p.is_file()],
    )
    return dst


def bootstrap_home(
    session_home: Path,
    real_home: Path,
    *,
    include: tuple[str, ...] = DEFAULT_HOME_TEMPLATE,
) -> list[str]:
    """Copy an allowlist of files/dirs from the real HOME into the contained
    HOME. A trailing ``/`` on an entry means "copy the directory tree"; any
    other entry is a single file. Returns the list of relative paths that
    were actually copied (files only — directory entries contribute the
    files inside). Missing sources are skipped silently. First-launch OAuth
    survival depends on this — without it the worker hits an empty
    ~/.codex / ~/.openclaw and asks the operator to re-auth / re-pair.
    """
    copied: list[str] = []
    for rel in include:
        is_dir_entry = rel.endswith("/")
        src = real_home / rel.rstrip("/")
        if is_dir_entry:
            if not src.is_dir():
                continue
            # Directory tree copy. Symlinks preserved so identity / credential
            # symlinks (if any) still resolve. ignore_dangling_symlinks keeps
            # us from failing on a stale link during the sweep.
            for file in src.rglob("*"):
                if not file.is_file() or file.is_symlink() is False and not file.is_file():
                    # keep normal regular-file copies only; skip sockets/fifos
                    pass
                if file.is_file():
                    rel_inside = file.relative_to(real_home)
                    dst = session_home / rel_inside
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(file, dst, follow_symlinks=True)
                        copied.append(str(rel_inside))
                    except OSError:
                        continue
            continue
        if not src.is_file():
            continue
        dst = session_home / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied.append(rel)
        except OSError:
            continue
    return copied


# NOTE: previous versions of this module shipped three functions that
# directly modified the contained ``~/.openclaw/openclaw.json`` —
# ``_wire_gateway_ws_redirect`` (rewrote gateway.port to ARA's WS tap),
# ``_wire_exec_approvals_redirect`` (wrote a replacement
# ``exec-approvals.json``), and ``_disable_openclaw_tailscale`` (flipped
# tailscale.mode to "off"). All three have been removed.
#
# Reasons:
#   1. Touching openclaw config is forbidden by design — ARA must not
#      modify another tool's authoritative state. The agent's view of
#      its own config must remain unchanged for audit clarity.
#   2. Newer openclaw versions (≥ 2026.2.17) validate the config
#      strictly and reject unrecognized keys; ARA's audit annotations
#      crashed the gateway on startup.
#   3. The redirection MUST happen at the LD_PRELOAD libc shim layer
#      anyway as defense-in-depth (per the launch.py shim block) —
#      so the file rewrite was always redundant.
#
# Replacement: redirection lives entirely at the shim + env-var layer:
#   - ``libara_shim.so`` rewrites ``connect()`` sockaddrs based on
#     ``ARA_GATEWAY_WS_TAP_PORT`` / ``ARA_GATEWAY_WS_UPSTREAM_*`` env
#     vars (set by the launch path).
#   - ``ARA_DISABLE_GATEWAY_WS_TAP=1`` and
#     ``ARA_DISABLE_EXEC_APPROVALS_TAP=1`` opt out at the shim layer.
#   - openclaw config remains as-bootstrapped, agent's view authoritative.

def instrumentation_env() -> dict[str, str]:
    """Verbose-everything env. Enabled only when the operator asks for it.

    Keep this list narrow: every variable here is a debug hook we're willing
    to recommend by default. Tool-specific deep-debug (e.g. pino-pretty)
    belongs in the per-worker adapter, not here.
    """
    return {
        "NODE_DEBUG": "module,net,http,child_process",
        "DEBUG": "openclaw:*,codex:*,ara:*",
        "ARA_HOOK_DEBUG": "1",
        # Source maps on all Node processes — catches stack traces that
        # would otherwise only point at bundled line numbers.
        "NODE_OPTIONS_APPEND_SOURCEMAPS": "1",  # consumed below in launch merge
    }


__all__ = [
    "DEFAULT_HOME_TEMPLATE",
    "bootstrap_home",
    "build_armory_task_hermes_home",
    "build_contained_env",
    "contained_root",
    "instrumentation_env",
]
