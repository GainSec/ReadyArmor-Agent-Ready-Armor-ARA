"""Tests for runtime.findings."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext, ChannelRejection
from ara.runtime.facts.channels import install_fact_channels
from ara.runtime.findings.alphabets import (
    Alphabets,
    CategoryEntry,
    CategoryEnum,
    FixLibrary,
    FixTemplate,
    RubricCell,
    SeverityRubric,
    load_alphabets,
    write_category_enum,
    write_fix_library,
    write_severity_rubric,
)
from ara.runtime.findings.channels import install_finding_channels
from ara.runtime.findings.record import (
    Finding,
    compute_content_hash,
    mint_finding_id,
    severity_name_from_score,
)
from ara.runtime.findings.store import FindingStore
from ara.runtime.leads.channels import install_lead_channels


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


# ---------- record ----------

def test_severity_buckets() -> None:
    assert severity_name_from_score(0.5) == "info"
    assert severity_name_from_score(2.5) == "low"
    assert severity_name_from_score(5.0) == "medium"
    assert severity_name_from_score(8.0) == "high"
    assert severity_name_from_score(9.5) == "critical"


def test_finding_id_format() -> None:
    a = mint_finding_id()
    assert a.startswith("F:") and len(a) == 12


def test_finding_round_trip() -> None:
    f = Finding(
        id="F:abcdef0123", title="T", category="xss.reflected",
        severity_cell=("PII", "read"), severity_score=6.0,
        severity_name="medium", fix_template_id=None,
        fix_custom={"what_changed": "x", "why_it_fixes": "y",
                    "verification_method": "z"},
        evidence_refs=[], fact_refs=[], lead_refs=[],
        provenance_chain=["c1"], impact={"confidentiality": 1, "integrity": 0,
                                          "availability": 0},
        affected_assets=[], origin="agent_derived",
        status="draft", activation_id="act-1", created_at=time.time(),
    )
    f2 = Finding.from_jsonable(f.to_jsonable())
    assert f2.id == f.id
    assert f2.severity_cell == f.severity_cell


def test_content_hash_changes_on_mutation() -> None:
    f = Finding(
        id="F:abcdef0123", title="T", category="xss.reflected",
        severity_cell=("PII", "read"), severity_score=6.0, severity_name="medium",
        fix_template_id=None, fix_custom={"verification_method": "x"},
        evidence_refs=[], fact_refs=[], lead_refs=[],
        provenance_chain=["c1"],
        impact={"confidentiality": 1, "integrity": 0, "availability": 0},
        affected_assets=[], origin="agent_derived", status="draft",
        activation_id="a", created_at=1.0,
    )
    h1 = compute_content_hash(f)
    from dataclasses import replace
    f2 = replace(f, title="T2")
    assert compute_content_hash(f2) != h1


# ---------- alphabets ----------

def _seed_alphabets(weapon_dir: Path) -> None:
    cat = CategoryEnum(entries={
        "xss.reflected": CategoryEntry(id="xss.reflected", label="Reflected XSS"),
        "sqli": CategoryEntry(id="sqli"),
        "other": CategoryEntry(id="other", requires_proposed=True),
    })
    rubric = SeverityRubric(
        data_classes=["public_data", "PII", "credentials"],
        accesses=["read", "write", "admin"],
        matrix={
            "public_data": {
                "read": RubricCell(1.5, "low"),
                "write": RubricCell(3.0, "low"),
            },
            "PII": {
                "read": RubricCell(6.0, "medium"),
                "write": RubricCell(8.0, "high"),
            },
            "credentials": {
                "admin": RubricCell(9.5, "critical"),
            },
        },
    )
    fixes = FixLibrary(templates={
        "fix.xss.encode_output": FixTemplate(
            id="fix.xss.encode_output", title="Encode output",
            applicable_categories=["xss.reflected", "xss.stored"],
            body="encode at boundary",
        ),
    })
    write_category_enum(weapon_dir / "alphabets" / "category_enum.json", cat)
    write_severity_rubric(weapon_dir / "alphabets" / "severity_rubric.json", rubric)
    write_fix_library(weapon_dir / "alphabets" / "fix_library.json", fixes)


def test_alphabets_round_trip(tmp_path: Path) -> None:
    _seed_alphabets(tmp_path)
    a = load_alphabets(tmp_path)
    assert a.categories.has("xss.reflected")
    assert a.categories.is_other("other") is True
    assert a.rubric.has_cell("PII", "read")
    cell = a.rubric.lookup("PII", "read")
    assert cell is not None and cell.score == 6.0
    assert a.fix_library.applicable("fix.xss.encode_output", "xss.reflected")


# ---------- channels ----------

def _ctx(audit: Path, *, eng="eng-1", act="act-1", caller="agent") -> ChannelContext:
    return ChannelContext(
        engagement_id=eng, activation_id=act, caller=caller, audit_sink=audit,
    )


def _new_session(tmp_path: Path) -> tuple[Path, Path, Path]:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    _seed_alphabets(weapon_dir)
    return sessions_root, eng_dir, weapon_dir


def _put_evidence_in_activation(eng_dir: Path, activation_id: str = "act-1") -> str:
    """Helper: put evidence tagged to the test's standard activation_id
    so EVIDENCE_FRESHNESS_RULE passes."""
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(
        b"x", source="tool:curl", source_metadata={}, summary="s",
        activation_id=activation_id,
    )
    return rec.id


def _put_bytes(eng_dir: Path, data: bytes, activation_id: str = "act-1") -> str:
    """Put custom evidence bytes tagged to the current activation."""
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(data, source="tool:curl", source_metadata={},
                     summary="s", activation_id=activation_id)
    return rec.id


# ---------- natural-finding intake (LLM emits natural, not structured) ----------

def test_natural_finding_auto_derive_accepts(tmp_path: Path) -> None:
    """A finding with only title/severity-word/description/confirmation_method
    + cited evidence (no severity_cell/impact/provenance/markers) files, and
    verification markers are auto-derived from the prose and hold."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    ev = _put_bytes(eng_dir, b"GET /api/v1/users HTTP/1.1\n\nHTTP/1.1 200 OK\n"
                            b'[{"email":"alice@corp.com","role":"admin"}]')
    chans = install_finding_channels(sessions_root=sessions_root, weapon_dir=weapon_dir)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    body = {
        "title": "Unauthenticated user data exposure",
        "severity": "high",
        "description": "The /api/v1/users endpoint leaks user emails.",
        "confirmation_method": "Unauthenticated GET /api/v1/users returned 200 "
                               "with email addresses.",
        "evidence_refs": [ev],
        "foo_stray_field": "should be tolerated/stripped",
    }
    res = chans["finding.file"].call(body, ctx_a)
    assert res["finding"]["status"] == "draft"
    assert res["finding"]["severity_name"] == "high"
    # the prose confirmation is persisted as the verification claim
    assert res["finding"]["verification"]["claim"].startswith("Unauthenticated GET")


def test_natural_finding_auto_derive_refutes_login_form(tmp_path: Path) -> None:
    """The /admin false positive: claim says unauthenticated, but the cited
    evidence is a login form -> mechanically REFUTED."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    ev = _put_bytes(eng_dir, b'<html><body>Please log in'
                            b'<form><input type="password"></form></body></html>')
    chans = install_finding_channels(sessions_root=sessions_root, weapon_dir=weapon_dir)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    body = {
        "title": "Unauthenticated admin dashboard",
        "severity": "critical",
        "confirmation_method": "Unauthenticated access to /admin returned the "
                               "admin dashboard.",
        "evidence_refs": [ev],
    }
    with pytest.raises(ChannelRejection, match="CLAIM_VERIFICATION_REFUTED"):
        chans["finding.file"].call(body, ctx_a)


def test_natural_finding_auto_derive_tokens_absent(tmp_path: Path) -> None:
    """When none of the claim's distinctive tokens appear in the evidence, the
    finding is rejected (the evidence doesn't demonstrate the claim)."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    ev = _put_bytes(eng_dir, b"totally unrelated homepage content")
    chans = install_finding_channels(sessions_root=sessions_root, weapon_dir=weapon_dir)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    body = {
        "title": "Secret endpoint exposure",
        "severity": "high",
        "confirmation_method": "The endpoint /secret/api/keys returned secrets.",
        "evidence_refs": [ev],
    }
    with pytest.raises(ChannelRejection, match="CLAIM_VERIFICATION_FAILED"):
        chans["finding.file"].call(body, ctx_a)


def test_natural_finding_requires_evidence(tmp_path: Path) -> None:
    """A natural finding with a claim but NO evidence is rejected — the
    mechanical core (evidence-backed) stays strict."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    chans = install_finding_channels(sessions_root=sessions_root, weapon_dir=weapon_dir)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    body = {
        "title": "Claim with no evidence",
        "severity": "medium",
        "confirmation_method": "I saw something at /x.",
        "evidence_refs": [],
    }
    with pytest.raises(ChannelRejection,
                       match="CLAIM_VERIFICATION_NO_EVIDENCE|ORIGIN_AGENT"):
        chans["finding.file"].call(body, ctx_a)


def _basic_body(ev_id: str, **over) -> dict:
    body = dict(
        title="Reflected XSS in /search",
        category="xss.reflected",
        severity_cell=["PII", "read"],
        impact={"confidentiality": 2, "integrity": 0, "availability": 0},
        evidence_refs=[ev_id],
        provenance_chain=["c1"],
        fix_template_id="fix.xss.encode_output",
        origin="agent_derived",
        lead_refs=[],
        # CLAIM_VERIFICATION: the standard test evidence is the byte "x"
        # (see _put_evidence_in_activation / binder.put(b"x", ...)), so a
        # must_contain marker of "x" is demonstrated by the cited evidence.
        verification={"must_contain": ["x"]},
    )
    body.update(over)
    return body


def test_finding_file_happy_path(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    # Need a lead_ref since origin=agent_derived requires it
    lead_chans = registry.all_channels()
    lead_file = next(c for c in lead_chans if c.name == "lead.file")
    out = lead_file.call({
        "title": "lead", "reasoning": "r", "evidence_refs": [rec.id],
    }, ctx_a)
    body = _basic_body(rec.id, lead_refs=[out["lead"]["id"]])
    res = chans["finding.file"].call(body, ctx_a)
    assert res["finding"]["status"] == "draft"
    assert res["finding"]["severity_score"] == 6.0
    assert res["finding"]["severity_name"] == "medium"


def test_gate_rejects_command_only_evidence(tmp_path: Path) -> None:
    """Primary-evidence gate: a finding whose activation captured NO response-
    bearing evidence (only a command-only shim:spawn — the F:95b00a3e88 defect)
    is rejected; auto-correlation cannot rescue it, so the gate fires."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    # response-bearing evidence exists only in act-1 (for the lead's own gate)
    okev = binder.put(b"response body x", source="tool:terminal", source_metadata={},
                      summary="out", activation_id="act-1")
    # the finding runs in act-2, which has ONLY a command-only record
    spawn = binder.put(b"x", source="shim:spawn", source_metadata={"command": "c"},
                       summary="cmd", activation_id="act-2")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(sessions_root=sessions_root, weapon_dir=weapon_dir)
    ctx1 = _ctx(eng_dir / "events.jsonl", act="act-1")
    ctx2 = _ctx(eng_dir / "events.jsonl", act="act-2")
    lead_file = next(c for c in registry.all_channels() if c.name == "lead.file")
    lead = lead_file.call({"title": "l", "reasoning": "r", "evidence_refs": [okev.id]}, ctx1)
    # finding filed in act-2 cites only the command-only record; no response-
    # bearing evidence in act-2 for auto-correlation to bind → gate rejects.
    body = _basic_body(spawn.id, lead_refs=[lead["lead"]["id"]])
    with pytest.raises(ChannelRejection, match="PRIMARY_EVIDENCE_RESPONSE_BEARING"):
        chans["finding.file"].call(body, ctx2)


def test_gate_autocorrelates_primary_from_activation(tmp_path: Path) -> None:
    """The safe path: a finding citing a command-only record still files when the
    activation captured a response-bearing record — enrichment auto-binds it."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    spawn = binder.put(b"x", source="shim:spawn", source_metadata={"command": "c"},
                       summary="cmd", activation_id="act-1")
    # response-bearing output captured in the SAME activation (must contain "x")
    binder.put(b"STATUS: 200 body x", source="tool:terminal", source_metadata={},
               summary="out", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(sessions_root=sessions_root, weapon_dir=weapon_dir)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_file = next(c for c in registry.all_channels() if c.name == "lead.file")
    lead = lead_file.call({"title": "l", "reasoning": "r", "evidence_refs": [spawn.id]}, ctx_a)
    body = _basic_body(spawn.id, lead_refs=[lead["lead"]["id"]])
    res = chans["finding.file"].call(body, ctx_a)
    assert res["finding"]["status"] == "draft"
    # the auto-correlated response-bearing primary is now bound to the finding
    from ara.runtime.binder import sources as _srcs
    srcs = {EvidenceBinder(eng_dir).get_record(r).source
            for r in res["finding"]["evidence_refs"]}
    assert any(_srcs.is_response_bearing(s) for s in srcs)


def test_gate_accepts_when_a_response_bearing_ref_present(tmp_path: Path) -> None:
    """Same finding also citing a response-bearing tool:* record passes the gate."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    spawn = binder.put(b"the command line", source="shim:spawn",
                       source_metadata={"command": "c"}, summary="cmd", activation_id="act-1")
    # distinct content (must contain "x" for the _basic_body claim marker)
    out = binder.put(b"STATUS: 200\nresponse body x", source="tool:terminal",
                     source_metadata={}, summary="out", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(sessions_root=sessions_root, weapon_dir=weapon_dir)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_file = next(c for c in registry.all_channels() if c.name == "lead.file")
    lead = lead_file.call({"title": "l", "reasoning": "r", "evidence_refs": [out.id]}, ctx_a)
    body = _basic_body(spawn.id, evidence_refs=[spawn.id, out.id],
                       lead_refs=[lead["lead"]["id"]])
    res = chans["finding.file"].call(body, ctx_a)
    assert res["finding"]["status"] == "draft"


def test_finding_file_returns_all_validator_failures(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    body = _basic_body(
        "e:notexist00",
        title="",  # empty title
        category="not.in.enum",  # bad category
        severity_cell=["PII", "nonsense"],  # bad cell
        provenance_chain=[],  # empty chain
        impact={},  # bad impact
    )
    with pytest.raises(ChannelRejection) as excinfo:
        chans["finding.file"].call(body, ctx_a)
    msg = str(excinfo.value)
    # All STILL-STRICT rules surface at once (not first-fail).
    assert "TITLE_LENGTH" in msg
    assert "SEVERITY_CELL_VALID" in msg  # a bad cell was explicitly provided
    assert "EVIDENCE_REFS_RESOLVE" in msg
    assert "CLAIM_VERIFICATION" in msg
    # Forensic-metadata rules are now relaxed to optional — they must NOT fire
    # for a natural finding that simply omits/loosely-fills them.
    assert "CATEGORY_IN_ENUM" not in msg
    assert "PROVENANCE_CHAIN_VALID" not in msg
    assert "IMPACT_FIELDS_VALID" not in msg


def test_finding_file_rejects_fix_template_or_custom_violation(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s", activation_id="act-1")
    install_lead_channels(sessions_root=sessions_root)
    install_fact_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({
        "title": "lead", "reasoning": "r", "evidence_refs": [rec.id],
    }, ctx_a)
    body = _basic_body(rec.id, lead_refs=[out["lead"]["id"]])
    body["fix_custom"] = {"what_changed": "x", "why_it_fixes": "y",
                           "verification_method": "z"}  # AND template set
    with pytest.raises(ChannelRejection, match="FIX_TEMPLATE_OR_CUSTOM"):
        chans["finding.file"].call(body, ctx_a)


def test_finding_sign_off_locks_and_confirms_leads(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({
        "title": "lead", "reasoning": "r", "evidence_refs": [rec.id],
    }, ctx_a)
    lead_id = out["lead"]["id"]
    body = _basic_body(rec.id, lead_refs=[lead_id])
    finding_id = chans["finding.file"].call(body, ctx_a)["finding"]["id"]

    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    res = chans["finding.sign_off"].call(
        {"finding_id": finding_id, "operator_id": "op-1"}, ctx_o,
    )
    assert res["finding"]["status"] == "signed_off"
    assert res["finding"]["content_hash"]

    # Lead auto-confirmed
    from ara.runtime.leads.store import LeadStore
    ls = LeadStore(eng_dir)
    ld = ls.by_id(lead_id)
    assert ld is not None and ld.status == "confirmed"


def test_finding_sign_off_rejects_non_operator(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({
        "title": "lead", "reasoning": "r", "evidence_refs": [rec.id],
    }, ctx_a)
    body = _basic_body(rec.id, lead_refs=[out["lead"]["id"]])
    fid = chans["finding.file"].call(body, ctx_a)["finding"]["id"]
    with pytest.raises(ChannelRejection, match="not allowed"):
        chans["finding.sign_off"].call({"finding_id": fid}, ctx_a)


def test_finding_rescind(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({
        "title": "lead", "reasoning": "r", "evidence_refs": [rec.id],
    }, ctx_a)
    body = _basic_body(rec.id, lead_refs=[out["lead"]["id"]])
    fid = chans["finding.file"].call(body, ctx_a)["finding"]["id"]
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    res = chans["finding.rescind"].call(
        {"finding_id": fid, "reason": "wrong category"}, ctx_o,
    )
    assert res["finding"]["status"] == "rescinded"


def test_finding_enrich_workflow(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    # Operator-input finding (no lead_refs needed)
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    body = _basic_body(rec.id, origin="operator_input", lead_refs=[])
    fid = chans["finding.file"].call(body, ctx_o)["finding"]["id"]
    # Agent enriches
    ctx_a = _ctx(eng_dir / "events.jsonl", caller="agent")
    res = chans["finding.enrich"].call({
        "finding_id": fid,
        "enrichments": [
            {"field": "fix_custom.anti_patterns",
             "proposed_value": "do not blacklist",
             "rationale": "blacklist bypassable"},
        ],
    }, ctx_a)
    assert res["finding"]["origin"] == "operator_seeded_agent_enriched"
    assert res["added_enrichments"] == 1
    # Operator approves
    res2 = chans["finding.enrichment_review"].call({
        "finding_id": fid, "field": "fix_custom.anti_patterns",
        "decision": "approved",
    }, ctx_o)
    enrichments = res2["finding"]["enrichments"]
    assert len(enrichments) == 1
    assert enrichments[0]["status"] == "approved"
    assert enrichments[0]["reviewed_by"] == "operator"


def test_evidence_freshness_rule_rejects_stale(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    # Evidence captured in a prior activation
    rec = binder.put(
        b"x", source="tool:curl", source_metadata={}, summary="s",
        activation_id="prior-activation",
    )
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    # Lead must exist for agent_derived findings
    ctx_prior = _ctx(eng_dir / "events.jsonl")
    ctx_prior.activation_id = "prior-activation"
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({
        "title": "lead", "reasoning": "r", "evidence_refs": [rec.id],
    }, ctx_prior)
    # Now we're in a NEW activation; evidence is stale
    ctx_now = _ctx(eng_dir / "events.jsonl", act="current-activation")
    body = _basic_body(rec.id, lead_refs=[out["lead"]["id"]])
    with pytest.raises(ChannelRejection, match="EVIDENCE_FRESHNESS_RULE"):
        chans["finding.file"].call(body, ctx_now)


def test_evidence_freshness_rule_skipped_for_operator_input(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    rec_id = _put_evidence_in_activation(eng_dir, activation_id="prior-activation")
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    # operator_input findings DON'T enforce freshness — operator may
    # author them retroactively against historic evidence
    ctx_now = _ctx(eng_dir / "events.jsonl", act="current-activation",
                   caller="operator")
    body = _basic_body(rec_id, origin="operator_input", lead_refs=[])
    res = chans["finding.file"].call(body, ctx_now)
    assert res["finding"]["status"] == "draft"


def test_impact_validator_accepts_pack_declared_axes(tmp_path: Path) -> None:
    """If the weapon ships an impact.json declaring a non-CIA axis set,
    the validator follows it instead of the hard-coded CIA defaults."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    # Override the impact alphabet with a code-review-flavored axis set
    (weapon_dir / "alphabets" / "impact.json").write_text(json.dumps({
        "shape": "vector",
        "axes": [
            {"name": "correctness", "min": 0, "max": 3},
            {"name": "maintainability", "min": 0, "max": 3},
            {"name": "performance", "min": 0, "max": 3},
        ],
    }))
    rec_id = _put_evidence_in_activation(eng_dir)
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({"title": "lead", "reasoning": "r",
                          "evidence_refs": [rec_id]}, ctx_a)
    # CIA impact is now the WRONG axis set
    body = _basic_body(rec_id, lead_refs=[out["lead"]["id"]],
                       impact={"confidentiality": 1, "integrity": 0,
                                "availability": 0})
    with pytest.raises(ChannelRejection, match="impact axis mismatch"):
        chans["finding.file"].call(body, ctx_a)
    # The pack-declared axes pass
    body["impact"] = {"correctness": 2, "maintainability": 1, "performance": 0}
    res = chans["finding.file"].call(body, ctx_a)
    assert res["finding"]["status"] == "draft"


def test_impact_validator_rejects_out_of_range(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    rec_id = _put_evidence_in_activation(eng_dir)
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({"title": "lead", "reasoning": "r",
                          "evidence_refs": [rec_id]}, ctx_a)
    body = _basic_body(rec_id, lead_refs=[out["lead"]["id"]],
                       impact={"confidentiality": 5,  # out of range
                                "integrity": 0,
                                "availability": 0})
    with pytest.raises(ChannelRejection, match="must be int in 0..3"):
        chans["finding.file"].call(body, ctx_a)


def test_finding_enrichment_review_rejects_unknown_decision(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    body = _basic_body(rec.id, origin="operator_input", lead_refs=[])
    fid = chans["finding.file"].call(body, ctx_o)["finding"]["id"]
    with pytest.raises(ChannelRejection, match="unknown decision"):
        chans["finding.enrichment_review"].call({
            "finding_id": fid, "field": "x", "decision": "garbage",
        }, ctx_o)


# ---------- queue_for_review / return_to_draft ----------

def _file_draft(tmp_path: Path) -> tuple[Path, Path, Path, str, ChannelContext, dict]:
    """Helper: stand up channels + file one draft finding. Returns
    (sessions_root, eng_dir, weapon_dir, finding_id, ctx_a, chans)."""
    sessions_root, eng_dir, weapon_dir = _new_session(tmp_path)
    rec_id = _put_evidence_in_activation(eng_dir)
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = _ctx(eng_dir / "events.jsonl")
    body = _basic_body(rec_id, origin="operator_input")
    res = chans["finding.file"].call(body, ctx_a)
    return sessions_root, eng_dir, weapon_dir, res["finding"]["id"], ctx_a, chans


def test_queue_for_review_transitions_draft_to_operator_review(tmp_path: Path) -> None:
    _, _eng, _wd, fid, _ca, chans = _file_draft(tmp_path)
    op_ctx = _ctx(_eng / "events.jsonl", caller="operator")
    res = chans["finding.queue_for_review"].call(
        {"finding_id": fid, "note": "needs domain review"}, op_ctx,
    )
    assert res["finding"]["status"] == "operator_review"
    assert "needs domain review" in res["finding"]["notes"]


def test_queue_for_review_rejects_non_draft(tmp_path: Path) -> None:
    _, _eng, _wd, fid, _ca, chans = _file_draft(tmp_path)
    op_ctx = _ctx(_eng / "events.jsonl", caller="operator")
    chans["finding.queue_for_review"].call({"finding_id": fid}, op_ctx)
    with pytest.raises(ChannelRejection, match="can't queue for review"):
        chans["finding.queue_for_review"].call({"finding_id": fid}, op_ctx)


def test_queue_for_review_rejects_unknown_finding(tmp_path: Path) -> None:
    _, _eng, _wd, _fid, _ca, chans = _file_draft(tmp_path)
    op_ctx = _ctx(_eng / "events.jsonl", caller="operator")
    with pytest.raises(ChannelRejection, match="finding not found"):
        chans["finding.queue_for_review"].call(
            {"finding_id": "F:ghost"}, op_ctx,
        )


def test_return_to_draft_transitions_back(tmp_path: Path) -> None:
    _, _eng, _wd, fid, _ca, chans = _file_draft(tmp_path)
    op_ctx = _ctx(_eng / "events.jsonl", caller="operator")
    chans["finding.queue_for_review"].call({"finding_id": fid}, op_ctx)
    res = chans["finding.return_to_draft"].call(
        {"finding_id": fid, "note": "agent re-runs probe"}, op_ctx,
    )
    assert res["finding"]["status"] == "draft"
    assert "agent re-runs probe" in res["finding"]["notes"]


def test_return_to_draft_rejects_non_review(tmp_path: Path) -> None:
    _, _eng, _wd, fid, _ca, chans = _file_draft(tmp_path)
    op_ctx = _ctx(_eng / "events.jsonl", caller="operator")
    with pytest.raises(ChannelRejection, match="can't return to draft"):
        chans["finding.return_to_draft"].call({"finding_id": fid}, op_ctx)


def test_sign_off_accepts_operator_review_status(tmp_path: Path) -> None:
    _, _eng, _wd, fid, _ca, chans = _file_draft(tmp_path)
    op_ctx = _ctx(_eng / "events.jsonl", caller="operator")
    chans["finding.queue_for_review"].call({"finding_id": fid}, op_ctx)
    res = chans["finding.sign_off"].call(
        {"finding_id": fid, "operator_id": "alice"}, op_ctx,
    )
    assert res["finding"]["status"] == "signed_off"
    assert res["finding"]["signed_off_by"] == "alice"
