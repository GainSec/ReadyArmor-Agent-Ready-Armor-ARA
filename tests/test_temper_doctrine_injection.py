from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ara import _rhema_shim as rhema
from ara import tempers
from ara import armors as armors_mod


@dataclass
class _Item:
    destination: str
    control: dict = field(default_factory=dict)
    title: str = "t"
    proposed_text: str = ""
    citations: list = field(default_factory=list)


def _doctrine_temper(state: Path, tid: str, text: str) -> None:
    tempers.ingest(state, tid, [_Item("absolute", proposed_text=text)])
    tempers.equip(state, tid)


def _make_active_armor(state: Path, name: str, absolute_text: str) -> None:
    armors_mod.create_armor(state, name, description="x")
    armor = armors_mod.load_armor(state, name)
    armor.absolute_path.write_text(absolute_text)
    (state / armors_mod.ACTIVE_FILE).write_text(name)


def test_no_armor_no_temper_returns_none(tmp_path: Path) -> None:
    assert rhema.active_absolute_rule(tmp_path) is None


def test_temper_only_appends_to_original_prompt(tmp_path: Path) -> None:
    _doctrine_temper(tmp_path, "creds", "TEMPER DOCTRINE: never disclose raw secrets.")
    rule = rhema.active_absolute_rule(tmp_path)
    assert rule is not None
    spec = rule.rewrite_with
    assert spec["append"] is True                 # additive, not replace
    assert "creds" in spec["tempers"]
    # applying it preserves the base prompt and appends the doctrine
    body = {"messages": [
        {"role": "system", "content": "BASE HERMES PROMPT"},
        {"role": "user", "content": "hi"},
    ]}
    out = rhema.apply_rewrite_spec(body, spec)
    sys_msg = out["messages"][0]["content"]
    assert "BASE HERMES PROMPT" in sys_msg          # original kept
    assert "never disclose raw secrets" in sys_msg  # temper appended


def test_armor_plus_temper_compose_in_one_envelope(tmp_path: Path) -> None:
    _make_active_armor(tmp_path, "hermes-default", "ARMOR IDENTITY DOCTRINE")
    _doctrine_temper(tmp_path, "hw", "TEMPER: no hardware flashing.")
    rule = rhema.active_absolute_rule(tmp_path)
    assert rule is not None
    spec = rule.rewrite_with
    assert spec["append"] is False                 # armor replaces; temper rides along
    assert "ARMOR IDENTITY DOCTRINE" in spec["text"]
    assert "no hardware flashing" in spec["text"]
    assert spec["tempers"] == ["hw"]


def test_armor_only_unaffected_when_no_temper(tmp_path: Path) -> None:
    _make_active_armor(tmp_path, "hermes-default", "ARMOR IDENTITY DOCTRINE")
    rule = rhema.active_absolute_rule(tmp_path)
    assert rule is not None
    assert rule.rewrite_with["text"] == "ARMOR IDENTITY DOCTRINE"
    assert rule.rewrite_with["tempers"] == []
    assert rule.rewrite_with["append"] is False


def test_unequip_temper_drops_its_doctrine(tmp_path: Path) -> None:
    _doctrine_temper(tmp_path, "creds", "TEMPER DOCTRINE: redact secrets.")
    tempers.unequip(tmp_path, "creds")
    assert rhema.active_absolute_rule(tmp_path) is None
