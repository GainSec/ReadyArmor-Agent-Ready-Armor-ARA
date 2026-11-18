"""Fix 1 — ABSOLUTE.md as an ephemeral live projection, not a persisted
managed Rhema rule.

Tests the helper that builds the in-memory rule + the migration that
purges any stale ``abs-sync`` managed entries left over from earlier
builds."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")


import json
from pathlib import Path

import pytest

from ara import armors as armors_mod
from ara import rhema as rhema_mod


def _mk_armor_with_absolute(state_root: Path, name: str, body: str) -> None:
    """Create armor ``name`` with its active ABSOLUTE.md = body. Uses the
    real armors.create_armor so the seeded layout matches production."""
    armors_mod.create_armor(state_root, name, description="test")
    armor = armors_mod.load_armor(state_root, name)
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text(body)


def test_active_absolute_rule_returns_none_when_no_armor_equipped(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    assert rhema_mod.active_absolute_rule(state) is None


def test_active_absolute_rule_returns_none_when_file_empty(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    _mk_armor_with_absolute(state, "a", "")
    armors_mod.set_active_armor(state, "a")
    assert rhema_mod.active_absolute_rule(state) is None


def test_active_absolute_rule_returns_none_when_file_missing(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armors_mod.set_active_armor(state, "a")
    # Wipe the default absolute that create_armor seeded.
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").unlink()
    # set_active_absolute points at the (now missing) file; helper returns None.
    assert rhema_mod.active_absolute_rule(state) is None


def test_active_absolute_rule_builds_replace_spec(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    _mk_armor_with_absolute(state, "a", "## Persona\nBe kind.\n")
    armors_mod.set_active_armor(state, "a")
    rule = rhema_mod.active_absolute_rule(state)
    assert rule is not None
    assert rule.scope == "request"
    assert rule.action == "shadow_rewrite"
    assert rule.pattern_kind == "substring"
    assert "codex/responses" in rule.pattern
    assert rule.managed_key == "abs-live"
    assert rule.rewrite_with == {
        "op": "replace", "field": "instructions",
        "text": "## Persona\nBe kind.",  # trimmed
    }
    # id is derived from armor name for traceability in logs.
    assert rule.id == "abs-live-a"


def test_active_absolute_rule_is_ephemeral_never_persisted(short_tmp: Path):
    """Must not write to rhema.json — this is the whole point of Fix 1."""
    state = short_tmp / "state"
    state.mkdir()
    _mk_armor_with_absolute(state, "a", "live prompt")
    armors_mod.set_active_armor(state, "a")
    rhema_path = rhema_mod.path_for(state)
    before = rhema_path.read_text() if rhema_path.exists() else ""
    for _ in range(5):
        _ = rhema_mod.active_absolute_rule(state)
    after = rhema_path.read_text() if rhema_path.exists() else ""
    assert before == after


def test_active_absolute_rule_picks_up_edits_immediately(short_tmp: Path):
    """Edit the file, call again — new content is reflected. No cache."""
    state = short_tmp / "state"
    state.mkdir()
    _mk_armor_with_absolute(state, "a", "first")
    armors_mod.set_active_armor(state, "a")
    r1 = rhema_mod.active_absolute_rule(state)
    assert r1 is not None
    assert r1.rewrite_with["text"] == "first"

    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("second")

    r2 = rhema_mod.active_absolute_rule(state)
    assert r2 is not None
    assert r2.rewrite_with["text"] == "second"


def test_active_absolute_rule_follows_active_pointer(short_tmp: Path):
    """Swapping which absolute is active inside an armor changes what
    the projection returns."""
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armors_mod.set_active_armor(state, "a")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("DEFAULT-CONTENT")
    armors_mod.create_absolute(armor, "alt", "ALT-CONTENT")
    armors_mod.set_active_absolute(armor, "alt")
    rule = rhema_mod.active_absolute_rule(state)
    assert rule is not None
    assert rule.rewrite_with["text"] == "ALT-CONTENT"


def test_purge_managed_removes_abs_sync_and_leaves_operator_rules(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    # Operator rule.
    op = rhema_mod.add(
        state, scope="request", pattern="x", pattern_kind="substring",
        action="deny", redirect="no", reason="operator",
    )
    # Legacy managed rule as if written by old build.
    rhema_mod.upsert_managed(
        state, "abs-sync",
        scope="request", pattern="codex/responses",
        pattern_kind="substring", action="shadow_rewrite",
        rewrite_with={"op": "replace", "field": "instructions", "text": "OLD"},
    )
    assert len(rhema_mod.load(state)) == 2
    removed = rhema_mod.purge_managed(state, "abs-sync")
    assert removed == 1
    remaining = rhema_mod.load(state)
    assert len(remaining) == 1
    assert remaining[0].id == op.id
    # Idempotent: second purge is a no-op.
    assert rhema_mod.purge_managed(state, "abs-sync") == 0
