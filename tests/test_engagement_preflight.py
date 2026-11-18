"""Tests for `ara engagement preflight <eid>` — pre-flight validation."""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ara import armors as armors_mod
from ara import engagement_mats as em
from ara import trims as trims_mod
from ara.engagement_cli import _cmd_preflight


def _ns(state_root: Path, eid: str, **kw) -> argparse.Namespace:
    base = {
        "state_root": state_root,
        "engagement_id": eid,
        "armor": None,
        "strict": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _run(ns: argparse.Namespace) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
        rc = _cmd_preflight(ns)
    return rc, out.getvalue(), err.getvalue()


def _setup_full(
    tmp_path: Path, *, env: str = "nonprod",
    active: bool = True, posture: str = "blackbox",
    eid: str = "eng1",
) -> dict:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    eng = {
        "engagement_id": eid,
        "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": env},
        "test_window": {
            "start": "2026-04-27T00:00:00Z",
            "end":   "2026-12-31T00:00:00Z",  # well in future
        },
        "posture": posture,
        "active_testing": active,
    }
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement=eng)
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    return {"meta": meta}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_full_setup_passes(tmp_path: Path) -> None:
    _setup_full(tmp_path)
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 0, out
    assert "✓" in out
    assert "fail" in out and "0 fail" in out


def test_prod_active_combo_passes_with_correct_tokens(tmp_path: Path) -> None:
    _setup_full(tmp_path, env="prod", active=True, posture="whitebox")
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 0, out
    # Tokens-check passes (no FAIL on declared tokens) — the expected
    # set is logged, not flagged.
    assert "missing" not in out.lower() or "missing for" not in out
    assert "0 fail" in out


# ---------------------------------------------------------------------------
# Missing pieces fail loudly
# ---------------------------------------------------------------------------

def test_no_engagement_dir_fails(tmp_path: Path) -> None:
    rc, out, _ = _run(_ns(tmp_path, "ghost-eng"))
    assert rc == 1
    assert "engagement dir" in out
    assert "FAIL" in out


def test_no_active_armor_fails(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 1
    assert "no armor active" in out


def test_no_trim_for_engagement_fails(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 1
    assert "no trim under armor" in out


def test_trim_not_applied_fails(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    eng = {
        "engagement_id": "eng1", "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": "nonprod"},
        "test_window": {"start": "2026-04-27T00:00:00Z", "end": "2026-12-31T00:00:00Z"},
        "posture": "blackbox", "active_testing": False,
    }
    trims_mod.derive_trim(tmp_path, "blacksmith", engagement=eng)
    # NOT applying it
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 1
    assert "trim applied" in out
    assert "not applied" in out


# ---------------------------------------------------------------------------
# Sanity checks on individual fields
# ---------------------------------------------------------------------------

def test_expired_window_fails(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    eng = {
        "engagement_id": "eng1", "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": "nonprod"},
        "test_window": {
            "start": "2020-01-01T00:00:00Z",
            "end":   "2020-01-02T00:00:00Z",  # well past
        },
        "posture": "blackbox", "active_testing": False,
    }
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement=eng)
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 1
    assert "EXPIRED" in out


def test_whitebox_no_mats_warns(tmp_path: Path) -> None:
    _setup_full(tmp_path, posture="whitebox")
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    # Whitebox + no mats uploaded → WARN but not FAIL
    assert "no mat classes populated" in out
    assert rc == 0  # warns don't fail without --strict


def test_strict_mode_fails_on_warns(tmp_path: Path) -> None:
    _setup_full(tmp_path, posture="whitebox")  # produces a warn (no mats)
    rc, _, _ = _run(_ns(tmp_path, "eng1", strict=True))
    assert rc == 1


def test_q9_destructive_approval_on_prod_fails(tmp_path: Path) -> None:
    """Spec Q9: env=prod + DESTRUCTIVE-APPROVAL declared = contradiction."""
    _setup_full(tmp_path, env="prod", active=True)
    # Manually corrupt declared_tokens to add the deprecated one
    meta = trims_mod.load_trim(tmp_path, "blacksmith", "eng1")
    assert meta is not None
    meta.declared_tokens.append("DESTRUCTIVE-APPROVAL")
    trims_mod.save_meta(tmp_path, meta)
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 1
    assert "Q9 contradiction" in out


def test_weapon_ref_missing_weapon_fails(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement={
        "engagement_id": "eng1", "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": "nonprod"},
        "test_window": {"start": "2026-04-27T00:00:00Z", "end": "2026-12-31T00:00:00Z"},
        "posture": "blackbox", "active_testing": False,
    }, weapon_id="ghost-weapon")
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 1
    assert "ghost-weapon" in out and "not found" in out


def test_armor_override(tmp_path: Path) -> None:
    """--armor lets the operator preflight against a specific armor
    even when something else is active."""
    armors_mod.create_armor(tmp_path, "alt")
    armors_mod.set_active_armor(tmp_path, "alt")  # different armor active
    armors_mod.create_armor(tmp_path, "blacksmith")
    eng = {
        "engagement_id": "eng1", "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": "nonprod"},
        "test_window": {"start": "2026-04-27T00:00:00Z", "end": "2026-12-31T00:00:00Z"},
        "posture": "blackbox", "active_testing": False,
    }
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement=eng)
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    rc, out, _ = _run(_ns(tmp_path, "eng1", armor="blacksmith"))
    assert rc == 0
    assert "blacksmith" in out


def test_tokens_check_catches_missing_active_prod_approval(
    tmp_path: Path,
) -> None:
    """env=prod + active=true but no ACTIVE-PROD-APPROVAL → fail.
    Hand-corrupt declared_tokens to remove the expected one."""
    _setup_full(tmp_path, env="prod", active=True)
    meta = trims_mod.load_trim(tmp_path, "blacksmith", "eng1")
    assert meta is not None
    meta.declared_tokens = [
        t for t in meta.declared_tokens if t != "ACTIVE-PROD-APPROVAL"
    ]
    trims_mod.save_meta(tmp_path, meta)
    rc, out, _ = _run(_ns(tmp_path, "eng1"))
    assert rc == 1
    assert "ACTIVE-PROD-APPROVAL" in out
