"""Tests for ForgeSession lifecycle + persistence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara.audit import verify_chain
from ara.forge import state as fstate
from ara.forge.session import (
    ForgeConfig,
    ForgePhase,
    ForgeSession,
    InvalidTransition,
)
from ara.runtime import audit as raudit


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()


def test_new_creates_dirs_and_persists(tmp_path: Path) -> None:
    s = ForgeSession.new(
        tmp_path, weapon_target_id="w1", operator_id="op",
        config=ForgeConfig(embedder_provider="null"),
    )
    assert s.phase == ForgePhase.DEPOSIT
    assert (fstate.forge_dir(tmp_path, s.forge_id) / "mats").is_dir()
    assert fstate.forge_session_path(tmp_path, s.forge_id).exists()
    assert fstate.forge_phase_path(tmp_path, s.forge_id).read_text() == "deposit"
    persisted = json.loads(fstate.forge_session_path(tmp_path, s.forge_id).read_text())
    assert persisted["weapon_target_id"] == "w1"


def test_load_round_trip(tmp_path: Path) -> None:
    s1 = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    s2 = ForgeSession.load(tmp_path, s1.forge_id)
    assert s2.forge_id == s1.forge_id
    assert s2.phase == s1.phase
    assert s2.config.embedder_provider == "null"


def test_full_happy_path_transition(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    s.transition(ForgePhase.MANIFEST, reason="deposit done")
    s.transition(ForgePhase.REVIEW, reason="manifest produced")
    s.transition(ForgePhase.EXHAUST, reason="manifest approved")
    s.transition(ForgePhase.REVIEW, reason="extractions done")
    s.transition(ForgePhase.SYNTHESIZE, reason="extractions approved")
    s.transition(ForgePhase.REVIEW, reason="synth done")
    s.transition(ForgePhase.SEAL, reason="synth approved")
    s.transition(ForgePhase.SEALED, reason="seal validated")
    assert s.phase == ForgePhase.SEALED
    assert fstate.forge_phase_path(tmp_path, s.forge_id).read_text() == "sealed"


def test_invalid_transition_rejected(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    with pytest.raises(InvalidTransition):
        s.transition(ForgePhase.SEAL)
    assert s.phase == ForgePhase.DEPOSIT


def test_abort_from_any_non_terminal(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    s.transition(ForgePhase.MANIFEST)
    s.abort(reason="operator cancelled")
    assert s.phase == ForgePhase.ABORTED
    # Idempotent (no-op when already terminal)
    s.abort(reason="again")
    assert s.phase == ForgePhase.ABORTED


def test_terminal_states_have_no_outgoing(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    s.transition(ForgePhase.MANIFEST)
    s.abort()
    with pytest.raises(InvalidTransition):
        s.transition(ForgePhase.MANIFEST)


def test_audit_chain_well_formed(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    s.transition(ForgePhase.MANIFEST)
    s.transition(ForgePhase.REVIEW)
    sink = fstate.forge_audit_path(tmp_path, s.forge_id)
    ok, last, err = verify_chain(sink)
    assert ok, err
    assert last == 3  # session.created + 2 transitions


def test_review_state_reentrant(tmp_path: Path) -> None:
    s = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op")
    s.transition(ForgePhase.MANIFEST)
    s.transition(ForgePhase.REVIEW)
    s.transition(ForgePhase.MANIFEST, reason="revise manifest")  # back-edge allowed
    s.transition(ForgePhase.REVIEW)
    assert s.phase == ForgePhase.REVIEW
