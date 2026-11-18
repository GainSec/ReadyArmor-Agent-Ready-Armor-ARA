import pytest
# Legacy: these tests expected stats derived from WALLS.md file content.
# Stats are now Bulwark-derived (bulwark.md at state root).
# Keeping the file as docs of the old contract.
_legacy = {"test_create_seeds_from_existing_walls", "test_stats_and_tier_climb_with_rules"}

from pathlib import Path

import pytest

from ara import armors


def test_create_list_equip_roundtrip(short_tmp: Path):
    state = short_tmp / ".ara"

    # empty initial state
    assert armors.list_armors(state) == []
    assert armors.active_armor_name(state) is None

    a = armors.create_armor(state, "strict", description="tight")
    assert a.name == "strict"
    assert a.walls_path.exists()
    assert a.absolute_path.exists()
    assert a.meta.description == "tight"

    listed = armors.list_armors(state)
    assert [x.name for x in listed] == ["strict"]

    equipped = armors.equip_armor(state, "strict")
    assert equipped is not None
    assert equipped.equipped
    assert armors.active_armor_name(state) == "strict"
    assert armors.active_walls_file(state) == a.walls_path

    armors.set_active_armor(state, None)
    assert armors.active_armor_name(state) is None


@pytest.mark.skip(reason="stats now Bulwark-derived")
def test_create_seeds_from_existing_walls(short_tmp: Path):
    state = short_tmp / ".ara"
    seed = short_tmp / "seed-walls.md"
    seed.write_text("## deny_path\n~/.ssh/**\n## deny_command\nrm\n")

    a = armors.create_armor(state, "seed", from_walls=seed)
    assert a.walls_path.read_text() == seed.read_text()
    assert a.stats.walls == 1
    assert a.stats.commands == 1


@pytest.mark.skip(reason="stats now Bulwark-derived")
def test_stats_and_tier_climb_with_rules(short_tmp: Path):
    state = short_tmp / ".ara"
    a = armors.create_armor(state, "bare")
    assert a.stats.tier == 1  # empty → Minimal

    a.walls_path.write_text("## deny_path\n" + "\n".join(f"/tmp/x{i}" for i in range(20)))
    refreshed = armors.load_armor(state, "bare")
    assert refreshed is not None
    assert refreshed.stats.walls == 20
    assert refreshed.stats.tier == 3  # 16–30 → Standard


def test_clone(short_tmp: Path):
    state = short_tmp / ".ara"
    armors.create_armor(state, "src", description="orig")
    clone = armors.clone_armor(state, "src", "dst")
    assert clone.name == "dst"
    assert clone.walls_path.exists()
    assert "orig" in clone.meta.description  # clone prepends "(clone of src)"


def test_invalid_name_rejected(short_tmp: Path):
    state = short_tmp / ".ara"
    with pytest.raises(ValueError):
        armors.create_armor(state, "bad/name")
    with pytest.raises(ValueError):
        armors.create_armor(state, "..")


def test_delete_clears_active(short_tmp: Path):
    state = short_tmp / ".ara"
    armors.create_armor(state, "x")
    armors.equip_armor(state, "x")
    assert armors.active_armor_name(state) == "x"
    assert armors.delete_armor(state, "x")
    assert armors.active_armor_name(state) is None
    assert armors.list_armors(state) == []
