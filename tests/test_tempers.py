"""Credential-shaped values in these tests are synthetic canary fixtures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ara import tempers, bulwark


@dataclass
class _Item:
    destination: str
    control: dict = field(default_factory=dict)
    title: str = "t"
    proposed_text: str = ""
    citations: list = field(default_factory=list)


def _bw_decision(state: Path, event: dict):
    bw = bulwark.parse((state / "bulwark.md").read_text())
    return bulwark.evaluate(bw, event).action


def _hw_item():
    return _Item("bulwark", {
        "action": "deny", "layer": "exec", "match": "jtag|uart|esptool",
        "exception_path": "op"}, title="Block flash",
        citations=[{"manifest_element_id": "elem_hw"}])


def _cred_item():
    return _Item("canary", {
        "signal": "cred", "pattern": "AKIA[0-9A-Z]{16}", "severity": "high",
        "trip_when": "secret out", "response": "flag"}, title="Cred canary")


def test_ingest_writes_own_policy_files(tmp_path: Path) -> None:
    meta = tempers.ingest(tmp_path, "wstg-hardening", [_hw_item()],
                          name="WSTG Hardening", source_forge="forge-1")
    bw = tempers.bulwark_policy_path(tmp_path, "wstg-hardening").read_text()
    assert "## global" in bw
    assert "deny exec regex:jtag|uart|esptool" in bw
    assert "# cite: elem_hw" in bw
    assert meta["bulwark_rules"] == 1
    assert "wstg-hardening" in tempers.list_tempers(tmp_path)


def test_equip_merges_overlay_and_rule_fires(tmp_path: Path) -> None:
    tempers.ingest(tmp_path, "t1", [_hw_item()])
    assert not (tmp_path / "bulwark.md").exists()
    tempers.equip(tmp_path, "t1")
    assert "ARA-TEMPER-OVERLAY-BEGIN t1" in (tmp_path / "bulwark.md").read_text()
    assert tempers.is_equipped(tmp_path, "t1")
    # the merged rule actually fires
    assert _bw_decision(tmp_path, {"layer": "exec", "path": "esptool", "argv": "x"}) == "deny"
    assert _bw_decision(tmp_path, {"layer": "exec", "path": "ls", "argv": "-l"}) == "allow"


def test_unequip_strips_overlay_cleanly(tmp_path: Path) -> None:
    # a pre-existing operator rule must survive equip/unequip untouched.
    (tmp_path / "bulwark.md").write_text("## global\ndeny net evil.test\n")
    tempers.ingest(tmp_path, "t1", [_hw_item()])
    tempers.equip(tmp_path, "t1")
    tempers.unequip(tmp_path, "t1")
    text = (tmp_path / "bulwark.md").read_text()
    assert "ARA-TEMPER-OVERLAY-BEGIN" not in text
    assert "deny net evil.test" in text            # operator rule preserved
    assert not tempers.is_equipped(tmp_path, "t1")


def test_tempers_stack_both_overlays_live(tmp_path: Path) -> None:
    tempers.ingest(tmp_path, "hw", [_hw_item()])
    tempers.ingest(tmp_path, "creds", [_cred_item()])
    tempers.equip(tmp_path, "hw")
    tempers.equip(tmp_path, "creds")
    assert tempers.active_tempers(tmp_path) == ["hw", "creds"]
    # both tempers' rules are simultaneously enforced
    assert _bw_decision(tmp_path, {"layer": "exec", "path": "esptool", "argv": "x"}) == "deny"
    assert _bw_decision(tmp_path, {"layer": "reply", "content": ("AKIA" + "ABCDEFGHIJKLMNOP")}) == "deny"
    # dropping one leaves the other live
    tempers.unequip(tmp_path, "hw")
    assert tempers.active_tempers(tmp_path) == ["creds"]
    assert _bw_decision(tmp_path, {"layer": "exec", "path": "esptool", "argv": "x"}) == "allow"
    assert _bw_decision(tmp_path, {"layer": "reply", "content": ("AKIA" + "ABCDEFGHIJKLMNOP")}) == "deny"


def test_equip_is_independent_of_armor(tmp_path: Path) -> None:
    # no armor exists; a temper still equips and enforces on its own slot.
    tempers.ingest(tmp_path, "solo", [_hw_item()])
    tempers.equip(tmp_path, "solo")
    assert _bw_decision(tmp_path, {"layer": "exec", "path": "esptool", "argv": "x"}) == "deny"


def test_new_temper_id_is_unique_per_run(tmp_path: Path) -> None:
    a = tempers.new_temper_id(tmp_path, "WSTG Hardening")
    assert a == "wstg-hardening"
    tempers.ingest(tmp_path, a, [_hw_item()], name="WSTG Hardening")
    # a second run with the same name must NOT overwrite — it's a new temper
    b = tempers.new_temper_id(tmp_path, "WSTG Hardening")
    assert b == "wstg-hardening-2"
    tempers.ingest(tmp_path, b, [_hw_item()], name="WSTG Hardening")
    c = tempers.new_temper_id(tmp_path, "WSTG Hardening")
    assert c == "wstg-hardening-3"
    assert set(tempers.list_tempers(tmp_path)) == {"wstg-hardening", "wstg-hardening-2"}
