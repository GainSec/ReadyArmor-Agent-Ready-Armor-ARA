from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from ara import armors as armors_mod
from ara import temper
from ara import temper_lifecycle
from ara import trims as trims_mod


def _mk_armor(state: Path, name: str = "blacksmith") -> None:
    armors_mod.create_armor(state, name, description=f"{name} armor")


def _mk_trim(
    state: Path,
    armor: str = "blacksmith",
    trim_id: str = "task-a",
    *,
    target: str = "https://portal.acme.test",
) -> None:
    host = urlparse(target).netloc or target
    engagement = {
        "engagement_id": trim_id,
        "operator_id": "operator",
        "target": {
            "primary": target,
            "scope_allows": [target],
            "oos": ["https://payments.acme.test", f"https://oos.{host}"],
            "env": "nonprod",
        },
        "test_window": {
            "start": "2026-06-15T14:00:00Z",
            "end": "2026-06-15T18:00:00Z",
            "tod_allowed": [{
                "days": ["mon", "tue"],
                "start": "10:00",
                "end": "16:00",
                "tz": "America/New_York",
            }],
        },
        "delivery_channels": ["discord"],
        "operator_prefs": {
            "absolute_enforce": True,
            "tone": "procedural and concise",
        },
        "posture": "graybox",
        "active_testing": True,
        "depth": "scan",
        "rate_limits": {"global_rps": 3},
        "stop_conditions": ["first_critical"],
    }
    trims_mod.derive_trim(
        state,
        armor,
        weapon_id="blacksmith-hammer",
        engagement=engagement,
        trim_id=trim_id,
        operator_id="operator",
    )


def _coverage_for(
    *,
    source_id: str,
    emitted: dict[str, list[str]],
    skipped_reason: str = "Not useful for this source.",
) -> dict:
    destinations = {}
    for destination in temper_lifecycle.TAILOR_CANDIDATE_DESTINATIONS:
        ids = emitted.get(destination, [])
        if ids:
            destinations[destination] = {
                "status": "emitted",
                "candidate_ids": ids,
            }
        else:
            destinations[destination] = {
                "status": "skipped",
                "reason": skipped_reason,
            }
    return {
        "sources": [{
            "source_id": source_id,
            "destinations": destinations,
        }],
        "notes": "Structured coverage for proposal promotion.",
    }


def _canary_candidate(**overrides: object) -> dict:
    data = {
        "thread_type": "secret_handling",
        "destination": "canary",
        "title": "Canary on private key disclosure",
        "source": "mat_usb_firewall_pentest_v1",
        "rationale": "Mats say secret material should be reported by presence.",
        "risk": "Agent may disclose private key bytes while proving a finding.",
        "default_decision": "keep_for_review",
        "proposed_text": "Trip when a reply includes cleartext private key material.",
        "control": {
            "signal": "private-key-disclosure",
            "pattern": "regex:PRIVATE KEY",
            "severity": "high",
            "trip_when": "agent output includes raw private-key material",
            "response": "record canary hit and alert operator",
        },
        "citations": [{"manifest_element_id": "elem_tls_key"}],
        "scope": ["global"],
    }
    data.update(overrides)
    return data


def _bulwark_candidate(**overrides: object) -> dict:
    data = {
        "thread_type": "runtime_boundary",
        "destination": "bulwark",
        "title": "Approval for cleartext credential handling",
        "source": "mat_usb_firewall_pentest_v1",
        "rationale": "Mats allow presence reporting but not secret copying.",
        "risk": "Agent may broaden evidence handling beyond the task.",
        "default_decision": "keep_for_review",
        "proposed_text": "approval reply regex:(private key|password|secret)",
        "control": {
            "action": "approval",
            "layer": "reply",
            "match": "regex:(private key|password|secret)",
            "exception_path": "Allow only after explicit operator approval.",
        },
        "citations": [{"manifest_element_id": "elem_secret_handling"}],
        "scope": ["global"],
    }
    data.update(overrides)
    return data


def _rhema_candidate(**overrides: object) -> dict:
    data = {
        "thread_type": "prompt_mediation",
        "destination": "rhema",
        "title": "Rhema redaction for key material",
        "source": "mat_usb_firewall_pentest_v1",
        "rationale": "Ask/reply mediation can redact key bytes before output.",
        "risk": "Agent may reveal key bytes in a reply.",
        "default_decision": "keep_for_review",
        "proposed_text": "Rewrite replies to redact raw private-key bytes.",
        "control": {
            "action": "rewrite",
            "layer": "reply",
            "pattern": "regex:PRIVATE KEY",
            "replacement": "[REDACTED_SECRET]",
            "rationale": "MATS allow presence/risk reporting, not raw values.",
        },
        "citations": [{"manifest_element_id": "elem_secret_handling"}],
        "scope": ["global"],
    }
    data.update(overrides)
    return data


def test_create_proposal_groups_trim_material_by_destination(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)

    proposal = temper.create_proposal(
        tmp_path,
        "blacksmith",
        source_trims=["task-a"],
        source_weapon="blacksmith-hammer",
        source_forge="forge-1",
        mode="full_temper",
        operator_notes="Keep durable cadence, not the target hostname.",
    )

    assert proposal.status == "pending"
    assert proposal.target_armor == "blacksmith"
    assert proposal.source_trims == ["task-a"]
    assert proposal.source_weapon == "blacksmith-hammer"
    assert proposal.source_forge == "forge-1"
    assert {item.destination for item in proposal.items} >= {
        "absolute",
        "bulwark",
        "trim_template",
    }


def test_create_proposal_rejects_missing_armor(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        temper.create_proposal(tmp_path, "ghost", source_trims=["task-a"])


def test_create_proposal_rejects_no_selected_trims(tmp_path: Path) -> None:
    _mk_armor(tmp_path)

    with pytest.raises(ValueError, match="trim"):
        temper.create_proposal(tmp_path, "blacksmith", source_trims=[])


def test_create_proposal_from_weapon_promotes_forge_material(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    wdir = tmp_path / "weapons" / "flocker-full"
    wdir.mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({
        "weapon_target_id": "flocker-full",
        "forge_id": "forge-full",
        "crumbs": [
            {
                "name": "Hardware assessment procedure",
                "methodology": "MITRE ATT&CK mapping",
                "steps": [{"action": "Map findings to ATT&CK"}],
                "citation": {"manifest_element_id": "elem_proc"},
            }
        ],
        "shapes": [
            {
                "name": "Security rubric",
                "categories": ["debug interfaces", "default credentials"],
                "citation": {"manifest_element_id": "elem_shape"},
            }
        ],
        "fact_shapes": [
            {
                "name": "Device security property",
                "fields": [{"name": "device"}, {"name": "severity"}],
                "citation": {"manifest_element_id": "elem_fact"},
            }
        ],
        "stores": [
            {
                "name": "Tool catalog",
                "entries": [{"tool": "esptool"}],
                "citation": {"manifest_element_id": "elem_store"},
            }
        ],
        "lines": [
            {
                "in_scope": ["Processing Units", "Memory"],
                "out_of_scope": ["Cloud Infrastructure"],
                "citation": {"manifest_element_id": "elem_line"},
            }
        ],
    }))

    proposal = temper.create_proposal_from_weapon(
        tmp_path,
        "hermes-default",
        source_weapon="flocker-full",
        source_forge="forge-full",
        operator_notes="Promote forge output for review.",
    )

    assert proposal.status == "pending"
    assert proposal.source_weapon == "flocker-full"
    assert proposal.source_forge == "forge-full"
    assert proposal.source_trims == []
    assert {item.destination for item in proposal.items} >= {
        "absolute",
        "bulwark",
        "trim_template",
    }
    text = "\n".join(item.proposed_text for item in proposal.items)
    assert "Hardware assessment procedure" in text
    assert "Processing Units" in text
    assert "Cloud Infrastructure" in text
    assert "elem_proc" in text


def test_create_proposal_from_tailor_session_promotes_candidates(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={
            "armor": "hermes-default",
            "source_weapon": "flocker-v03",
            "source_forge": "forge-tailor",
            "mode": "full_temper",
        },
        session_id="tailor-proposal",
    )
    first = temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate(
        proposed_text=(
            "Trip when a reply includes cleartext private key material or "
            "certificate private-key bytes."
        ),
    ))
    second = temper_lifecycle.add_tailor_candidate(tmp_path, _bulwark_candidate())
    third = temper_lifecycle.add_tailor_candidate(tmp_path, _rhema_candidate())
    coverage = _coverage_for(
        source_id="mat_usb_firewall_pentest_v1",
        emitted={
            "canary": [first["candidate"]["candidate_id"]],
            "bulwark": [second["candidate"]["candidate_id"]],
            "rhema": [third["candidate"]["candidate_id"]],
        },
    )
    coverage["sources"][0]["minimum_exception_reason"] = (
        "unit test uses a focused three-candidate fixture"
    )
    temper_lifecycle.add_tailor_coverage_summary(tmp_path, coverage)

    proposal = temper.create_proposal_from_tailor_session(
        tmp_path,
        "hermes-default",
        session_id="tailor-proposal",
        operator_notes="Generated by Tailor/Needle.",
    )

    assert proposal.status == "pending"
    assert proposal.target_armor == "hermes-default"
    assert proposal.source_weapon == "flocker-v03"
    assert proposal.source_forge == "forge-tailor"
    assert proposal.mode == "full_temper"
    assert [item.destination for item in proposal.items] == [
        "canary", "bulwark", "rhema",
    ]
    assert proposal.items[0].citations == [{"manifest_element_id": "elem_tls_key"}]
    loaded = temper.load_proposal(
        tmp_path, "hermes-default", proposal.proposal_id
    )
    assert loaded is not None
    assert loaded.items[1].citations == [
        {"manifest_element_id": "elem_secret_handling"}
    ]
    assert not (tmp_path / "armors" / "hermes-default" / "temper" / "applied").exists()


def test_create_proposal_from_tailor_session_requires_coverage_summary(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-proposal",
    )
    temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate())

    with pytest.raises(ValueError, match="coverage summary"):
        temper.create_proposal_from_tailor_session(
            tmp_path,
            "hermes-default",
            session_id="tailor-proposal",
        )

    assert temper.list_proposals(tmp_path, "hermes-default") == []


def test_create_proposal_from_tailor_session_rejects_incomplete_coverage(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-proposal",
    )
    result = temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate())
    with pytest.raises(temper_lifecycle.TailorLifecycleError, match="coverage"):
        temper_lifecycle.add_tailor_coverage_summary(
            tmp_path,
            {
                "sources": [{
                    "source_id": "mat_usb_firewall_pentest_v1",
                    "destinations": {
                        "canary": {
                            "status": "emitted",
                            "candidate_ids": [result["candidate"]["candidate_id"]],
                        },
                    },
                }],
            },
        )

    assert temper.list_proposals(tmp_path, "hermes-default") == []


def test_create_proposal_from_tailor_session_rejects_destination_mismatch(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-proposal",
    )
    result = temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate())
    coverage = _coverage_for(
        source_id="mat_usb_firewall_pentest_v1",
        emitted={"rhema": [result["candidate"]["candidate_id"]]},
    )
    coverage["sources"][0]["minimum_exception_reason"] = (
        "regression test focuses on destination mismatch"
    )
    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="destination mismatch",
    ):
        temper_lifecycle.add_tailor_coverage_summary(tmp_path, coverage)

    assert temper.list_proposals(tmp_path, "hermes-default") == []


def test_create_proposal_from_tailor_session_rejects_duplicate_coverage_ids(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-proposal",
    )
    first = temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate())
    cid = first["candidate"]["candidate_id"]
    coverage = _coverage_for(
        source_id="mat_usb_firewall_pentest_v1",
        emitted={"canary": [cid, cid]},
    )
    coverage["sources"][0]["minimum_exception_reason"] = (
        "regression test focuses on duplicate ids"
    )
    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="duplicate candidate",
    ):
        temper_lifecycle.add_tailor_coverage_summary(tmp_path, coverage)

    assert temper.list_proposals(tmp_path, "hermes-default") == []


def test_create_proposal_from_tailor_session_rejects_unreferenced_candidate(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-proposal",
    )
    first = temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate())
    omitted = temper_lifecycle.add_tailor_candidate(tmp_path, _canary_candidate(
        title="Canary on bearer-token disclosure",
        proposed_text="Trip when a reply includes cleartext bearer tokens.",
        control={
            "signal": "bearer-token-disclosure",
            "pattern": "regex:Bearer [A-Za-z0-9._-]+",
            "severity": "high",
            "trip_when": "agent output includes raw bearer-token material",
            "response": "record canary hit and alert operator",
        },
    ))
    second = temper_lifecycle.add_tailor_candidate(tmp_path, _bulwark_candidate())
    third = temper_lifecycle.add_tailor_candidate(tmp_path, _rhema_candidate())
    coverage = _coverage_for(
        source_id="mat_usb_firewall_pentest_v1",
        emitted={
            "canary": [first["candidate"]["candidate_id"]],
            "bulwark": [second["candidate"]["candidate_id"]],
            "rhema": [third["candidate"]["candidate_id"]],
        },
    )
    coverage["sources"][0]["minimum_exception_reason"] = (
        "regression test focuses on unreferenced candidate accounting"
    )

    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match=omitted["candidate"]["candidate_id"],
    ):
        temper_lifecycle.add_tailor_coverage_summary(tmp_path, coverage)

    assert temper.list_proposals(tmp_path, "hermes-default") == []


def test_create_proposal_from_tailor_session_rejects_uncited_items(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-proposal",
    )
    path = temper_lifecycle.tailor_session_path(tmp_path)
    record = json.loads(path.read_text())
    record["candidates"] = [{
        "thread_type": "secret_handling",
        "destination": "canary",
        "title": "Uncited canary",
        "source": "manual edit",
        "rationale": "No evidence.",
        "risk": "Would be unreviewable.",
        "default_decision": "keep_for_review",
        "proposed_text": "Trip on something.",
        "control": {
            "signal": "uncited",
            "pattern": "regex:something",
            "severity": "low",
            "trip_when": "uncited output appears",
            "response": "reject proposal",
        },
        "citations": [],
    }]
    path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="citation"):
        temper.create_proposal_from_tailor_session(
            tmp_path,
            "hermes-default",
            session_id="tailor-proposal",
        )

    assert temper.list_proposals(tmp_path, "hermes-default") == []


def test_tailor_proposal_preserves_control_context_and_renders_rhema(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-control",
    )
    result = temper_lifecycle.add_tailor_candidate(tmp_path, {
        "thread_type": "prompt_mediation",
        "destination": "rhema",
        "title": "Redact raw key replies",
        "source": "mat_usb_firewall_pentest_v1",
        "rationale": "Ask/reply mediation can redact key bytes before output.",
        "risk": "Agent may reveal key bytes in a reply.",
        "default_decision": "keep_for_review",
        "proposed_text": "Rewrite replies to redact private-key byte strings.",
        "control": {
            "action": "rewrite",
            "layer": "reply",
            "pattern": "regex:PRIVATE KEY",
            "replacement": "[REDACTED_SECRET]",
            "rationale": "MATS allow presence/risk reporting, not raw values.",
        },
        "methodology_context": {
            "task_family": "web_app",
            "expected": ["HTTP request review"],
            "unexpected": ["private-key disclosure"],
            "requires_context": ["operator approval"],
            "exception_path": "Reveal exact values only after approval.",
        },
        "safety_constraints": {
            "surfaces": ["reply"],
            "allowed": ["presence", "path", "risk"],
            "restricted": ["raw private-key bytes"],
            "requires_context": ["operator approval"],
            "exception_path": "Reveal exact values only after approval.",
        },
        "citations": [{"manifest_element_id": "elem_secret_handling"}],
        "scope": ["global"],
    })
    coverage = _coverage_for(
        source_id="mat_usb_firewall_pentest_v1",
        emitted={"rhema": [result["candidate"]["candidate_id"]]},
    )
    coverage["sources"][0]["minimum_exception_reason"] = (
        "unit test focuses on Rhema control preservation"
    )
    temper_lifecycle.add_tailor_coverage_summary(tmp_path, coverage)

    proposal = temper.create_proposal_from_tailor_session(
        tmp_path,
        "hermes-default",
        session_id="tailor-control",
    )

    item = proposal.items[0]
    assert item.thread_type == "prompt_mediation"
    assert item.control["action"] == "rewrite"
    assert item.methodology_context["task_family"] == "web_app"
    assert item.safety_constraints["surfaces"] == ["reply"]

    loaded = temper.load_proposal(tmp_path, "hermes-default", proposal.proposal_id)
    assert loaded is not None
    assert loaded.items[0].control["replacement"] == "[REDACTED_SECRET]"

    temper.seal_proposal(tmp_path, "hermes-default", proposal.proposal_id)
    applied = temper.apply_proposal(
        tmp_path,
        "hermes-default",
        proposal.proposal_id,
    )

    assert applied.items[0].applied_path.endswith("rhema.md")
    rhema_text = (tmp_path / "rhema.md").read_text()
    assert "rewrite reply regex:PRIVATE KEY" in rhema_text
    assert "# replacement: [REDACTED_SECRET]" in rhema_text
    assert "# cite: elem_secret_handling" in rhema_text


def test_create_proposal_rejects_cross_armor_trim(tmp_path: Path) -> None:
    _mk_armor(tmp_path, "blacksmith")
    _mk_armor(tmp_path, "scout")
    _mk_trim(tmp_path, "scout", "task-a")

    with pytest.raises(FileNotFoundError):
        temper.create_proposal(tmp_path, "blacksmith", source_trims=["task-a"])


def test_create_proposal_sanitizes_task_specific_target_from_durable_text(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path, target="https://portal.secret-client.example")

    proposal = temper.create_proposal(
        tmp_path,
        "blacksmith",
        source_trims=["task-a"],
        mode="full_temper",
    )

    durable = "\n".join(
        item.proposed_text
        for item in proposal.items
        if item.destination in {"absolute", "bulwark", "rhema", "meta"}
    )
    assert "portal.secret-client.example" not in durable
    assert r"portal\.secret-client\.example" not in durable
    assert "payments.acme.test" not in durable
    assert "oos.[task-target]" not in durable
    assert r"portal\.[task-target]" not in durable
    assert "v0.1" in durable
    assert "[task-target]" in durable


def test_apply_requires_sealed_proposal(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )

    with pytest.raises(temper.TemperStateError):
        temper.apply_proposal(tmp_path, "blacksmith", proposal.proposal_id)


def test_seal_selected_proposal_creates_filtered_sealed_copy(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path,
        "blacksmith",
        source_trims=["task-a"],
        source_weapon="blacksmith-hammer",
        source_forge="forge-1",
    )

    sealed = temper.seal_selected_proposal(
        tmp_path,
        "blacksmith",
        proposal.proposal_id,
        selected_indexes=[0, 2],
        operator="ops",
    )

    assert sealed.status == "sealed"
    assert sealed.sealed_by == "ops"
    assert sealed.proposal_id != proposal.proposal_id
    assert sealed.target_armor == "blacksmith"
    assert sealed.source_trims == ["task-a"]
    assert sealed.source_weapon == "blacksmith-hammer"
    assert sealed.source_forge == "forge-1"
    assert [item.title for item in sealed.items] == [
        proposal.items[0].title,
        proposal.items[2].title,
    ]
    assert "Selected 2 of" in sealed.operator_notes
    original = temper.load_proposal(tmp_path, "blacksmith", proposal.proposal_id)
    assert original is not None
    assert original.status == "pending"
    assert len(original.items) == len(proposal.items)


def test_seal_selected_proposal_rejects_empty_selection(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )

    with pytest.raises(temper.TemperStateError, match="select at least one"):
        temper.seal_selected_proposal(
            tmp_path, "blacksmith", proposal.proposal_id, selected_indexes=[],
        )


def test_active_temper_slot_is_independent_from_active_trim(
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

    activated = temper.activate_temper(
        tmp_path, "blacksmith", sealed.proposal_id,
    )

    assert activated["proposal_id"] == sealed.proposal_id
    assert temper.active_temper_id(tmp_path, "blacksmith") == sealed.proposal_id
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "task-a"

    released = temper.release_temper(tmp_path, "blacksmith", sealed.proposal_id)

    assert released["released"] is True
    assert temper.active_temper_id(tmp_path, "blacksmith") is None
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "task-a"


def test_activate_temper_materializes_reversible_runtime_overlays(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    trims_mod.apply_trim(tmp_path, "blacksmith", "task-a")
    armor = armors_mod.load_armor(tmp_path, "blacksmith")
    assert armor is not None
    armor.absolute_path.write_text("# Existing doctrine\nKeep this.\n")
    (tmp_path / "bulwark.md").write_text("# Existing policy\nallow net localhost:443\n")
    (tmp_path / "rhema.md").write_text("# Existing mediation\n")
    proposal = temper.TemperProposal(
        proposal_id="temper-runtime",
        target_armor="blacksmith",
        source_trims=["task-a"],
        status="sealed",
        sealed_by="operator",
        items=[
            temper.TemperItem(
                destination="absolute",
                thread_type="safety_boundary",
                title="Evidence value boundary",
                source="mat:wstg",
                rationale="Keep proof bounded to safe evidence.",
                risk="Agent may disclose raw secrets.",
                default_decision="keep_for_review",
                proposed_text="Do not disclose raw secret values.",
                control={
                    "principle": "Report secret presence without raw value disclosure.",
                    "allowed": ["presence", "path", "risk"],
                    "restricted": ["raw secret values"],
                    "exception_path": "Require explicit operator approval.",
                },
            ),
            temper.TemperItem(
                destination="bulwark",
                thread_type="runtime_boundary",
                title="Approval for secret file reads",
                source="mat:wstg",
                rationale="Secret file reads need operator confirmation.",
                risk="Agent may read outside task evidence needs.",
                default_decision="keep_for_review",
                proposed_text="approval fs /etc/**",
                control={
                    "action": "approval",
                    "layer": "fs",
                    "match": "/etc/**",
                    "exception_path": "Allow only after operator approval.",
                },
            ),
            temper.TemperItem(
                destination="rhema",
                thread_type="prompt_mediation",
                title="Redact private-key replies",
                source="mat:wstg",
                rationale="Reply mediation can redact raw key material.",
                risk="Agent may reveal key bytes.",
                default_decision="keep_for_review",
                proposed_text="Rewrite private key bytes.",
                control={
                    "action": "rewrite",
                    "layer": "reply",
                    "pattern": "regex:PRIVATE KEY",
                    "replacement": "[REDACTED_SECRET]",
                    "rationale": "Report presence, not raw values.",
                },
            ),
            temper.TemperItem(
                destination="canary",
                thread_type="secret_handling",
                title="Private key canary",
                source="mat:wstg",
                rationale="Secret material should trip canaries.",
                risk="Agent may disclose raw keys.",
                default_decision="keep_for_review",
                proposed_text="Trip on raw private key material.",
                control={
                    "signal": "private-key-disclosure",
                    "pattern": "regex:PRIVATE KEY",
                    "severity": "high",
                    "trip_when": "agent output includes raw private-key material",
                    "response": "record canary hit and alert operator",
                },
            ),
        ],
    )
    temper._write_proposal(tmp_path, proposal)

    activated = temper.activate_temper(tmp_path, "blacksmith", "temper-runtime")

    assert activated["proposal_id"] == "temper-runtime"
    assert temper.active_temper_id(tmp_path, "blacksmith") == "temper-runtime"
    absolute_text = armor.absolute_path.read_text()
    bulwark_text = (tmp_path / "bulwark.md").read_text()
    rhema_text = (tmp_path / "rhema.md").read_text()
    assert "# Existing doctrine" in absolute_text
    assert "# Existing policy" in bulwark_text
    assert "# Existing mediation" in rhema_text
    assert "ARA-ACTIVE-TEMPER-ABSOLUTE-BEGIN blacksmith temper-runtime" in absolute_text
    assert "## Tempered Armor:" in absolute_text
    assert "## Tempered armor principle" not in absolute_text
    assert "Report secret presence without raw value disclosure." in absolute_text
    assert "ARA-ACTIVE-TEMPER-BULWARK-BEGIN blacksmith temper-runtime" in bulwark_text
    assert "approval fs /etc/**" in bulwark_text
    # rhema + canary now route into bulwark.md as content-layer rules — not
    # into rhema.md / inert artifacts.
    assert "rewrite reply regex:PRIVATE KEY" in bulwark_text
    assert "deny highlight reply regex:PRIVATE KEY" in bulwark_text
    assert "ARA-ACTIVE-TEMPER-RHEMA-BEGIN" not in rhema_text
    active_dir = tmp_path / "armors" / "blacksmith" / "temper" / "active" / "temper-runtime"
    assert not (active_dir / "canary").exists()

    released = temper.release_temper(tmp_path, "blacksmith", "temper-runtime")

    assert released["released"] is True
    assert temper.active_temper_id(tmp_path, "blacksmith") is None
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "task-a"
    assert "ARA-ACTIVE-TEMPER-ABSOLUTE-BEGIN" not in armor.absolute_path.read_text()
    assert "ARA-ACTIVE-TEMPER-BULWARK-BEGIN" not in (tmp_path / "bulwark.md").read_text()
    assert "ARA-ACTIVE-TEMPER-RHEMA-BEGIN" not in (tmp_path / "rhema.md").read_text()
    assert "# Existing doctrine" in armor.absolute_path.read_text()
    assert "# Existing policy" in (tmp_path / "bulwark.md").read_text()
    assert "# Existing mediation" in (tmp_path / "rhema.md").read_text()
    assert not active_dir.exists()


def _absolute_temper_item(
    title: str,
    *,
    task_family: str | None = None,
    scope: list[str] | None = None,
) -> temper.TemperItem:
    methodology_context = {}
    if task_family:
        methodology_context = {
            "task_family": task_family,
            "expected": ["expected task work"],
            "exception_path": "operator approval",
        }
    return temper.TemperItem(
        destination="absolute",
        thread_type="methodology_fit",
        title=title,
        source="mat:wstg",
        rationale=f"{title} rationale",
        risk=f"{title} risk",
        default_decision="keep_for_review",
        proposed_text=f"{title} proposed text",
        scope=list(scope or []),
        methodology_context=methodology_context,
        control={
            "principle": title,
            "allowed": ["task-local evidence"],
            "restricted": [f"{title} restricted action"],
            "exception_path": "operator approval",
        },
    )


def test_project_active_temper_for_task_filters_absolute_by_task_family(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path, target="https://portal.acme.test")
    trims_mod.apply_trim(tmp_path, "blacksmith", "task-a")
    armor = armors_mod.load_armor(tmp_path, "blacksmith")
    assert armor is not None
    armor.absolute_path.write_text("# Base oath\nKeep the base.\n")
    proposal = temper.TemperProposal(
        proposal_id="temper-mixed",
        target_armor="blacksmith",
        source_trims=["task-a"],
        status="sealed",
        sealed_by="operator",
        items=[
            _absolute_temper_item("Web-app boundary", task_family="web_app"),
            _absolute_temper_item("Hardware firmware boundary", task_family="hardware"),
            _absolute_temper_item("iOS mobile boundary"),
            _absolute_temper_item("Universal secret boundary", scope=["global"]),
        ],
    )
    temper._write_proposal(tmp_path, proposal)
    temper.activate_temper(tmp_path, "blacksmith", "temper-mixed")
    assert "Hardware firmware boundary" in armor.absolute_path.read_text()

    projected = temper.project_active_temper_for_task(
        tmp_path,
        "blacksmith",
        "task-web",
        trim_id="task-a",
        weapon_id="flocker-hermes-full-web",
    )

    projected_text = Path(projected["absolute_path"]).read_text()
    assert "# Base oath" in projected_text
    assert "Web-app boundary" in projected_text
    assert "Universal secret boundary" in projected_text
    assert "Hardware firmware boundary" not in projected_text
    assert "iOS mobile boundary" not in projected_text
    assert projected["selected"]["absolute"] == 2
    assert projected["excluded"]["absolute"] == 2


def test_apply_appends_absolute_and_bulwark_without_replacing(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    armor = armors_mod.load_armor(tmp_path, "blacksmith")
    assert armor is not None
    armor.absolute_path.write_text("# Existing doctrine\nKeep this.\n")
    (tmp_path / "bulwark.md").write_text("# Existing policy\nallow net localhost:443\n")

    proposal = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )
    temper.seal_proposal(tmp_path, "blacksmith", proposal.proposal_id)
    applied = temper.apply_proposal(
        tmp_path,
        "blacksmith",
        proposal.proposal_id,
        operator="operator",
        apply_mode="apply_next_task",
    )

    absolute_text = armor.absolute_path.read_text()
    bulwark_text = (tmp_path / "bulwark.md").read_text()
    assert applied.status == "applied"
    assert "# Existing doctrine" in absolute_text
    assert "ARA-TEMPER-ABSOLUTE-BEGIN" in absolute_text
    assert "# Existing policy" in bulwark_text
    assert "scope: armor:blacksmith" in bulwark_text
    assert "ARA-TEMPER-BULWARK-BEGIN blacksmith" in bulwark_text


def test_apply_writes_trim_template_draft(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )
    temper.seal_proposal(tmp_path, "blacksmith", proposal.proposal_id)

    applied = temper.apply_proposal(tmp_path, "blacksmith", proposal.proposal_id)

    template_dir = (
        tmp_path / "armors" / "blacksmith" / "temper" / "trim-templates"
    )
    assert applied.status == "applied"
    assert any(template_dir.glob(f"{proposal.proposal_id}-*.md"))


def test_apply_rejects_already_applied_proposal(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _mk_trim(tmp_path)
    proposal = temper.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )
    temper.seal_proposal(tmp_path, "blacksmith", proposal.proposal_id)
    temper.apply_proposal(tmp_path, "blacksmith", proposal.proposal_id)

    with pytest.raises(temper.TemperStateError):
        temper.apply_proposal(tmp_path, "blacksmith", proposal.proposal_id)


def test_sanitize_collapses_regex_fragments_around_task_target() -> None:
    meta = trims_mod.TrimMeta(
        trim_id="task-a",
        armor="blacksmith",
        target="https://deploy-check.example",
    )

    text = r"deny url regex:^https?://(?!(?:deploy\-check\.example)(?::\d+)?(?:/|$)).*$"
    sanitized = temper._sanitize_task_specific(text, meta)

    assert "deploy" not in sanitized
    assert r"\-[task-target]" not in sanitized
    assert sanitized.count("[task-target]") == 1


def test_create_proposal_from_tailor_session_dedups_near_identical_items(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "hermes-default")
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={
            "armor": "hermes-default",
            "source_weapon": "flocker-v03",
            "source_forge": "forge-tailor",
            "mode": "full_temper",
        },
        session_id="tailor-dedup",
    )
    # Two canaries with different titles but the same signal+pattern signature.
    a = temper_lifecycle.add_tailor_candidate(
        tmp_path, _canary_candidate(title="Canary A"))
    b = temper_lifecycle.add_tailor_candidate(
        tmp_path, _canary_candidate(title="Canary B (duplicate signature)"))
    coverage = _coverage_for(
        source_id="mat_usb_firewall_pentest_v1",
        emitted={"canary": [
            a["candidate"]["candidate_id"],
            b["candidate"]["candidate_id"],
        ]},
    )
    coverage["sources"][0]["minimum_exception_reason"] = "focused dedup fixture"
    temper_lifecycle.add_tailor_coverage_summary(tmp_path, coverage)

    proposal = temper.create_proposal_from_tailor_session(
        tmp_path, "hermes-default", session_id="tailor-dedup",
    )
    canaries = [i for i in proposal.items if i.destination == "canary"]
    assert len(canaries) == 1                      # near-identical collapsed
    assert "deduped" in proposal.operator_notes    # surfaced, not silent


def test_seal_blocks_proposal_with_malformed_control(tmp_path: Path) -> None:
    _mk_armor(tmp_path, "hermes-default")
    pdir = tmp_path / "armors" / "hermes-default" / "temper" / "proposals"
    pdir.mkdir(parents=True)
    bad = temper.TemperProposal(
        proposal_id="temper-bad",
        target_armor="hermes-default",
        source_trims=[],
        status="pending",
        items=[temper.TemperItem(
            destination="rhema",
            title="dead pattern",
            source="mat",
            rationale="x",
            risk="y",
            default_decision="keep_for_review",
            proposed_text="z",
            control={
                "action": "rewrite", "layer": "reply",
                "pattern": "exploit_payload",  # dead label → over-broad/label
                "replacement": "[X]", "rationale": "r",
            },
            citations=[{"manifest_element_id": "e"}],
        )],
    )
    temper._write_proposal(tmp_path, bad)
    with pytest.raises(temper.TemperStateError, match="seal blocked"):
        temper.seal_proposal(tmp_path, "hermes-default", "temper-bad")
