from pathlib import Path

from ara.forge import loop_driver
from ara.forge import state as fstate
from ara.forge.phases import deposit, exhaust, manifest
from ara.forge.phases.exhaust import record_extraction
from ara.forge.session import ForgeConfig, ForgeKind, ForgeSession


def _pack_to_exhaust(tmp_path):
    state_root = tmp_path / "state"
    (tmp_path / "src").mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="p", operator_id="op",
                            config=ForgeConfig(kind=ForgeKind.PACK.value))
    md = tmp_path / "src" / "r.md"
    md.write_text("# T\n\n## Auth\nDefault creds on /login\n\n## Inj\nSQLi in id param\n")
    deposit.deposit_mat(sess, md)
    deposit.finalize_deposit(sess)
    manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    return sess


def test_run_pack_exhaust_drives_agent_and_records_proposals(tmp_path: Path):
    sess = _pack_to_exhaust(tmp_path)
    seen_prompts = []

    def fake_agent(state_root, prompt):
        seen_prompts.append(prompt)
        # emulate the contained agent proposing via the propose.* channels
        s = ForgeSession.load(state_root, sess.forge_id)
        mid = manifest.read_manifest(
            fstate.forge_manifest_path(state_root, sess.forge_id))[0].id
        record_extraction(s, manifest_element_id=mid, channel="propose.alphabet",
                          body={"name": "cat", "shape": "categorical",
                                "fields": [{"name": "id", "type": "str"}]},
                          extraction_id="a1")
        return 0

    res = loop_driver.run_pack_exhaust(
        sess.state_root, sess.forge_id, agent_invoke=fake_agent,
        target_coverage=0.01, max_iterations=1)
    assert res.final_extractions >= 1
    assert "alphabet pack" in seen_prompts[0].lower()   # pack prompt, not weapon
    channels = {x.channel for x in
                exhaust.read_extractions(sess.state_root, sess.forge_id)}
    assert "propose.alphabet" in channels


def test_default_agent_invoke_merges_extra_env(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env", {})
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(loop_driver.subprocess, "run", fake_run)
    monkeypatch.setattr(loop_driver.shutil, "which", lambda x: "/usr/bin/true")
    loop_driver._default_agent_invoke(
        tmp_path, "p", extra_env={"ARA_FORGE_MCP_MODE": "pack"})
    assert captured["env"].get("ARA_FORGE_MCP_MODE") == "pack"
    # the forge profile must be selected so the ara-forge MCP bridge attaches
    # (without it the contained agent falls back to the wrong MCP tools)
    cmd = captured["cmd"]
    assert "--contain-profile" in cmd
    assert cmd[cmd.index("--contain-profile") + 1] == "forge"


def test_handler_routes_pack_to_agentic(tmp_path: Path, monkeypatch):
    from ara.armory import forge_actions
    sess = _pack_to_exhaust(tmp_path)
    called = {}

    def fake(state_root, forge_id, **kw):
        called["hit"] = True
        return loop_driver.DriverResult(
            forge_id=forge_id, final_coverage=0.5,
            final_extractions=3, stopped_reason="ok")

    monkeypatch.setattr(loop_driver, "run_pack_exhaust", fake)
    ok, msg = forge_actions.handle_forge_run_exhaust(
        sess.state_root, sess.forge_id, {})
    assert ok and called.get("hit"), msg
    assert "stateless" not in msg.lower()


def test_handler_surfaces_agent_failure_no_fallback(tmp_path: Path, monkeypatch):
    from ara.armory import forge_actions
    sess = _pack_to_exhaust(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("hermes not found")

    def no_fallback(*a, **k):
        raise AssertionError("stateless fallback ran")

    monkeypatch.setattr(loop_driver, "run_pack_exhaust", boom)
    monkeypatch.setattr(loop_driver, "run_pack_exhaust_stateless", no_fallback)
    ok, msg = forge_actions.handle_forge_run_exhaust(
        sess.state_root, sess.forge_id, {})
    assert not ok and "hermes not found" in msg


def test_forge_profile_installs_ara_forge_mcp_bridge(tmp_path: Path):
    # Root cause of the live bug: contained forge agents inherited the
    # operator's ~/.hermes (a temper config). profile=forge must install the
    # forge profile so the agent gets the ara-forge MCP bridge.
    from ara import containment
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / ".env").write_text("SECRET=keep\n")
    dst = containment._install_hermes_profile(home, containment.FORGE_PROFILE_SEED)
    cfg = (dst / "config.yaml").read_text()
    assert "ara-forge" in cfg
    assert "ara-temper" not in cfg
    # bootstrapped credentials preserved
    assert (dst / ".env").read_text() == "SECRET=keep\n"
