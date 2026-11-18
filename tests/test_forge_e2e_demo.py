"""End-to-end forge-v1 demo.

Drives a complete loop:

    forge new → deposit (tiny WSTG-shaped mat) → finalize → manifest →
    review → record extractions → synthesize → seal
        ↓
    session open against sealed weapon → ActivationManager mint →
    install all channels → capture evidence → fact.assert → lead.file →
    anomaly.emit → anomaly.promote → finding.file → finding.sign_off
    (auto-confirms cited lead) → verify audit chains

This exists to expose integration gaps that the per-module unit tests
don't see. Each ``# GAP:`` comment marks a place where the test had to
work around something the v1 scaffolding doesn't yet handle on its own.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ara.audit import verify_chain
from ara.forge import state as fstate
from ara.forge.phases import deposit, exhaust, manifest, seal, synthesize
from ara.forge.session import ForgePhase, ForgeSession
from ara.runtime import audit as raudit
from ara.runtime.activation import ActivationManager
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.bootstrap import (
    engagement_context_factory,
    install_engagement_channels,
)
from ara.runtime.channels import registry
from ara.runtime.findings.alphabets import (
    CategoryEntry,
    CategoryEnum,
    FixLibrary,
    FixTemplate,
    RubricCell,
    SeverityRubric,
    write_category_enum,
    write_fix_library,
    write_severity_rubric,
)
from ara.runtime.findings.store import FindingStore
from ara.runtime.leads.store import LeadStore


def _record_alphabet_extractions(sess, element_id: str) -> None:
    """Record three CONC extractions that populate the offsec pack's
    alphabets (category / severity_rubric / fix_library).

    With the alphabet engine now mat-driven, the seal phase produces
    <weapon>/alphabets/*.json from these extractions automatically —
    no manual seeding needed.
    """
    from ara.forge.phases import exhaust as _exhaust
    _exhaust.record_extraction(
        sess, manifest_element_id=element_id, channel="extract.conc",
        body={"alphabet_name": "category",
              "entry": {"id": "xss.reflected", "label": "Reflected XSS"}},
        extraction_id="x_conc_cat_xss",
    )
    _exhaust.record_extraction(
        sess, manifest_element_id=element_id, channel="extract.conc",
        body={"alphabet_name": "category",
              "entry": {"id": "idor", "label": "IDOR"}},
        extraction_id="x_conc_cat_idor",
    )
    _exhaust.record_extraction(
        sess, manifest_element_id=element_id, channel="extract.conc",
        body={"alphabet_name": "severity_rubric",
              "coords": {"data_class": "PII", "access": "read"},
              "cell": {"score": 6.0, "bucket": "medium"}},
        extraction_id="x_conc_rubric_pii_read",
    )
    _exhaust.record_extraction(
        sess, manifest_element_id=element_id, channel="extract.conc",
        body={"alphabet_name": "severity_rubric",
              "coords": {"data_class": "credentials", "access": "admin"},
              "cell": {"score": 9.5, "bucket": "critical"}},
        extraction_id="x_conc_rubric_creds_admin",
    )
    _exhaust.record_extraction(
        sess, manifest_element_id=element_id, channel="extract.conc",
        body={"alphabet_name": "fix_library",
              "entry": {"id": "fix.xss.encode_output",
                        "title": "Encode output at boundary",
                        "applicable_categories": ["xss.reflected"],
                        "body": "encode HTML/JS context output"}},
        extraction_id="x_conc_fix_xss",
    )


def _seed_alphabets_legacy(weapon_dir: Path) -> None:
    """Retained only as a reference; no longer called. The alphabet
    pack engine derives alphabets from CONC extractions during
    synthesize+seal. Kept here in case a future test wants to bypass
    extractions and seed the on-disk files directly.
    """
    write_category_enum(
        weapon_dir / "alphabets" / "category_enum.json",
        CategoryEnum(entries={
            "xss.reflected": CategoryEntry(id="xss.reflected", label="Reflected XSS"),
            "sqli": CategoryEntry(id="sqli", label="SQL injection"),
            "idor": CategoryEntry(id="idor", label="IDOR"),
        }),
    )
    write_severity_rubric(
        weapon_dir / "alphabets" / "severity_rubric.json",
        SeverityRubric(
            data_classes=["public_data", "PII", "credentials"],
            accesses=["read", "write", "admin"],
            matrix={
                "public_data": {"read": RubricCell(1.5, "low")},
                "PII": {
                    "read": RubricCell(6.0, "medium"),
                    "write": RubricCell(8.0, "high"),
                },
                "credentials": {"admin": RubricCell(9.5, "critical")},
            },
        ),
    )
    write_fix_library(
        weapon_dir / "alphabets" / "fix_library.json",
        FixLibrary(templates={
            "fix.xss.encode_output": FixTemplate(
                id="fix.xss.encode_output",
                title="Encode output at the boundary",
                applicable_categories=["xss.reflected", "xss.stored"],
                body="encode HTML/JS-context output before reflection",
            ),
        }),
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    """Test isolation — registry + audit-writer caches leak between tests."""
    registry.unregister_for_tests()
    raudit.reset_registry_for_tests()
    yield
    registry.unregister_for_tests()
    raudit.reset_registry_for_tests()


def test_forge_to_engagement_e2e(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()

    # ---------- Stage 1: forge a tiny weapon ----------

    mat = sources / "wstg-mini.md"
    mat.write_text(
        "# WSTG-mini\n\n"
        "## Finding A — Reflected XSS in /search\n"
        "Reflected user input in `q` parameter without HTML encoding.\n\n"
        "## Finding B — IDOR on /api/orders/{id}\n"
        "Sequential numeric ids; no authorization check on read.\n\n"
        "## Finding C — Stored credentials in response\n"
        "JSON response includes `apiKey` for the requesting user.\n"
    )

    sess = ForgeSession.new(
        state_root,
        weapon_target_id="wstg-mini-v1",
        operator_id="op-e2e",
    )
    deposit.deposit_mat(sess, mat)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    assert sess.phase == ForgePhase.REVIEW  # auto-advanced
    assert len(elements) >= 3  # 3 finding-sections

    manifest.approve_manifest(sess)
    assert sess.phase == ForgePhase.EXHAUST

    # GAP: no agent-driven extraction yet. Operator/test records the
    # extractions directly, one per element. In a real run, these come
    # from the supervised agent loop using extract.* channels (1.4
    # synthesize §4.4).
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess,
            manifest_element_id=el.id,
            channel="extract.proc",
            body={
                "crumb_id": f"c_{i:03d}",
                "name": f"probe from {el.metadata.get('heading', 'section')!r}"[:120],
                "tags": ["recon"],
                "requires": [], "action": "fetch", "emits": [],
            },
            extraction_id=f"x_proc_{i:03d}",
        )
    # CONC alphabet extractions — populate the offsec pack's three
    # alphabets (category / severity_rubric / fix_library). In a real
    # run the agent proposes these from CONC over example-report mats;
    # here we author them directly to drive the seal-time validator.
    _record_alphabet_extractions(sess, elements[0].id)
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="extractions approved")
    draft = synthesize.synthesize(sess)
    assert len(draft.crumbs) == len(elements)
    assert draft.alphabet_pack_name == "offsec"
    assert "category" in draft.alphabets
    assert "severity_rubric" in draft.alphabets
    assert "fix_library" in draft.alphabets

    sess.transition(ForgePhase.REVIEW, reason="synthesis done")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="wstg-mini-v1")
    assert result.ok, f"seal failed: {result.failures}"
    assert sess.phase == ForgePhase.SEALED

    weapon_dir = fstate.weapon_dir(state_root, "wstg-mini-v1")
    assert (weapon_dir / "weapon.json").exists()
    assert (weapon_dir / "seal.json").exists()
    assert (weapon_dir / "channel_catalog.json").exists()
    assert (weapon_dir / "search" / "fts.db").exists()

    # Alphabet files were produced automatically by the seal phase
    # from the CONC extractions recorded above (offsec pack).
    assert (weapon_dir / "alphabets" / "category.json").exists()
    assert (weapon_dir / "alphabets" / "severity_rubric.json").exists()
    assert (weapon_dir / "alphabets" / "fix_library.json").exists()

    # ---------- Stage 2: open an engagement against the sealed weapon ----------

    sessions_root = state_root / "sessions"
    engagement_id = "eng-e2e-1"
    session_dir = sessions_root / engagement_id
    session_dir.mkdir(parents=True)

    activations = ActivationManager(session_dir)
    initial_aid = activations.current()
    assert initial_aid

    # One bootstrap call wires every subsystem's typed channels.
    install_engagement_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    audit_sink = session_dir / "events.jsonl"
    make_ctx = engagement_context_factory(
        engagement_id=engagement_id,
        activation_manager=activations,
        audit_sink=audit_sink,
    )

    def agent_ctx():
        return make_ctx("agent")

    def operator_ctx():
        return make_ctx("operator")

    # ---------- Stage 3: agent captures evidence ----------

    binder = EvidenceBinder(session_dir)
    ev_xss = binder.put(
        b'<html><body>Hello, <script>alert(1)</script></body></html>',
        source="tool:curl",
        source_metadata={"args": "https://target.test/search?q=<script>alert(1)</script>"},
        summary="reflected payload in /search response",
        content_type="text/html",
        activation_id=activations.current(),
    )
    ev_idor = binder.put(
        b'{"order_id": 1042, "customer": "alice", "total": 99.0}',
        source="tool:curl",
        source_metadata={"args": "https://target.test/api/orders/1042"},
        summary="IDOR — read other customer's order",
        content_type="application/json",
        activation_id=activations.current(),
    )

    # ---------- Stage 4: assert facts from observations ----------

    fact_assert = registry.get("fact.assert")
    fact_target = fact_assert.call({
        "predicate": "target.endpoints.search",
        "value": "/search",
        "asserted_by_crumb": "c_000",
        "evidence_refs": [ev_xss.id],
    }, agent_ctx())
    assert fact_target["fact"]["predicate"] == "target.endpoints.search"

    fact_idor_evidence = fact_assert.call({
        "predicate": "target.endpoints.orders.idor_observed",
        "value": True,
        "asserted_by_crumb": "c_001",
        "evidence_refs": [ev_idor.id],
    }, agent_ctx())

    # ---------- Stage 5: agent files leads ----------

    lead_file = registry.get("lead.file")
    lead_xss = lead_file.call({
        "title": "Reflected XSS in /search?q=",
        "reasoning": "Reflected payload appears unencoded; encoded variant suggests no HTML escape on output.",
        "evidence_refs": [ev_xss.id],
        "fact_refs": [fact_target["fact"]["id"]],
        "derived_from_crumb": "c_000",
        "attack_surface_tier": 0.7,
        "scope_weight": 0.9,
    }, agent_ctx())

    lead_idor = lead_file.call({
        "title": "IDOR on /api/orders/{id}",
        "reasoning": "Sequential ids returned without auth check; observed cross-tenant read.",
        "evidence_refs": [ev_idor.id],
        "fact_refs": [fact_idor_evidence["fact"]["id"]],
        "derived_from_crumb": "c_001",
        "attack_surface_tier": 1.0,
        "scope_weight": 1.0,
    }, agent_ctx())
    assert lead_idor["priority"] in ("high", "critical")

    # ---------- Stage 6: anomaly emit + operator promote ----------
    # GAP: in a real run, the supervisor compares tool output against the
    # active crumb's expected_evidence_shape after each tool call, then
    # auto-fires anomaly.emit. There is no such auto-firer yet — the
    # anomaly channel is only callable directly.

    ev_weird = binder.put(
        b'{"order_id": 1042, "debug_mode": true, "internal_log": "auth bypassed"}',
        source="tool:curl",
        source_metadata={"args": "https://target.test/api/orders/1042?debug=1"},
        summary="response includes unexpected debug fields",
        content_type="application/json",
        activation_id=activations.current(),
    )
    anomaly_emit = registry.get("anomaly.emit")
    emitted = anomaly_emit.call({
        "evidence_id": ev_weird.id,
        "crumb_id": "c_001",
        "expected_shape_hash": "sha256:placeholder",
        "divergence": {"raw_score": 0.78,
                       "unexpected_fields": ["debug_mode", "internal_log"]},
        "threshold_at_emission": 0.5,
    }, agent_ctx())
    assert emitted["dropped"] is False
    anomaly_promote = registry.get("anomaly.promote")
    promoted = anomaly_promote.call({
        "anomaly_id": emitted["anomaly"]["id"],
        "title": "Backend leaks debug fields on order endpoint",
    }, operator_ctx())
    assert promoted["lead"]["origin"] == "anomaly_promoted"

    # ---------- Stage 7: file + sign off a finding (current activation) ----------

    finding_file = registry.get("finding.file")
    drafted = finding_file.call({
        "title": "Reflected XSS in /search",
        "category": "xss.reflected",
        "severity_cell": ["PII", "read"],
        "impact": {"confidentiality": 2, "integrity": 0, "availability": 0},
        "evidence_refs": [ev_xss.id],
        "fact_refs": [fact_target["fact"]["id"]],
        "lead_refs": [lead_xss["lead"]["id"]],
        "provenance_chain": ["c_000", fact_target["fact"]["id"], lead_xss["lead"]["id"]],
        "fix_template_id": "fix.xss.encode_output",
        "origin": "agent_derived",
        "affected_assets": ["target.test"],
    }, agent_ctx())
    assert drafted["finding"]["status"] == "draft"
    assert drafted["finding"]["severity_score"] == 6.0
    assert drafted["finding"]["severity_name"] == "medium"

    finding_sign_off = registry.get("finding.sign_off")
    signed = finding_sign_off.call({
        "finding_id": drafted["finding"]["id"],
        "operator_id": "op-e2e",
    }, operator_ctx())
    assert signed["finding"]["status"] == "signed_off"
    assert signed["finding"]["content_hash"]

    # ---------- Stage 8: chunk swap → freshness rule rejects stale evidence ----------

    pre_swap_aid = activations.current()
    activations.mint(reason="chunk_swap", chunk_id="writeup")
    post_swap_aid = activations.current()
    assert pre_swap_aid != post_swap_aid

    # Trying to file a NEW finding citing the same evidence (now stale —
    # captured in a prior activation, never re-fetched in this one)
    # should be rejected by EVIDENCE_FRESHNESS_RULE.
    from ara.runtime.channels.base import ChannelRejection
    with pytest.raises(ChannelRejection, match="EVIDENCE_FRESHNESS_RULE"):
        finding_file.call({
            "title": "Second IDOR finding (stale evidence)",
            "category": "idor",
            "severity_cell": ["credentials", "admin"],
            "impact": {"confidentiality": 3, "integrity": 0, "availability": 0},
            "evidence_refs": [ev_idor.id],
            "fact_refs": [fact_idor_evidence["fact"]["id"]],
            "lead_refs": [lead_idor["lead"]["id"]],
            "provenance_chain": ["c_001"],
            "fix_custom": {
                "what_changed": "add owner check on /api/orders/{id}",
                "why_it_fixes": "blocks cross-tenant read",
                "verification_method": "fetch with non-owner token expects 403",
            },
            "origin": "agent_derived",
            "affected_assets": ["target.test"],
        }, agent_ctx())

    # ---------- Stage 9: cited lead auto-confirmed ----------

    leads = LeadStore(session_dir)
    confirmed = leads.by_id(lead_xss["lead"]["id"])
    assert confirmed is not None
    assert confirmed.status == "confirmed", (
        f"expected lead.confirm hook to fire when finding signed off; "
        f"status={confirmed.status}"
    )

    # ---------- Stage 10: post-conditions ----------

    findings = FindingStore(session_dir)
    assert len(findings.signed_off()) == 1

    # Audit chain on session events.jsonl is well-formed
    ok, last, err = verify_chain(audit_sink)
    assert ok, f"session audit chain broken at line {last}: {err}"
    # 2 facts + 2 leads + 1 anomaly.emit + 1 anomaly.promote (+1 lead.file
    # the promote chained) + 1 finding.file + 1 finding.sign_off + 1
    # lead.confirm = ~10 channel-call audit events.
    assert last >= 10

    # Forge audit chain still well-formed too
    forge_audit = fstate.forge_audit_path(state_root, sess.forge_id)
    ok, last, err = verify_chain(forge_audit)
    assert ok, f"forge audit chain broken at line {last}: {err}"

    # Activation history reflects the chunk swap
    history = activations.history()
    assert len(history) >= 2
    assert history[-1].chunk_id == "writeup"

    # 4.1: catalog ships with the weapon and now captures every typed
    # channel from every subsystem (class-level registration runs at
    # module import; seal phase imports those modules). This was a v1
    # gap — fixed by splitting the registry into class + instance tiers.
    import json as _json
    catalog = _json.loads((weapon_dir / "channel_catalog.json").read_text())
    catalog_names = {c["name"] for c in catalog["channels"]}
    expected = {
        "fact.assert", "facts.query", "facts.history", "facts.neighbors",
        "lead.file", "lead.investigate", "lead.touch", "lead.dismiss",
        "lead.promote", "lead.confirm", "lead.priority_override",
        "finding.file", "finding.sign_off", "finding.rescind",
        "finding.enrich", "finding.enrichment_review",
        "anomaly.emit", "anomaly.promote", "anomaly.dismiss",
        "style.score",
    }
    missing = expected - catalog_names
    assert not missing, f"channels missing from sealed catalog: {missing}"
