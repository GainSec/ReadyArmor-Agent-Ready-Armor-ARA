"""Audit #12 — cross-armor section copy. ``append_absolute_section``
takes a title + body and appends a ``## title`` block to the target
armor's active absolute file."""

from __future__ import annotations

from pathlib import Path

from ara import armors as armors_mod
from ara.armory.app import append_absolute_section


def test_append_creates_section_in_empty_file(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "dst", description="t")
    armor = armors_mod.load_armor(state, "dst")
    assert armor is not None
    armor.absolute_path.write_text("")

    ok, msg = append_absolute_section(state, "dst", "Persona", "Be kind.")
    assert ok is True and msg == "ok"
    text = armor.absolute_path.read_text()
    assert "## Persona" in text
    assert "Be kind." in text


def test_append_preserves_existing_content(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "dst", description="t")
    armor = armors_mod.load_armor(state, "dst")
    assert armor is not None
    armor.absolute_path.write_text("## First\n\nOriginal\n")

    append_absolute_section(state, "dst", "Second", "Added")
    text = armor.absolute_path.read_text()
    assert "## First" in text
    assert "Original" in text
    assert "## Second" in text
    assert "Added" in text
    # Existing content must come before the appended section.
    assert text.index("## First") < text.index("## Second")


def test_append_unknown_armor_returns_false(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    ok, msg = append_absolute_section(state, "missing", "T", "B")
    assert ok is False
    assert "not found" in msg.lower()


def test_append_strips_trailing_whitespace_on_body(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "dst", description="t")
    armor = armors_mod.load_armor(state, "dst")
    assert armor is not None
    armor.absolute_path.write_text("")

    append_absolute_section(state, "dst", "X", "body content\n\n\n")
    text = armor.absolute_path.read_text()
    assert text.endswith("body content\n"), repr(text)
