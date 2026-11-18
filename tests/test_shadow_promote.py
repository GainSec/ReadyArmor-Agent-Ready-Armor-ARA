"""Fix 1 follow-up (audit #8) — promote a shadow_rewrite variant into an
enforcing rewrite.

Two promotion sources:
  - ABSOLUTE live projection → flip armor meta's ``absolute_enforce``.
  - Operator rhema rule → flip rule's action from ``shadow_rewrite`` to
    ``rewrite`` and persist.
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")


from pathlib import Path

from ara import armors as armors_mod
from ara import rhema as rhema_mod


# ---- armor flag lifecycle -------------------------------------------------


def test_absolute_enforce_flag_defaults_false(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    assert armors_mod.is_absolute_enforcing(state, "a") is False


def test_set_absolute_enforce_persists(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    assert armors_mod.set_absolute_enforce(state, "a", True) is True
    assert armors_mod.is_absolute_enforcing(state, "a") is True
    # Reload from disk to prove it was saved, not just in-memory.
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    assert armor.meta.absolute_enforce is True


def test_set_absolute_enforce_unknown_armor_returns_false(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    assert armors_mod.set_absolute_enforce(state, "missing", True) is False


# ---- rhema.active_absolute_rule picks action off the flag -----------------


def test_active_absolute_rule_is_shadow_when_not_enforcing(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("X")
    armors_mod.set_active_armor(state, "a")
    rule = rhema_mod.active_absolute_rule(state)
    assert rule is not None
    assert rule.action == "shadow_rewrite"


def test_active_absolute_rule_is_rewrite_when_enforcing(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "a", description="t")
    armor = armors_mod.load_armor(state, "a")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("X")
    armors_mod.set_active_armor(state, "a")
    armors_mod.set_absolute_enforce(state, "a", True)
    rule = rhema_mod.active_absolute_rule(state)
    assert rule is not None
    assert rule.action == "rewrite"
    assert "ENFORCING" in (rule.reason or "")


# ---- operator rule promotion via rhema save -------------------------------


def test_operator_shadow_rule_can_be_promoted_to_rewrite(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    r = rhema_mod.upsert_managed(
        state, "test-op", scope="request", pattern="codex/responses",
        pattern_kind="substring",
        action="shadow_rewrite",
        rewrite_with={"op": "replace", "field": "instructions", "text": "NEW"},
        reason="A/B",
    )
    rules = rhema_mod.load(state)
    [target] = [x for x in rules if x.id == r.id]
    assert target.action == "shadow_rewrite"
    target.action = "rewrite"
    rhema_mod.save(state, rules)

    reloaded = rhema_mod.load(state)
    [after] = [x for x in reloaded if x.id == r.id]
    assert after.action == "rewrite"
    assert after.rewrite_with == {
        "op": "replace", "field": "instructions", "text": "NEW",
    }
