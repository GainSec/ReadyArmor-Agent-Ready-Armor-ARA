"""Phase 1 — armor channel scoping. Metadata only; enforcement gating
arrives in Phase 2.
"""

from __future__ import annotations

from pathlib import Path

from ara import armors as armors_mod


def test_default_channels_is_empty(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    assert armor.meta.channels == []


def test_set_channels_persists_known_ids(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    ok = armors_mod.set_channels(state, "a", ["discord", "signal"])
    assert ok is True
    assert armors_mod.get_channels(state, "a") == ["discord", "signal"]


def test_set_channels_drops_unknown_ids(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armors_mod.set_channels(state, "a", ["discord", "bogus", "signal"])
    assert armors_mod.get_channels(state, "a") == ["discord", "signal"]


def test_set_channels_empty_clears_to_global(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armors_mod.set_channels(state, "a", ["discord"])
    armors_mod.set_channels(state, "a", [])
    assert armors_mod.get_channels(state, "a") == []


def test_set_channels_unknown_armor(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    assert armors_mod.set_channels(state, "missing", ["discord"]) is False


def test_channel_color_returns_brand_for_known(short_tmp: Path):
    assert armors_mod.channel_color("discord") == "#5865F2"
    assert armors_mod.channel_color("telegram") == "#229ED9"


def test_channel_color_returns_default_for_unknown(short_tmp: Path):
    # Falls back to a neutral so UI never blanks out.
    assert armors_mod.channel_color("nope") == "#888888"


def test_channels_round_trip_through_meta_save(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armors_mod.set_channels(state, "a", ["discord", "imessage"])
    # Reload from disk completely.
    reloaded = armors_mod.load_armor(state, "a")
    assert reloaded is not None
    assert reloaded.meta.channels == ["discord", "imessage"]
