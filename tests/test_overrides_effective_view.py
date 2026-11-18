"""Audit #5 — /overrides and /blackholes render a merged global +
per-armor view with an 'Applies now?' column.
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")


import time
from pathlib import Path

from ara import armors as armors_mod
from ara import overrides as overrides_mod
from ara.armory import views


def test_overrides_page_shows_global_entries(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    overrides_mod.add_override(
        state, "path", "/tmp/global-allowed",
        reason="test-global",
    )
    html = views.render_overrides(state, "tok")
    assert "/tmp/global-allowed" in html
    assert ">global<" in html
    assert "YES" in html


def test_overrides_page_shows_equipped_armor_entries(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "guard", description="t")
    armor = armors_mod.load_armor(state, "guard")
    assert armor is not None
    overrides_mod.add_override_at(
        armor.overrides_path, "path", "/tmp/armor-allowed",
        reason="test-armor",
    )
    armors_mod.set_active_armor(state, "guard")

    html = views.render_overrides(state, "tok")
    assert "/tmp/armor-allowed" in html
    assert "armor:guard" in html
    assert "YES" in html


def test_overrides_page_marks_expired_as_not_applying(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    overrides_mod.add_override(
        state, "path", "/tmp/expired",
        reason="expired-test",
        expires_at=time.time() - 60,
    )
    html = views.render_overrides(state, "tok")
    assert "/tmp/expired" in html
    assert "NO" in html
    assert "expired" in html.lower()


def test_overrides_page_empty_when_nothing_set(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    html = views.render_overrides(state, "tok")
    assert "(empty)" in html


def test_blackholes_page_uses_same_merging(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "guard", description="t")
    armor = armors_mod.load_armor(state, "guard")
    assert armor is not None
    overrides_mod.add_blackhole_at(
        armor.overrides_path, "command", "rm -rf",
        reason="prevent",
    )
    armors_mod.set_active_armor(state, "guard")

    html = views.render_blackholes(state, "tok")
    assert "rm -rf" in html
    assert "armor:guard" in html
