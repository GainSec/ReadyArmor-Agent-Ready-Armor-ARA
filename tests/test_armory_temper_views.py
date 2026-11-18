from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from ara import armors as armors_mod
from ara import temper
from ara import temper_lifecycle
from ara import trims as trims_mod
from ara.armory import forge_v1_views
from ara.armory import temper_views
from ara.armory import trim_views
from ara.armory import views


def _mk_armor(state: Path, name: str = "blacksmith") -> None:
    armors_mod.create_armor(state, name, description=f"{name} armor")


def _mk_trim(state: Path, trim_id: str = "task-a") -> None:
    trims_mod.derive_trim(
        state,
        "blacksmith",
        weapon_id="blacksmith-hammer",
        engagement={
            "engagement_id": trim_id,
            "operator_id": "operator",
            "target": {
                "primary": "https://portal.acme.test",
                "scope_allows": ["https://portal.acme.test"],
                "oos": ["https://payments.acme.test"],
                "env": "nonprod",
            },
            "test_window": {
                "start": "2026-06-15T14:00:00Z",
                "end": "2026-06-15T18:00:00Z",
            },
            "delivery_channels": ["discord"],
            "operator_prefs": {"tone": "procedural"},
            "posture": "graybox",
            "active_testing": True,
            "depth": "scan",
        },
        trim_id=trim_id,
        operator_id="operator",
    )


def _mk_forge(state: Path, forge_id: str = "forge-1") -> None:
    fdir = state / "forge" / forge_id
    fdir.mkdir(parents=True)
    (fdir / "session.json").write_text(json.dumps({
        "forge_id": forge_id,
        "weapon_target_id": "blacksmith-hammer",
        "phase": "sealed",
        "armor": "blacksmith",
    }))


def _canary_candidate(**overrides: object) -> dict:
    data = {
        "thread_type": "secret_handling",
        "destination": "canary",
        "title": "Secret disclosure canary",
        "source": "mat",
        "rationale": "Keep responses from leaking raw secrets.",
        "risk": "cleartext secret disclosure",
        "default_decision": "keep_for_review",
        "proposed_text": "Trip on raw secret value disclosure.",
        "control": {
            "signal": "secret-disclosure",
            "pattern": "regex:PRIVATE KEY",
            "severity": "high",
            "trip_when": "agent output includes raw private-key material",
            "response": "record canary hit and alert operator",
        },
        "citations": [{"manifest_element_id": "elem_secret"}],
        "scope": ["global"],
    }
    data.update(overrides)
    return data


def _rhema_candidate(**overrides: object) -> dict:
    data = {
        "thread_type": "prompt_mediation",
        "destination": "rhema",
        "title": "Redact raw key replies",
        "source": "mat",
        "rationale": "Replies should redact key bytes before reaching operators.",
        "risk": "cleartext key disclosure",
        "default_decision": "keep_for_review",
        "proposed_text": "Rewrite replies to redact private-key byte strings.",
        "control": {
            "action": "rewrite",
            "layer": "reply",
            "pattern": "regex:PRIVATE KEY",
            "replacement": "[REDACTED_SECRET]",
            "rationale": "MATS allow presence/risk reporting, not raw values.",
        },
        "citations": [{"manifest_element_id": "elem_key"}],
        "scope": ["global"],
    }
    data.update(overrides)
    return data


def _alert_candidate(**overrides: object) -> dict:
    data = {
        "thread_type": "alert_signal",
        "destination": "alert_hook",
        "title": "Alert on raw key request",
        "source": "mat",
        "rationale": "Operators should see attempts to request raw keys.",
        "risk": "cleartext key disclosure",
        "default_decision": "keep_for_review",
        "proposed_text": "Alert when a prompt asks for raw private-key bytes.",
        "control": {
            "event": "raw_key_request",
            "severity": "high",
            "message": "Prompt requested raw private-key bytes.",
            "route": "courtyard",
        },
        "citations": [{"manifest_element_id": "elem_key"}],
        "scope": ["global"],
    }
    data.update(overrides)
    return data


def test_temper_index_groups_pending_sealed_and_rejected(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path, "task-a")
    _mk_trim(tmp_path, "task-b")
    _mk_trim(tmp_path, "task-c")
    pending = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )
    sealed = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-b"],
    )
    rejected = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-c"],
    )
    temper.seal_proposal(tmp_path, "blacksmith", sealed.proposal_id)
    temper.reject_proposal(tmp_path, "blacksmith", rejected.proposal_id)

    html = temper_views.render_temper_index(tmp_path, "tok")

    assert "page-temper" in html
    assert "temper-proposal-grid" in html
    assert "temper-proposal-card" in html
    assert "Pending" in html and "Sealed" in html and "Rejected" in html
    assert pending.proposal_id in html
    assert sealed.proposal_id in html
    assert rejected.proposal_id in html


def test_temper_create_shows_curated_baseline_not_forge(tmp_path: Path) -> None:
    # forging is retired — the create page is the curated baseline temper.
    html = temper_views.render_temper_create(tmp_path, "tok")

    assert "Curated hardening" in html
    assert "cross-cutting" in html
    assert "Install baseline hardening temper" in html
    assert 'action="/temper/baseline/install?t=tok"' in html
    # no forge/tailor UI
    assert "Create Temper" not in html
    assert "Generation brief" not in html
    assert "/temper/tailor/start" not in html


def test_temper_create_shows_equip_when_installed(tmp_path: Path) -> None:
    from ara import seed_temper
    seed_temper.install_baseline_temper(tmp_path)
    html = temper_views.render_temper_create(tmp_path, "tok")
    assert "Equip baseline hardening" in html
    assert "/tempers/baseline-hardening/equip" in html


def test_handle_install_baseline_materializes_and_no_forge_session(tmp_path: Path) -> None:
    from ara import seed_temper, tempers

    target = temper_views.handle_temper_install_baseline(tmp_path, "tok")

    assert tempers.load_meta(tmp_path, seed_temper.BASELINE_TEMPER_ID) is not None
    assert target.startswith("/temper/create?t=tok")
    assert "installed baseline hardening" in urllib.parse.unquote(target)


def test_handle_temper_tailor_start_is_retired(tmp_path: Path) -> None:
    # the forge entry point no longer starts a session; it redirects with a note.
    target = temper_views.handle_temper_tailor_start(
        tmp_path, {"armor": "hermes-default", "mode": "full_temper"}, "tok")
    assert temper_lifecycle.load_tailor_session(tmp_path) is None   # nothing forged
    assert "retired" in urllib.parse.unquote(target).lower()


def test_handle_temper_tailor_finish_creates_proposal_and_restores(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={
            "armor": "hermes-default",
            "source_weapon": "flocker-v03",
            "source_forge": "forge-123",
        },
        session_id="tailor-ui",
    )
    first = temper_lifecycle.add_tailor_candidate(tmp_path, _alert_candidate())
    second = temper_lifecycle.add_tailor_candidate(tmp_path, _rhema_candidate())
    third = temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate(
        title="Canary on raw key disclosure",
        source="mat",
        rationale="Trip if private-key material appears in output.",
        risk="cleartext key disclosure",
        proposed_text="Trip when output includes raw private-key bytes.",
        citations=[{"manifest_element_id": "elem_key"}],
    ))
    temper_lifecycle.add_tailor_coverage_summary(tmp_path, {
        "sources": [{
            "source_id": "mat",
            "minimum_exception_reason": (
                "UI finish test uses a focused three-candidate fixture"
            ),
            "destinations": {
                "alert_hook": {
                    "status": "emitted",
                    "candidate_ids": [first["candidate"]["candidate_id"]],
                },
                "rhema": {
                    "status": "emitted",
                    "candidate_ids": [second["candidate"]["candidate_id"]],
                },
                "canary": {
                    "status": "emitted",
                    "candidate_ids": [third["candidate"]["candidate_id"]],
                },
                "absolute": {"status": "skipped", "reason": "existing doctrine covers it"},
                "approval_gate": {"status": "skipped", "reason": "no gated action"},
                "bulwark": {"status": "skipped", "reason": "no runtime boundary"},
                "trim_template": {"status": "skipped", "reason": "not task framing"},
            },
        }],
    })

    target = temper_views.handle_temper_tailor_finish(tmp_path, {}, "tok")

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session["status"] == "finished"
    proposals = temper.list_proposals(tmp_path, "hermes-default")
    assert len(proposals) == 1
    assert proposals[0].items[0].destination == "alert_hook"
    assert target.startswith(f"/temper/hermes-default/{proposals[0].proposal_id}?t=tok")


def test_handle_temper_tailor_abort_restores_without_proposal(tmp_path: Path) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-ui",
    )

    target = temper_views.handle_temper_tailor_abort(tmp_path, "tok")

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session["status"] == "aborted"
    assert temper.list_proposals(tmp_path, "hermes-default") == []
    assert target.startswith("/temper/create?t=tok")


def test_temper_detail_renders_sources_items_and_actions(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path,
        "blacksmith",
        source_trims=["task-a"],
        source_weapon="blacksmith-hammer",
        source_forge="forge-1",
        operator_notes="Promote reusable cadence.",
    )

    html = temper_views.render_temper_detail(
        tmp_path, "blacksmith", proposal.proposal_id, "tok",
    )

    assert proposal.proposal_id in html
    assert "blacksmith" in html
    assert "task-a" in html
    assert "blacksmith-hammer" in html
    assert "forge-1" in html
    assert "Doctrine candidate" in html
    assert "Scoped boundary" in html
    assert "Reusable trim fit" in html
    assert "Ingest checked" in html
    assert "Reject" in html


def test_temper_detail_renders_selectable_pending_items(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path,
        "blacksmith",
        source_trims=["task-a"],
        source_weapon="blacksmith-hammer",
        source_forge="forge-1",
        operator_notes="Promote reusable cadence.",
    )

    html = temper_views.render_temper_detail(
        tmp_path, "blacksmith", proposal.proposal_id, "tok",
    )

    assert 'class="temper-select-form"' in html
    assert 'class="temper-digest"' in html
    assert 'class="ara-armor-detail-inline temper-selection-panel"' in html
    assert 'class="temper-select-toolbar"' in html
    assert 'class="temper-select-actions"' in html
    assert 'class="temper-select-grid"' in html
    assert 'name="selected_indexes"' in html
    assert 'data-temper-index="0"' in html
    assert "Proposed entry" in html
    assert "temper-entry-full" in html
    assert "decision" not in html.lower()
    assert "Ingest checked" in html
    assert "Select all" in html
    assert "Clear" in html
    assert "Seal Temper" not in html


def test_temper_detail_cards_explain_methodology_restrictions(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    proposal = temper.TemperProposal(
        proposal_id="temper-methodology",
        target_armor="blacksmith",
        source_trims=["task-a"],
        status="pending",
        items=[
            temper.TemperItem(
                destination="absolute",
                thread_type="methodology_fit",
                title="Web-app methodology boundary",
                source="mat:wstg",
                rationale="Keep web-app work from drifting into firmware work.",
                risk="Agent may use tools unrelated to the active test family.",
                default_decision="keep_for_review",
                proposed_text="For web_app work, firmware tools need context.",
                control={
                    "principle": "methodology_fit",
                    "allowed": ["browser automation", "web port scanners"],
                    "restricted": ["binwalk", "flashrom"],
                    "exception_path": "Require firmware trim or approval.",
                },
                methodology_context={
                    "task_family": "web_app",
                    "expected": ["browser automation", "web port scanners"],
                    "unexpected": ["binwalk", "flashrom"],
                    "exception_path": "Require firmware trim or approval.",
                },
            )
        ],
    )
    temper._write_proposal(tmp_path, proposal)

    html = temper_views.render_temper_detail(
        tmp_path, "blacksmith", proposal.proposal_id, "tok",
    )

    assert "Applies when" in html
    assert "web_app" in html
    assert "Expected here" in html
    assert "browser automation" in html
    assert "Restricted in this context" in html
    assert "binwalk" in html


def test_temper_detail_renders_active_slot_controls(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    trims_mod.apply_trim(tmp_path, "blacksmith", "task-a")
    proposal = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )
    sealed = temper.seal_selected_proposal(
        tmp_path, "blacksmith", proposal.proposal_id, selected_indexes=[0],
    )

    standby = temper_views.render_temper_detail(
        tmp_path, "blacksmith", sealed.proposal_id, "tok",
    )

    assert "Activate Temper slot" in standby
    assert "active trim" in standby
    assert "task-a" in standby

    temper.activate_temper(tmp_path, "blacksmith", sealed.proposal_id)
    active = temper_views.render_temper_detail(
        tmp_path, "blacksmith", sealed.proposal_id, "tok",
    )

    assert "active Temper" in active
    assert "Release Temper slot" in active
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "task-a"


def test_temper_detail_renders_apply_for_sealed_proposal(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )
    temper.seal_proposal(tmp_path, "blacksmith", proposal.proposal_id)

    html = temper_views.render_temper_detail(
        tmp_path, "blacksmith", proposal.proposal_id, "tok",
    )

    assert "Apply Temper" in html


def test_nav_splits_temper_creation_and_review() -> None:
    html = views._nav("temper", "tok")
    create_pos = html.index("Create")
    barracks_pos = html.index("Barracks")
    create_temper_pos = html.index('href="/temper/create?t=tok"')
    review_temper_pos = html.index('href="/temper?t=tok" class="active">Tempers</a>')
    assert create_pos < create_temper_pos < barracks_pos < review_temper_pos
    create_html = views._nav("temper_create", "tok")
    assert 'href="/temper/create?t=tok" class="active">Tailor</a>' in create_html


def test_armor_detail_links_to_temper(tmp_path: Path) -> None:
    _mk_armor(tmp_path)

    html = views.render_armor_detail(tmp_path, "blacksmith", "tok")

    assert html is not None
    assert "Temper Armor" in html
    assert "/temper/create?t=tok&amp;armor=blacksmith" in html


def test_trim_detail_links_to_temper(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)

    html = trim_views.render_trim_detail(tmp_path, "blacksmith", "task-a", "tok")

    assert "Promote to Armor" in html
    assert "/temper/create?t=tok&amp;armor=blacksmith&amp;trim=task-a" in html


def test_forge_detail_links_to_temper(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_forge(tmp_path)

    html = forge_v1_views.render_forge_detail(tmp_path, "forge-1", "tok")

    assert "Use in Tempering" in html
    assert "/temper/create?t=tok&amp;forge=forge-1" in html


def test_temper_detail_shows_ingest_target_and_ingest_writes_bulwark(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    proposal = temper.TemperProposal(
        proposal_id="temper-ingest",
        target_armor="blacksmith",
        source_trims=["task-a"],
        status="pending",
        items=[
            temper.TemperItem(
                destination="bulwark",
                thread_type="runtime_boundary",
                title="Block hardware flash tools",
                source="mat:hw",
                rationale="Debug interface enabled in scope device.",
                risk="Agent may flash/brick hardware.",
                default_decision="keep_for_review",
                proposed_text="deny exec esptool",
                control={
                    "action": "deny", "layer": "exec",
                    "match": "jtag|uart|esptool",
                    "exception_path": "Operator activates hardware trim.",
                },
                citations=[{"manifest_element_id": "elem_hw"}],
            ),
        ],
    )
    temper._write_proposal(tmp_path, proposal)

    html = temper_views.render_temper_detail(
        tmp_path, "blacksmith", "temper-ingest", "tok",
    )
    # the card shows where it lands + that it enforces + the resolved rule
    assert "Lands in" in html
    assert "bulwark.md" in html
    assert "enforced" in html
    assert "deny exec regex:jtag|uart|esptool" in html

    # Ingest the checked entry — writes the temper's OWN policy files and
    # equips it (its own independent slot), merged into live bulwark.md.
    from ara import tempers
    temper_views.handle_temper_ingest_selected(
        tmp_path, "blacksmith", "temper-ingest",
        {"selected_indexes": "0", "temper_name": "Ingest Test"}, "tok",
    )
    assert tempers.is_equipped(tmp_path, "ingest-test")
    own_policy = tempers.bulwark_policy_path(tmp_path, "ingest-test").read_text()
    assert "deny exec regex:jtag|uart|esptool" in own_policy
    assert "# cite: elem_hw" in own_policy
    bulwark_text = (tmp_path / "bulwark.md").read_text()
    assert "ARA-TEMPER-OVERLAY-BEGIN ingest-test" in bulwark_text
    assert "deny exec regex:jtag|uart|esptool" in bulwark_text


def test_temper_equipment_panel_and_equip_unequip(tmp_path: Path) -> None:
    from ara import tempers
    _mk_armor(tmp_path)
    proposal = temper.TemperProposal(
        proposal_id="wstg-pack", target_armor="blacksmith",
        source_trims=[], status="pending",
        items=[temper.TemperItem(
            destination="bulwark", thread_type="runtime_boundary",
            title="Block flash", source="mat", rationale="dbg", risk="brick",
            default_decision="keep_for_review", proposed_text="deny exec esptool",
            control={"action": "deny", "layer": "exec", "match": "jtag|esptool",
                     "exception_path": "op"})],
    )
    temper._write_proposal(tmp_path, proposal)
    # ingest → creates + equips the temper equipment (id derived from name)
    temper_views.handle_temper_ingest_selected(
        tmp_path, "blacksmith", "wstg-pack",
        {"selected_indexes": "0", "temper_name": "WSTG Pack"}, "tok")

    # the equipment panel lists it as equipped and hot-swappable
    html = temper_views.render_temper_review(tmp_path, "tok")
    assert "Temper slot" in html
    assert "wstg-pack" in html
    assert "equipped" in html
    assert "/tempers/wstg-pack/unequip" in html

    # unequip strips the live overlay; re-equip restores it
    temper_views.handle_temper_unequip(tmp_path, "wstg-pack", "tok")
    assert not tempers.is_equipped(tmp_path, "wstg-pack")
    assert "ARA-TEMPER-OVERLAY-BEGIN" not in (tmp_path / "bulwark.md").read_text()
    html2 = temper_views.render_temper_review(tmp_path, "tok")
    assert "/tempers/wstg-pack/equip" in html2  # now offers Equip
    temper_views.handle_temper_equip(tmp_path, "wstg-pack", "tok")
    assert tempers.is_equipped(tmp_path, "wstg-pack")
    assert "deny exec regex:jtag|esptool" in (tmp_path / "bulwark.md").read_text()


def test_ingest_mints_a_new_named_temper_each_run(tmp_path: Path) -> None:
    from ara import tempers
    _mk_armor(tmp_path)
    proposal = temper.TemperProposal(
        proposal_id="run1", target_armor="blacksmith",
        source_trims=[], status="pending",
        items=[temper.TemperItem(
            destination="bulwark", thread_type="runtime_boundary",
            title="Block flash", source="mat", rationale="dbg", risk="brick",
            default_decision="keep_for_review", proposed_text="deny exec esptool",
            control={"action": "deny", "layer": "exec", "match": "jtag|esptool",
                     "exception_path": "op"})],
    )
    temper._write_proposal(tmp_path, proposal)

    # two ingests from the same proposal, same name -> two distinct tempers
    temper_views.handle_temper_ingest_selected(
        tmp_path, "blacksmith", "run1",
        {"selected_indexes": "0", "temper_name": "Hardware Safety"}, "tok")
    temper_views.handle_temper_ingest_selected(
        tmp_path, "blacksmith", "run1",
        {"selected_indexes": "0", "temper_name": "Hardware Safety"}, "tok")

    ids = tempers.list_tempers(tmp_path)
    assert "hardware-safety" in ids and "hardware-safety-2" in ids
    assert tempers.load_meta(tmp_path, "hardware-safety")["name"] == "Hardware Safety"
    # the review page exposes the name field
    html = temper_views.render_temper_detail(tmp_path, "blacksmith", "run1", "tok")
    assert 'name="temper_name"' in html
    assert "Seal selection only" not in html   # legacy button removed
