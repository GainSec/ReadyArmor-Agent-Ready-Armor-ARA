from pathlib import Path

from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind
from ara.forge.cli import _equip_forge_pair, _unequip_forge_pair


def test_pack_kind_equips_codex_pen(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="p1", operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    summary = _equip_forge_pair(s)
    assert summary["armor"] == "codex"
    assert summary["weapon"] == "pen"
    # unequip restores prior (nothing) without error
    _unequip_forge_pair(s)


def test_weapon_kind_still_equips_blacksmith(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    summary = _equip_forge_pair(s)
    assert summary["armor"] == "blacksmith"
    assert summary["weapon"] == "blacksmith-hammer"
