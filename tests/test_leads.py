"""Tests for runtime.leads."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext, ChannelRejection
from ara.runtime.facts.channels import install_fact_channels
from ara.runtime.leads.channels import install_lead_channels
from ara.runtime.leads.closure_gate import check_closure
from ara.runtime.leads.deaths import (
    aged_past_ceiling,
    contradicted_by_fact,
    engagement_closed,
)
from ara.runtime.leads.decay import (
    AGED_CEILING_S,
    INITIAL_STABILITY_S,
    apply_action,
    retrievability,
)
from ara.runtime.leads.priority import compute_priority
from ara.runtime.leads.record import (
    Lead,
    NegationPredicate,
    PrioritySignals,
    mint_lead_id,
)
from ara.runtime.leads.store import LeadStore


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


# ---------- record + store ----------

def test_lead_id_format() -> None:
    a = mint_lead_id()
    b = mint_lead_id()
    assert a.startswith("L:") and len(a) == 12
    assert a != b


def _basic_lead(**over) -> Lead:
    base = dict(
        title="t", reasoning="r",
        proposed_test=None, not_mechanically_testable=False,
        evidence_refs=[], fact_refs=[], activation_id="a-1",
        origin="agent_hypothesis", enrichments=[],
        priority_computed="medium",
        priority_signals=PrioritySignals(0.5, 0.4, 0.4, 0.7, 0.0, 0.5),
        priority_agent_proposed=None, priority_operator_override=None,
        stability_s=INITIAL_STABILITY_S["medium"],
        last_reviewed_at=time.time(), review_history=[],
        aged_ceiling_at=time.time() + AGED_CEILING_S["medium"],
        status="open", death_signal=None, death_citation=None,
        created_at=time.time(), closed_at=None, closed_by=None,
    )
    base.update(over)
    base["id"] = base.get("id") or mint_lead_id()
    return Lead(**base)


def test_lead_store_round_trip(tmp_path: Path) -> None:
    s = LeadStore(tmp_path)
    ld = _basic_lead()
    s.append(ld)
    re_read = s.by_id(ld.id)
    assert re_read is not None
    assert re_read.title == "t"
    assert s.path.exists()


def test_lead_store_latest_wins(tmp_path: Path) -> None:
    s = LeadStore(tmp_path)
    ld = _basic_lead(title="v1")
    s.append(ld)
    from dataclasses import replace
    s.append(replace(ld, title="v2"))
    s.append(replace(ld, title="v3"))
    out = s.by_id(ld.id)
    assert out is not None and out.title == "v3"
    assert len(s.all()) == 1


# ---------- decay ----------

def test_retrievability_decreases_over_time() -> None:
    ld = _basic_lead(stability_s=3600.0, last_reviewed_at=1000.0)
    assert retrievability(ld, 1000.0) == pytest.approx(1.0)
    half = retrievability(ld, 1000.0 + 3600.0)
    assert 0.35 < half < 0.40
    assert retrievability(ld, 1000.0 + 36000.0) < 0.001


def test_apply_action_investigating_doubles_stability() -> None:
    ld = _basic_lead(stability_s=100.0, last_reviewed_at=1000.0)
    out = apply_action(ld, "investigating", now=2000.0)
    assert out.stability_s == 200.0
    assert out.last_reviewed_at == 2000.0
    assert out.status == "investigating"
    assert out.review_history[-1].action == "investigating"


def test_apply_action_dismissed_closes_lead() -> None:
    ld = _basic_lead()
    out = apply_action(ld, "dismissed", now=time.time())
    assert out.status == "dismissed_operator"
    assert out.closed_at is not None


def test_apply_action_promoted_sets_promoted_at() -> None:
    ld = _basic_lead()
    out = apply_action(ld, "promoted", now=time.time())
    assert out.status == "promoted"
    assert out.promoted_at is not None
    assert out.closed_at is None  # promoted leaves closed_at unset


# ---------- priority ----------

def test_compute_priority_low_signal() -> None:
    p, sig = compute_priority(
        {"evidence_refs": [], "origin": "agent_hypothesis"},
        binder=None,
    )
    assert p == "low"
    assert sig.aggregate < 0.40


def test_compute_priority_high_signal() -> None:
    p, sig = compute_priority(
        {
            "evidence_refs": ["e:abc"],
            "origin": "agent_hypothesis",
            "derived_from_crumb": "crumb-1",
            "attack_surface_tier": 1.0,
            "scope_weight": 1.0,
        },
        binder=None,
    )
    assert p in ("high", "critical")


# ---------- death detectors ----------

def test_aged_past_ceiling() -> None:
    ld = _basic_lead(aged_ceiling_at=1000.0)
    assert aged_past_ceiling(ld, now=2000.0) is not None
    assert aged_past_ceiling(ld, now=500.0) is None


def test_engagement_closed_dismisses_open_only() -> None:
    open_ld = _basic_lead(status="open")
    closed_ld = _basic_lead(status="dismissed_operator")
    assert engagement_closed(open_ld) is not None
    assert engagement_closed(closed_ld) is None


def test_contradicted_by_fact_uses_negation_predicates(tmp_path: Path) -> None:
    from ara.runtime.facts.record import Fact, mint_id
    from ara.runtime.facts.store import FactStore

    fs = FactStore(tmp_path)
    f = Fact(
        id=mint_id(predicate="jwt.signed_with", value="HS256",
                   asserted_by_crumb="c", evidence_refs=["e"], activation_id="a",
                   asserted_at=time.time()),
        predicate="jwt.signed_with", value="HS256",
        asserted_by_crumb="c", evidence_refs=["e"], chunk_at_time="x",
        activation_id="a", asserted_at=time.time(),
    )
    fs.append(f)
    ld = _basic_lead(negation_predicates=[
        NegationPredicate(predicate="jwt.signed_with", contradicts_when_value_in=["HS256"]),
    ])
    verdict = contradicted_by_fact(ld, fact_store=fs)
    assert verdict is not None
    assert verdict.death_signal == "contradicted_by_fact"
    assert verdict.death_citation == f.id


def test_contradicted_by_fact_no_negation_skips(tmp_path: Path) -> None:
    from ara.runtime.facts.store import FactStore
    fs = FactStore(tmp_path)
    ld = _basic_lead()
    assert contradicted_by_fact(ld, fact_store=fs) is None


# ---------- closure gate ----------

def test_closure_gate_blocks_stale_high(tmp_path: Path) -> None:
    s = LeadStore(tmp_path)
    ld = _basic_lead(
        priority_computed="high",
        stability_s=1.0,
        last_reviewed_at=time.time() - 3600,  # very stale
    )
    s.append(ld)
    blockers = check_closure(s)
    assert len(blockers) == 1
    assert blockers[0].lead_id == ld.id


def test_closure_gate_ignores_low(tmp_path: Path) -> None:
    s = LeadStore(tmp_path)
    s.append(_basic_lead(priority_computed="low", stability_s=1.0,
                         last_reviewed_at=time.time() - 3600))
    assert check_closure(s) == []


def test_closure_gate_ignores_not_mechanically_testable(tmp_path: Path) -> None:
    s = LeadStore(tmp_path)
    s.append(_basic_lead(priority_computed="critical",
                         not_mechanically_testable=True,
                         stability_s=1.0,
                         last_reviewed_at=time.time() - 3600))
    assert check_closure(s) == []


# ---------- channels ----------

def _ctx(audit: Path, *, eng="eng-1", act="act-1", caller="agent") -> ChannelContext:
    return ChannelContext(
        engagement_id=eng, activation_id=act,
        caller=caller, audit_sink=audit,
    )


def test_lead_file_channel_validates_evidence(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx = _ctx(eng_dir / "events.jsonl")
    with pytest.raises(ChannelRejection, match="evidence_ref not found"):
        chans["lead.file"].call({
            "title": "t", "reasoning": "r",
            "evidence_refs": ["e:notexist00"],
        }, ctx)


def test_lead_file_channel_happy_path(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx = _ctx(eng_dir / "events.jsonl")
    out = chans["lead.file"].call({
        "title": "Possible IDOR on /api/orders/{id}",
        "reasoning": "Sequential ids visible without auth check.",
        "evidence_refs": [rec.id],
        "attack_surface_tier": 0.7,
        "scope_weight": 0.9,
    }, ctx)
    assert out["priority"] in ("low", "medium", "high", "critical")
    assert out["lead"]["status"] == "open"


def test_lead_dismiss_requires_operator(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    out = chans["lead.file"].call({
        "title": "t", "reasoning": "r", "evidence_refs": [rec.id],
    }, ctx_a)
    lead_id = out["lead"]["id"]
    # Agent caller — dismiss must reject
    with pytest.raises(ChannelRejection, match="not allowed"):
        chans["lead.dismiss"].call(
            {"lead_id": lead_id, "reason": "test"}, ctx_a,
        )
    # Operator caller passes
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    out2 = chans["lead.dismiss"].call(
        {"lead_id": lead_id, "reason": "false positive"}, ctx_o,
    )
    assert out2["lead"]["status"] == "dismissed_operator"


def test_lead_supersede_marks_status(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    a = chans["lead.file"].call({"title": "a", "reasoning": "r",
                                  "evidence_refs": [rec.id]}, ctx_a)
    b = chans["lead.file"].call({"title": "b", "reasoning": "r",
                                  "evidence_refs": [rec.id]}, ctx_a)
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    chans["lead.dismiss"].call({
        "lead_id": a["lead"]["id"], "reason": "superseded",
        "supersedes": b["lead"]["id"],
    }, ctx_o)
    store = LeadStore(eng_dir)
    sup = store.by_id(a["lead"]["id"])
    assert sup is not None and sup.status == "superseded"
    assert sup.death_signal == "superseded_by"


def test_lead_confirm_caller_must_be_system(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    out = chans["lead.file"].call(
        {"title": "t", "reasoning": "r", "evidence_refs": [rec.id]}, ctx_a,
    )
    ctx_sys = _ctx(eng_dir / "events.jsonl", caller="system")
    out2 = chans["lead.confirm"].call(
        {"lead_id": out["lead"]["id"], "finding_id": "F:abcd1234ab"}, ctx_sys,
    )
    assert out2["lead"]["status"] == "confirmed"


def test_lead_priority_override_takes_precedence(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    out = chans["lead.file"].call(
        {"title": "t", "reasoning": "r", "evidence_refs": [rec.id]}, ctx_a,
    )
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    chans["lead.priority_override"].call(
        {"lead_id": out["lead"]["id"], "priority": "critical"}, ctx_o,
    )
    store = LeadStore(eng_dir)
    ld = store.by_id(out["lead"]["id"])
    assert ld is not None
    assert ld.canonical_priority() == "critical"


def test_value_tier_canonical_name(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx = _ctx(eng_dir / "events.jsonl")
    out = chans["lead.file"].call({
        "title": "t", "reasoning": "r", "evidence_refs": [rec.id],
        "value_tier": 0.9, "scope_weight": 1.0,
    }, ctx)
    assert out["signals"]["value_tier"] == 0.9
    # back-compat alias also reflects the same value
    assert out["signals"]["attack_surface_tier"] == 0.9


def test_attack_surface_tier_alias_still_works(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx = _ctx(eng_dir / "events.jsonl")
    # Old name still accepted on the way in
    out = chans["lead.file"].call({
        "title": "t", "reasoning": "r", "evidence_refs": [rec.id],
        "attack_surface_tier": 0.9, "scope_weight": 1.0,
    }, ctx)
    assert out["signals"]["value_tier"] == 0.9


def test_lead_file_validates_fact_refs(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    install_fact_channels(sessions_root=sessions_root)
    chans = install_lead_channels(sessions_root=sessions_root)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    with pytest.raises(ChannelRejection, match="fact_ref not found"):
        chans["lead.file"].call({
            "title": "t", "reasoning": "r",
            "fact_refs": ["f:nonexist0"],
        }, ctx_a)
