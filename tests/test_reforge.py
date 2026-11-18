"""Tests for forge.reforge.proposal."""
from __future__ import annotations

import json
from pathlib import Path

from ara.forge.reforge.proposal import (
    ComponentDiff,
    ReforgeProposal,
    build_proposal,
    diff_drafts,
    mint_proposal_id,
    read_proposal,
    summarize,
    write_proposal,
)


def test_mint_proposal_id_format() -> None:
    a = mint_proposal_id()
    assert a.startswith("rp_") and len(a) == 3 + 10


def test_diff_drafts_empty() -> None:
    out = diff_drafts({}, {})
    assert out == []


def test_diff_drafts_added() -> None:
    prior = {"crumbs": []}
    proposed = {"crumbs": [{"crumb_id": "c1", "name": "probe sqli"}]}
    out = diff_drafts(prior, proposed)
    assert len(out) == 1
    assert out[0].bucket == "crumbs"
    assert len(out[0].added) == 1
    assert out[0].added[0]["crumb_id"] == "c1"


def test_diff_drafts_removed() -> None:
    prior = {"crumbs": [{"crumb_id": "c1"}, {"crumb_id": "c2"}]}
    proposed = {"crumbs": [{"crumb_id": "c1"}]}
    out = diff_drafts(prior, proposed)
    assert len(out[0].removed) == 1
    assert out[0].removed[0]["crumb_id"] == "c2"


def test_diff_drafts_modified() -> None:
    prior = {"crumbs": [{"crumb_id": "c1", "name": "old name"}]}
    proposed = {"crumbs": [{"crumb_id": "c1", "name": "new name"}]}
    out = diff_drafts(prior, proposed)
    assert len(out[0].modified) == 1
    assert out[0].modified[0]["before"]["name"] == "old name"
    assert out[0].modified[0]["after"]["name"] == "new name"


def test_diff_drafts_handles_all_buckets() -> None:
    prior = {
        "crumbs": [], "shapes": [], "stores": [],
        "lines": [], "fact_shapes": [], "seed_facts": [],
    }
    proposed = {
        "crumbs": [{"crumb_id": "c1"}],
        "shapes": [{"shape_id": "s1"}],
        "stores": [{"store_id": "st1"}],
        "lines": [{"line_id": "l1"}],
    }
    out = diff_drafts(prior, proposed)
    buckets = {d.bucket for d in out}
    assert buckets == {"crumbs", "shapes", "stores", "lines"}


def test_build_proposal_round_trip(tmp_path: Path) -> None:
    prior = {"crumbs": [{"crumb_id": "c1"}]}
    proposed = {"crumbs": [{"crumb_id": "c1"}, {"crumb_id": "c2"}]}
    p = build_proposal(
        weapon_predecessor_id="wpred",
        forge_id="f1",
        prior_weapon_json=prior,
        proposed_weapon_json=proposed,
        triggered_by="operator",
        rationales={"crumbs:c2": "covered new attack class from updated mat"},
    )
    assert p.weapon_predecessor_id == "wpred"
    assert p.rationales["crumbs:c2"]
    out = tmp_path / "proposal.json"
    write_proposal(out, p)
    p2 = read_proposal(out)
    assert p2.id == p.id
    assert len(p2.component_diffs) == 1
    assert p2.rationales == p.rationales


def test_summarize_counts_buckets() -> None:
    prior = {"crumbs": [{"crumb_id": "c1"}]}
    proposed = {"crumbs": [{"crumb_id": "c1"}, {"crumb_id": "c2"}]}
    p = build_proposal(
        weapon_predecessor_id="w",
        forge_id="f",
        prior_weapon_json=prior,
        proposed_weapon_json=proposed,
    )
    s = summarize(p)
    assert s["crumbs"]["added"] == 1
    assert s["crumbs"]["removed"] == 0


def test_proposal_with_synonym_and_rubric_changes() -> None:
    p = build_proposal(
        weapon_predecessor_id="w",
        forge_id="f",
        prior_weapon_json={},
        proposed_weapon_json={},
        synonym_changes={"sqli": ["sql_injection", "sql injection"]},
        rubric_changes={"PII": {"admin": {"score": 9.5, "name": "critical"}}},
    )
    s = summarize(p)
    assert s.get("synonym_changes") is True
    assert s.get("rubric_changes") is True


def test_proposal_telemetry_recommended_marker() -> None:
    p = build_proposal(
        weapon_predecessor_id="w", forge_id="f",
        prior_weapon_json={}, proposed_weapon_json={"crumbs": [{"crumb_id": "c1"}]},
        triggered_by="telemetry-recommendation",
    )
    assert p.triggered_by == "telemetry-recommendation"


# ---------- driver: propose / approve / get ----------

import pytest
from ara.forge import state as fstate
from ara.forge.phases.synthesize import WeaponDraft, save_draft
from ara.forge.reforge import driver as reforge_driver
from ara.forge.session import ForgeConfig, ForgePhase, ForgeSession
from ara.runtime import audit as raudit


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()


def _seed_predecessor(state_root: Path, weapon_id: str, draft_dict: dict) -> None:
    wdir = fstate.weapon_dir(state_root, weapon_id)
    wdir.mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps(draft_dict))
    (wdir / "seal.json").write_text(json.dumps({
        "weapon_id": weapon_id, "weapon_target_id": weapon_id,
        "weapon_hash": "h" * 64,
    }))


def _session_in_review(state_root: Path, *, predecessor: str | None = "wprior",
                          ) -> ForgeSession:
    sess = ForgeSession.new(
        state_root, weapon_target_id="wnext", operator_id="op",
        weapon_predecessor_id=predecessor,
        config=ForgeConfig(alphabet_pack=""),
    )
    # March through phases without doing real deposits
    sess.transition(ForgePhase.MANIFEST, reason="x")
    sess.transition(ForgePhase.REVIEW, reason="x")
    sess.transition(ForgePhase.EXHAUST, reason="x")
    sess.transition(ForgePhase.REVIEW, reason="x")
    return sess


def test_driver_propose_writes_proposal_and_transitions(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {
        "crumbs": [{"crumb_id": "c1", "name": "old"}],
        "shapes": [], "stores": [], "lines": [],
        "fact_shapes": [], "seed_facts": [], "chunks": [],
    })
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id=sess.weapon_target_id, forge_id=sess.forge_id,
        generated_at=1.0,
        crumbs=[{"crumb_id": "c1", "name": "new"},
                {"crumb_id": "c2", "name": "added"}],
    ))
    proposal = reforge_driver.propose(sess)
    assert proposal.weapon_predecessor_id == "wprior"
    assert sess.phase == ForgePhase.REFORGE_PROPOSAL
    p = fstate.forge_proposal_path(state_root, sess.forge_id)
    assert p.is_file()
    persisted = read_proposal(p)
    # crumbs: 1 added, 1 modified
    by_bucket = {d.bucket: d for d in persisted.component_diffs}
    crumbs = by_bucket["crumbs"]
    assert len(crumbs.added) == 1 and crumbs.added[0]["crumb_id"] == "c2"
    assert len(crumbs.modified) == 1


def test_driver_propose_requires_predecessor(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sess = _session_in_review(state_root, predecessor=None)
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
    ))
    with pytest.raises(reforge_driver.ReforgeError, match="predecessor"):
        reforge_driver.propose(sess)


def test_driver_propose_requires_review(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = ForgeSession.new(
        state_root, weapon_target_id="wnext", operator_id="op",
        weapon_predecessor_id="wprior",
        config=ForgeConfig(alphabet_pack=""),
    )
    # Still in DEPOSIT
    with pytest.raises(reforge_driver.ReforgeError, match="REVIEW"):
        reforge_driver.propose(sess)


def test_driver_propose_requires_draft(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    with pytest.raises(reforge_driver.ReforgeError, match="weapon_draft"):
        reforge_driver.propose(sess)


def test_driver_approve_marks_and_transitions_to_review(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
    ))
    reforge_driver.propose(sess)
    assert sess.phase == ForgePhase.REFORGE_PROPOSAL

    approved = reforge_driver.approve(sess, approved_by="alice")
    assert approved.approved_by == "alice"
    assert approved.applied_at is not None
    assert sess.phase == ForgePhase.REVIEW
    persisted = read_proposal(
        fstate.forge_proposal_path(state_root, sess.forge_id)
    )
    assert persisted.approved_by == "alice"


def test_driver_approve_requires_proposal_phase(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    with pytest.raises(reforge_driver.ReforgeError, match="REFORGE_PROPOSAL"):
        reforge_driver.approve(sess, approved_by="x")


def test_driver_get_returns_none_when_missing(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    assert reforge_driver.get(sess) is None


def test_driver_propose_predecessor_missing(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sess = _session_in_review(state_root, predecessor="wghost")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
    ))
    with pytest.raises(reforge_driver.ReforgeError, match="weapon.json"):
        reforge_driver.propose(sess)


# ---------- CLI ----------

def test_cli_reforge_show_no_proposal(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    rc = main([
        "forge", "--state-root", str(state_root),
        "reforge", "show", sess.forge_id,
    ])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert "no proposal" in out["error"]


def test_cli_reforge_propose_and_approve(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {
        "crumbs": [{"crumb_id": "c1"}],
    })
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
        crumbs=[{"crumb_id": "c1"}, {"crumb_id": "c2"}],
    ))
    rc = main([
        "forge", "--state-root", str(state_root),
        "reforge", "propose", sess.forge_id,
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["weapon_predecessor_id"] == "wprior"
    assert out["crumbs"]["added"] == 1

    rc = main([
        "forge", "--state-root", str(state_root),
        "reforge", "approve", sess.forge_id,
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out.get("applied_at") is not None
    sess2 = ForgeSession.load(state_root, sess.forge_id)
    assert sess2.phase == ForgePhase.REVIEW


def test_cli_reforge_propose_errors_surface_as_rc2(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    state_root = tmp_path / "state"
    sess = ForgeSession.new(
        state_root, weapon_target_id="x", operator_id="op",
        config=ForgeConfig(alphabet_pack=""),
    )
    rc = main([
        "forge", "--state-root", str(state_root),
        "reforge", "propose", sess.forge_id,
    ])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert "error" in out


# ---------- armory UI ----------

def test_armory_renders_proposal_card_pending(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_forge_detail
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {"crumbs": [{"crumb_id": "c1"}]})
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
        crumbs=[{"crumb_id": "c1"}, {"crumb_id": "c2"}],
    ))
    reforge_driver.propose(sess)
    html = render_forge_detail(state_root, sess.forge_id, "tok")
    assert "Reforge proposal" in html
    assert "pending" in html
    assert "wprior" in html
    assert "crumbs" in html
    assert "/proposal/approve" in html


def test_armory_proposal_card_approved_hides_button(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_forge_detail
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
    ))
    reforge_driver.propose(sess)
    reforge_driver.approve(sess, approved_by="alice")
    html = render_forge_detail(state_root, sess.forge_id, "tok")
    assert "approved" in html
    assert "alice" in html
    assert "/proposal/approve" not in html


def test_armory_handle_proposal_approve(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import handle_proposal_approve
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
    ))
    reforge_driver.propose(sess)
    ok, msg = handle_proposal_approve(state_root, sess.forge_id)
    assert ok
    assert "Approved" in msg
    sess2 = ForgeSession.load(state_root, sess.forge_id)
    assert sess2.phase == ForgePhase.REVIEW


def test_armory_handle_proposal_approve_when_missing(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import handle_proposal_approve
    state_root = tmp_path / "state"
    sess = ForgeSession.new(
        state_root, weapon_target_id="x", operator_id="op",
        config=ForgeConfig(alphabet_pack=""),
    )
    ok, msg = handle_proposal_approve(state_root, sess.forge_id)
    assert not ok
    assert "Approve failed" in msg


def test_armory_no_proposal_card_when_absent(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_forge_detail
    state_root = tmp_path / "state"
    sess = ForgeSession.new(
        state_root, weapon_target_id="x", operator_id="op",
        config=ForgeConfig(alphabet_pack=""),
    )
    html = render_forge_detail(state_root, sess.forge_id, "tok")
    assert "Reforge proposal" not in html


# ---------- proposal drill-down ----------

def test_render_proposal_detail_full(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_proposal_detail
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {
        "crumbs": [{"crumb_id": "c1", "name": "old"},
                    {"crumb_id": "c_drop", "name": "removeme"}],
    })
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
        crumbs=[{"crumb_id": "c1", "name": "renamed"},
                {"crumb_id": "c_new", "name": "added"}],
    ))
    reforge_driver.propose(sess)
    html = render_proposal_detail(state_root, sess.forge_id, "tok")
    # Header + back link
    assert "Reforge proposal" in html
    assert "wprior" in html
    assert f"/forge/{sess.forge_id}?t=tok" in html
    # Bucket header with counts
    assert "+1 / −1 / ~1" in html
    # Per-component drill-downs
    assert "Added" in html and "Removed" in html and "Modified" in html
    assert "c_new" in html
    assert "c_drop" in html
    assert "renamed" in html and "old" in html


def test_render_proposal_detail_unknown(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_proposal_detail
    html = render_proposal_detail(tmp_path, "ghost", "tok")
    assert "Proposal not found" in html


def test_render_proposal_detail_with_calibration_queries(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_proposal_detail
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {})
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
    ))
    reforge_driver.propose(
        sess, new_calibration_queries=[
            {"query": "sqli probe", "expected_top_ids": ["c:1", "c:2"],
             "min_precision_at_5": 0.4},
        ],
    )
    html = render_proposal_detail(state_root, sess.forge_id, "tok")
    assert "New calibration queries" in html
    assert "sqli probe" in html
    assert "0.4" in html


def test_proposal_card_links_to_drilldown(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_forge_detail
    state_root = tmp_path / "state"
    _seed_predecessor(state_root, "wprior", {"crumbs": [{"crumb_id": "c1"}]})
    sess = _session_in_review(state_root, predecessor="wprior")
    save_draft(state_root, sess.forge_id, WeaponDraft(
        weapon_target_id="x", forge_id=sess.forge_id, generated_at=1.0,
        crumbs=[{"crumb_id": "c1"}, {"crumb_id": "c2"}],
    ))
    reforge_driver.propose(sess)
    html = render_forge_detail(state_root, sess.forge_id, "tok")
    assert f"/forge/{sess.forge_id}/proposal" in html
    assert "view full diff" in html
