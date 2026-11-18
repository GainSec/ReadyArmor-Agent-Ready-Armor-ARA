"""Audit #22 — duplicate an absolute within one armor."""

from __future__ import annotations

from pathlib import Path

from ara import armors as armors_mod


def test_duplicate_copies_contents(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("BODY-V1")
    ok = armors_mod.duplicate_absolute(armor, "default", "alt")
    assert ok is True
    assert armors_mod.path_for_absolute(armor, "alt").read_text() == "BODY-V1"


def test_duplicate_refuses_when_dst_exists(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("A")
    armors_mod.create_absolute(armor, "alt", "B")
    ok = armors_mod.duplicate_absolute(armor, "default", "alt")
    assert ok is False
    # Existing file unchanged.
    assert armors_mod.path_for_absolute(armor, "alt").read_text() == "B"


def test_duplicate_refuses_when_src_missing(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    ok = armors_mod.duplicate_absolute(armor, "nope", "copy")
    assert ok is False


def test_duplicate_sanitizes_name(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("X")
    assert armors_mod.duplicate_absolute(armor, "default", "  ") is False
