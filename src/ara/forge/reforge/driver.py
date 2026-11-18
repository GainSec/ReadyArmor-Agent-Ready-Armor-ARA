"""Reforge driver — the operator-facing API on top of
:mod:`ara.forge.reforge.proposal`.

Workflow::

    ara forge new <weapon_target_id> --predecessor <prior_weapon_id>
    # ... deposit / manifest / exhaust / synthesize as usual ...
    ara forge reforge propose <forge_id>
    # operator reviews <forge_dir>/proposal.json (or armory UI)
    ara forge reforge approve <forge_id>
    ara forge seal <forge_id>

A proposal is the typed delta between the predecessor sealed weapon and
the in-flight ``weapon_draft.json``. The proposal lives at
``<forge_dir>/proposal.json``; the operator approves it, which records
``approved_by`` + ``applied_at`` and transitions back to REVIEW so the
existing seal flow takes over.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ara.forge import state as fstate
from ara.forge.phases.synthesize import load_draft
from ara.forge.reforge.proposal import (
    ReforgeProposal,
    build_proposal,
    read_proposal,
    write_proposal,
)
from ara.forge.session import ForgePhase, ForgeSession


class ReforgeError(Exception):
    pass


def _predecessor_weapon_json(state_root: Path, predecessor_id: str) -> dict:
    wpath = fstate.weapon_dir(state_root, predecessor_id) / "weapon.json"
    if not wpath.is_file():
        raise ReforgeError(
            f"predecessor weapon {predecessor_id!r} has no weapon.json "
            f"at {wpath}"
        )
    try:
        return json.loads(wpath.read_text())
    except Exception as exc:
        raise ReforgeError(
            f"could not parse predecessor weapon.json: {exc}"
        ) from exc


def propose(sess: ForgeSession, *,
            triggered_by: str = "operator",
            rationales: dict[str, str] | None = None,
            new_calibration_queries: list[dict] | None = None,
            ) -> ReforgeProposal:
    """Build and persist a reforge proposal for ``sess``.

    Requires:
    - ``sess.weapon_predecessor_id`` set (operator must pass --predecessor
      at ``forge new`` time, or via ``ForgeSession.new``)
    - phase REVIEW (so the draft has been synthesized)
    - ``weapon_draft.json`` written

    On success, transitions the session to REFORGE_PROPOSAL.
    """
    if sess.phase != ForgePhase.REVIEW:
        raise ReforgeError(
            f"propose requires REVIEW (current: {sess.phase.value})"
        )
    if not sess.weapon_predecessor_id:
        raise ReforgeError(
            "session has no weapon_predecessor_id — start a forge with "
            "--predecessor <weapon_id> to enable reforge"
        )
    draft = load_draft(sess.state_root, sess.forge_id)
    if draft is None:
        raise ReforgeError(
            "no weapon_draft.json — run synthesize before propose"
        )
    prior = _predecessor_weapon_json(
        sess.state_root, sess.weapon_predecessor_id,
    )
    proposal = build_proposal(
        weapon_predecessor_id=sess.weapon_predecessor_id,
        forge_id=sess.forge_id,
        prior_weapon_json=prior,
        proposed_weapon_json=draft.to_jsonable(),
        triggered_by=triggered_by,
        rationales=rationales,
        new_calibration_queries=new_calibration_queries,
    )
    out = fstate.forge_proposal_path(sess.state_root, sess.forge_id)
    write_proposal(out, proposal)
    sess.transition(
        ForgePhase.REFORGE_PROPOSAL,
        reason="reforge proposal created",
        meta={"proposal_id": proposal.id,
              "predecessor": sess.weapon_predecessor_id},
    )
    return proposal


def approve(sess: ForgeSession, *, approved_by: str) -> ReforgeProposal:
    """Mark the proposal as approved and transition back to REVIEW.

    The operator then runs ``ara forge seal`` as usual to seal the new
    weapon version.
    """
    if sess.phase != ForgePhase.REFORGE_PROPOSAL:
        raise ReforgeError(
            f"approve requires REFORGE_PROPOSAL "
            f"(current: {sess.phase.value})"
        )
    p = fstate.forge_proposal_path(sess.state_root, sess.forge_id)
    if not p.is_file():
        raise ReforgeError(f"no proposal at {p}")
    proposal = read_proposal(p)
    approved = ReforgeProposal(
        id=proposal.id,
        weapon_predecessor_id=proposal.weapon_predecessor_id,
        forge_id=proposal.forge_id,
        created_at=proposal.created_at,
        triggered_by=proposal.triggered_by,
        component_diffs=proposal.component_diffs,
        rubric_changes=proposal.rubric_changes,
        synonym_changes=proposal.synonym_changes,
        new_calibration_queries=proposal.new_calibration_queries,
        rationales=proposal.rationales,
        applied_at=time.time(),
        approved_by=approved_by,
    )
    write_proposal(p, approved)
    sess.transition(
        ForgePhase.REVIEW, reason="reforge proposal approved",
        meta={"proposal_id": approved.id, "approved_by": approved_by},
    )
    return approved


def get(sess: ForgeSession) -> ReforgeProposal | None:
    """Return the persisted proposal, or ``None`` if there isn't one."""
    p = fstate.forge_proposal_path(sess.state_root, sess.forge_id)
    if not p.is_file():
        return None
    return read_proposal(p)
