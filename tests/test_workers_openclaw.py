from pathlib import Path

from ara.workers import openclaw as oc


def test_state_dir_namespaced(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = oc.state_dir_for("demo")
    assert d == tmp_path / ".openclaw-ara-demo"


def test_env_for_sets_required_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = oc.env_for("poc")
    assert env["OPENCLAW_STATE_DIR"].endswith("/.openclaw-ara-poc")
    assert env["OPENCLAW_CONFIG_PATH"].endswith("/.openclaw-ara-poc/openclaw.json")
    assert env["OPENCLAW_SANDBOX"] == "0"
    assert Path(env["OPENCLAW_STATE_DIR"]).is_dir()


def test_env_for_default_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = oc.env_for("")
    assert env["OPENCLAW_STATE_DIR"].endswith("/.openclaw-ara-default")


def test_build_command_uses_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cmd, env = oc.build_command(
        "demo",
        ["agent", "--local", "-m", "hi"],
        binary_override="/fake/openclaw",
    )
    assert cmd == ["/fake/openclaw", "agent", "--local", "-m", "hi"]
    assert env["OPENCLAW_STATE_DIR"].endswith("/.openclaw-ara-demo")


def test_build_command_does_not_inject_openclaw_profile_flag(
    monkeypatch, tmp_path
):
    """ARA owns the profile concept; OpenClaw's own --profile must not appear."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cmd, _ = oc.build_command(
        "demo", ["agent"], binary_override="/fake/openclaw"
    )
    assert "--profile" not in cmd
