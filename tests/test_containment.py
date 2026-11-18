"""Tests for session-scoped containment env remap."""

from __future__ import annotations

from pathlib import Path

import pytest

from ara.containment import (
    bootstrap_home,
    build_contained_env,
    contained_root,
    instrumentation_env,
)


def test_contained_root_creates(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "abc"
    root = contained_root(session)
    assert root.is_dir()
    assert root == session / "contain"


def test_build_contained_env_creates_tree(tmp_path: Path) -> None:
    session = tmp_path / "sess"
    env = build_contained_env(session, template_home=tmp_path / "empty_home")
    for key in (
        "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "XDG_DATA_HOME", "XDG_STATE_HOME", "npm_config_cache",
        "NODE_REPL_HISTORY", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH",
        "OPENCLAW_SANDBOX", "CODEX_HOME", "HERMES_HOME", "ARA_CONTAINED",
        "ARA_CONTAIN_ROOT", "ARA_CONTAIN_PROFILE",
    ):
        assert key in env, f"missing key: {key}"
    assert env["ARA_CONTAINED"] == "1"
    assert env["OPENCLAW_SANDBOX"] == "0"
    # Every path-valued var points under the session root.
    for key in (
        "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        "XDG_DATA_HOME", "XDG_STATE_HOME", "npm_config_cache",
        "NODE_REPL_HISTORY", "OPENCLAW_STATE_DIR", "CODEX_HOME",
        "HERMES_HOME", "ARA_CONTAIN_ROOT",
    ):
        assert Path(env[key]).resolve().is_relative_to(session.resolve()), (
            f"{key} leaks out of session: {env[key]}"
        )
    # HOME is actually a directory, not just a future path.
    assert Path(env["HOME"]).is_dir()
    # CODEX_HOME is a subpath of the contained HOME.
    assert Path(env["CODEX_HOME"]).is_relative_to(Path(env["HOME"]))


def test_bootstrap_home_copies_allowlisted(tmp_path: Path) -> None:
    real_home = tmp_path / "real"
    (real_home / ".codex").mkdir(parents=True)
    (real_home / ".codex" / "auth.json").write_text('{"t":"x"}')
    (real_home / ".codex" / "config.toml").write_text("model=foo")
    # A file outside the allowlist must NOT be copied.
    (real_home / ".ssh").mkdir()
    (real_home / ".ssh" / "id_rsa").write_text("SECRET")

    session_home = tmp_path / "contained_home"
    copied = bootstrap_home(session_home, real_home)

    assert ".codex/auth.json" in copied
    assert ".codex/config.toml" in copied
    assert (session_home / ".codex" / "auth.json").read_text() == '{"t":"x"}'
    # Allowlist is strict.
    assert not (session_home / ".ssh" / "id_rsa").exists()


def test_bootstrap_home_copies_hermes_model_config(tmp_path: Path) -> None:
    real_home = tmp_path / "real"
    (real_home / ".hermes").mkdir(parents=True)
    (real_home / ".hermes" / "config.yaml").write_text("model:\n  provider: lmstudio\n")
    (real_home / ".hermes" / ".env").write_text("OPENAI_API_KEY=secret\n")
    (real_home / ".hermes" / "logs").mkdir()
    (real_home / ".hermes" / "logs" / "agent.log").write_text("do not copy")

    session_home = tmp_path / "contained_home"
    copied = bootstrap_home(session_home, real_home)

    assert ".hermes/config.yaml" in copied
    assert ".hermes/.env" in copied
    assert (session_home / ".hermes" / "config.yaml").is_file()
    assert (session_home / ".hermes" / ".env").is_file()
    assert not (session_home / ".hermes" / "logs" / "agent.log").exists()


def test_bootstrap_home_copies_dir_tree(tmp_path: Path) -> None:
    """Entries ending in '/' copy the whole directory recursively so
    openclaw's gateway/identity/devices/credentials come along."""
    real = tmp_path / "real"
    (real / ".openclaw" / "gateway").mkdir(parents=True)
    (real / ".openclaw" / "openclaw.json").write_text("{}")
    (real / ".openclaw" / "gateway" / "state.json").write_text('{"x":1}')
    (real / ".openclaw" / "identity" / "peer.key").parent.mkdir()
    (real / ".openclaw" / "identity" / "peer.key").write_text("KEYBYTES")
    session_home = tmp_path / "h"
    copied = bootstrap_home(session_home, real, include=(".openclaw/",))
    assert (session_home / ".openclaw" / "openclaw.json").is_file()
    assert (session_home / ".openclaw" / "gateway" / "state.json").is_file()
    assert (session_home / ".openclaw" / "identity" / "peer.key").read_text() == "KEYBYTES"
    assert ".openclaw/identity/peer.key" in copied


def test_bootstrap_home_skips_missing(tmp_path: Path) -> None:
    real_home = tmp_path / "empty"
    real_home.mkdir()
    session_home = tmp_path / "h"
    copied = bootstrap_home(session_home, real_home)
    assert copied == []


def test_containment_does_not_modify_openclaw_config(tmp_path: Path) -> None:
    """ARA must not write into openclaw config files — even contained copies.
    All gateway/exec-approvals redirection happens at the LD_PRELOAD libc
    shim layer + env vars. The contained openclaw.json must be byte-identical
    to the source.
    """
    import json
    real = tmp_path / "home"
    (real / ".openclaw").mkdir(parents=True)
    src_json = (
        '{"gateway": {"tailscale": {"mode": "serve"}, "mode": "local",'
        ' "port": 9000}}'
    )
    (real / ".openclaw" / "openclaw.json").write_text(src_json)
    session = tmp_path / "s"
    build_contained_env(session, template_home=real)
    contained_cfg = session / "contain" / "home" / ".openclaw" / "openclaw.json"
    assert contained_cfg.is_file()
    contained_text = contained_cfg.read_text()
    # Byte-identical to source — no port rewrite, no _ara_redirected_from,
    # no tailscale flip, no _ara_note.
    assert contained_text == src_json
    data = json.loads(contained_text)
    assert "_ara_redirected_from" not in data.get("gateway", {})
    assert "_ara_note" not in data
    # Real config also untouched (the original guard).
    assert (real / ".openclaw" / "openclaw.json").read_text() == src_json


def test_instrumentation_env_has_expected_keys() -> None:
    env = instrumentation_env()
    assert env["NODE_DEBUG"]
    assert env["DEBUG"]
    assert env["ARA_HOOK_DEBUG"] == "1"


def test_build_contained_env_bootstraps_auth(tmp_path: Path) -> None:
    real = tmp_path / "home"
    (real / ".codex").mkdir(parents=True)
    (real / ".codex" / "auth.json").write_text("{}")
    session = tmp_path / "s"
    env = build_contained_env(session, template_home=real)
    assert (Path(env["HOME"]) / ".codex" / "auth.json").exists()


def test_build_contained_env_points_hermes_home_at_bootstrap(tmp_path: Path) -> None:
    real = tmp_path / "home"
    (real / ".hermes").mkdir(parents=True)
    (real / ".hermes" / "config.yaml").write_text("model:\n  provider: lmstudio\n")
    session = tmp_path / "s"

    env = build_contained_env(session, template_home=real)

    hermes_home = Path(env["HERMES_HOME"])
    assert hermes_home == Path(env["HOME"]) / ".hermes"
    assert (hermes_home / "config.yaml").is_file()


def test_armory_task_profile_uses_generated_hermes_home(tmp_path: Path) -> None:
    real = tmp_path / "home"
    (real / ".hermes").mkdir(parents=True)
    (real / ".hermes" / "config.yaml").write_text("agent:\n  personalities:\n    hype: bad\n")
    (real / ".hermes" / ".env").write_text("OPENAI_API_KEY=secret\n")
    (real / ".openclaw").mkdir(parents=True)
    (real / ".openclaw" / "openclaw.json").write_text('{"channels":{"discord":"yes"}}')
    session = tmp_path / "s"

    env = build_contained_env(session, profile="armory-task", template_home=real)

    hermes_home = Path(env["HERMES_HOME"])
    assert hermes_home == Path(env["HOME"]) / ".hermes"
    assert (hermes_home / "config.yaml").is_file()
    assert (hermes_home / "SOUL.md").is_file()
    assert (hermes_home / ".env").read_text() == "OPENAI_API_KEY=secret\n"
    config_text = (Path(env["HOME"]) / ".hermes" / "config.yaml").read_text()
    assert "mcp_servers:" in config_text
    assert "ara-forge:" in config_text
    assert 'ARA_FORGE_MCP_MODE: "task"' in config_text
    assert "platform_toolsets:" in config_text
    assert "cli: [terminal, file]" in config_text
    assert "cli: [terminal, file, browser]" not in config_text
    assert "mcp_ara_forge" not in (hermes_home / "SOUL.md").read_text()
    assert "personalities" not in config_text
    assert (Path(env["OPENCLAW_CONFIG_PATH"])).is_file()


def test_openclaw_config_path_matches_bootstrap_location(tmp_path: Path) -> None:
    """Regression: the adapter reads ``OPENCLAW_CONFIG_PATH`` and ignores
    HOME. Bootstrap + env must point at the same file or contained openclaw
    silently loses its Discord / Slack / Telegram config."""
    real = tmp_path / "home"
    (real / ".openclaw").mkdir(parents=True)
    (real / ".openclaw" / "openclaw.json").write_text('{"channels":{"discord":"yes"}}')
    (real / ".openclaw" / "clawdbot.json").write_text("{}")
    session = tmp_path / "s"
    env = build_contained_env(session, template_home=real)
    config_path = Path(env["OPENCLAW_CONFIG_PATH"])
    state_dir = Path(env["OPENCLAW_STATE_DIR"])
    # The env var must resolve to a file we actually populated.
    assert config_path.is_file(), (
        f"OPENCLAW_CONFIG_PATH points at {config_path}, which doesn't exist "
        "— bootstrap landed somewhere else"
    )
    assert '"discord"' in config_path.read_text()
    # clawdbot.json sits next to openclaw.json under the state dir.
    assert (state_dir / "clawdbot.json").is_file()


def test_mcp_profiles_preserve_launch_pythonpath_for_portable_import(tmp_path):
    import os
    import string
    import subprocess
    import sys
    import yaml
    from ara import containment

    source_root = str(Path(containment.__file__).resolve().parents[1])
    for name, server in (("forge", "ara-forge"), ("temper", "ara-temper")):
        seed = containment.ARA_SRC_DIR / "ara/seed/hermes-profiles" / name / "config.yaml"
        profile = yaml.safe_load(seed.read_text())
        configured = profile["mcp_servers"][server]["env"]["PYTHONPATH"]
        inherited = {**os.environ, "PYTHONPATH": source_root}
        expanded = string.Template(configured).substitute(inherited)
        env = {**inherited, "PYTHONPATH": expanded}
        result = subprocess.run(
            [sys.executable, "-c", "import ara; print(ara.__name__)"],
            cwd=tmp_path, env=env, text=True, capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ara"
