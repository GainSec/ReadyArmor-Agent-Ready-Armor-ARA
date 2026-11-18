"""Credential-shaped values in these tests are synthetic canary fixtures."""

from __future__ import annotations

from pathlib import Path

from ara import seed_temper, tempers, temper_controls, temper_ingest, bulwark


def test_all_baseline_doctrines_validate() -> None:
    for d in seed_temper.BASELINE_DOCTRINES:
        temper_controls.validate_thread_type(d["thread_type"])
        temper_controls.validate_control(d["destination"], dict(d["control"]))
        if d["destination"] == "rhema":
            temper_controls.validate_rhema_pattern(d["control"]["pattern"])


def test_baseline_has_no_per_tool_denylist() -> None:
    # the whole point: no per-tool DENY rules; hardening only.
    for d in seed_temper.BASELINE_DOCTRINES:
        assert d["destination"] in {"canary", "rhema", "absolute", "approval_gate"}
        if d["destination"] == "approval_gate":
            # gates are 'approval', never blanket 'deny'
            assert "trigger" in d["control"]


def test_install_writes_policy_and_enforced_rules_fire(tmp_path: Path) -> None:
    meta = seed_temper.install_baseline_temper(tmp_path, equip=True)
    assert tempers.is_equipped(tmp_path, seed_temper.BASELINE_TEMPER_ID)
    # 6 enforced (3 canary + 3 approval_gate); absolutes -> advisory doctrine
    assert meta["bulwark_rules"] >= 6
    assert "secret_handling" in meta["enforced"] or "canary" in meta["enforced"]

    bw = bulwark.parse((tmp_path / "bulwark.md").read_text())
    # credential canary blocks a leaking reply
    d1 = bulwark.evaluate(bw, {"layer": "reply",
                               "content": ("api_key = sk_live_" + "ABCDEFGH12345678")})
    assert d1.action == "deny"
    # destructive device op needs approval
    d2 = bulwark.evaluate(bw, {"layer": "exec",
                               "path": "esptool", "argv": "write_flash 0x0 fw.bin"})
    assert d2.action == "approval"
    # a normal command is untouched
    d3 = bulwark.evaluate(bw, {"layer": "exec", "path": "curl", "argv": "-s https://x"})
    assert d3.action == "allow"


def test_install_absolutes_land_as_doctrine(tmp_path: Path) -> None:
    seed_temper.install_baseline_temper(tmp_path)
    abs_text = tempers.absolute_policy_path(
        tmp_path, seed_temper.BASELINE_TEMPER_ID).read_text()
    assert "never disclose raw secret" in abs_text.lower() \
        or "never output raw secret" in abs_text.lower()


def test_baseline_is_reasonably_small() -> None:
    # a temper is a small curated set, not forged volume
    assert 8 <= len(seed_temper.BASELINE_DOCTRINES) <= 15
