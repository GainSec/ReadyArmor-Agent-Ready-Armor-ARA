from pathlib import Path

from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind


def test_kind_defaults_weapon_and_roundtrips(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    assert s.config.kind == "pack"
    loaded = ForgeSession.load(tmp_path, s.forge_id)
    assert loaded.config.kind == "pack"


def test_kind_default_is_weapon(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    assert s.config.kind == "weapon"
    assert ForgeSession.load(tmp_path, s.forge_id).config.kind == "weapon"
