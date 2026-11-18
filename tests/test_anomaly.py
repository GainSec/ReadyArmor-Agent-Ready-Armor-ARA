"""Tests for runtime.anomaly."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.anomaly.channels import install_anomaly_channels
from ara.runtime.anomaly.divergence import compute_divergence
from ara.runtime.anomaly.record import (
    AnomalyRecord,
    AnomalyStore,
    divergence_to_dict,
    mint_anomaly_id,
)
from ara.runtime.anomaly.shape import (
    ExpectedEvidenceShape,
    FieldPattern,
    FieldRequirement,
    LineMatcher,
    SectionMatcher,
    StructuralConstraint,
)
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext, ChannelRejection
from ara.runtime.facts.channels import install_fact_channels
from ara.runtime.leads.channels import install_lead_channels


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


# ---------- shape ----------

def test_shape_hash_stable() -> None:
    s1 = ExpectedEvidenceShape(divergence_threshold=0.5)
    s2 = ExpectedEvidenceShape(divergence_threshold=0.5)
    assert s1.hash() == s2.hash()
    s3 = ExpectedEvidenceShape(divergence_threshold=0.3)
    assert s1.hash() != s3.hash()


# ---------- divergence (JSON) ----------

def test_divergence_zero_when_evidence_matches_shape() -> None:
    shape = ExpectedEvidenceShape(
        required_fields=[FieldRequirement(name="$.plugins", cardinality="one_or_more")],
        unexpected_fields_threshold=2,
    )
    evidence = {"plugins": [{"name": "p1"}, {"name": "p2"}]}
    dv = compute_divergence(evidence, shape)
    assert dv.raw_score == 0.0


def test_divergence_high_on_missing_required() -> None:
    shape = ExpectedEvidenceShape(
        required_fields=[FieldRequirement(name="$.plugins", cardinality="one_or_more")],
    )
    dv = compute_divergence({"users": []}, shape)
    assert "$.plugins" in dv.missing_required
    assert dv.raw_score > 0.0


def test_divergence_pattern_mismatch() -> None:
    shape = ExpectedEvidenceShape(
        field_patterns={
            "$.version": FieldPattern(name="$.version", kind="regex",
                                       pattern=r"^\d+\.\d+$"),
        },
    )
    dv = compute_divergence({"version": "not-a-version"}, shape)
    assert "$.version" in dv.pattern_mismatches


def test_divergence_value_in_pattern() -> None:
    shape = ExpectedEvidenceShape(
        field_patterns={
            "$.status": FieldPattern(name="$.status", kind="value_in",
                                      pattern=["ok", "warn"]),
        },
    )
    dv1 = compute_divergence({"status": "ok"}, shape)
    dv2 = compute_divergence({"status": "fail"}, shape)
    assert "$.status" not in dv1.pattern_mismatches
    assert "$.status" in dv2.pattern_mismatches


def test_divergence_unexpected_fields() -> None:
    shape = ExpectedEvidenceShape(
        required_fields=[FieldRequirement(name="$.plugins")],
        unexpected_fields_threshold=2,
    )
    dv = compute_divergence(
        {"plugins": [], "debug_mode": True, "extra": 1, "more": 2}, shape,
    )
    assert "debug_mode" in dv.unexpected_fields
    assert dv.raw_score > 0


def test_divergence_structural_http() -> None:
    shape = ExpectedEvidenceShape(
        structural_constraints=[
            StructuralConstraint(kind="http_status_in", body={"allowed": [200]}),
        ],
    )
    dv = compute_divergence("body", shape, http_status=500)
    assert any("http_status_in" in v for v in dv.structural_violations)


def test_divergence_structural_content_type_match() -> None:
    shape = ExpectedEvidenceShape(
        structural_constraints=[
            StructuralConstraint(kind="content_type_in",
                                  body={"allowed": ["text/html"]}),
        ],
    )
    dv = compute_divergence("body", shape, content_type="text/html; charset=utf-8")
    assert dv.raw_score == 0.0


def test_divergence_header_present() -> None:
    shape = ExpectedEvidenceShape(
        structural_constraints=[
            StructuralConstraint(kind="header_present", body={"name": "X-Frame-Options"}),
        ],
    )
    dv1 = compute_divergence("", shape, headers={"X-Frame-Options": "DENY"})
    dv2 = compute_divergence("", shape, headers={})
    assert dv1.raw_score == 0.0
    assert dv2.raw_score > 0


# ---------- divergence (text logs) ----------

def test_line_matcher_finds_lines() -> None:
    shape = ExpectedEvidenceShape(
        line_matchers=[
            LineMatcher(line_regex=r"^\d+/tcp\s+\w+", cardinality="one_or_more",
                        label="port_row"),
        ],
    )
    text = (
        "PORT     STATE    SERVICE\n"
        "22/tcp   open     ssh\n"
        "80/tcp   open     http\n"
    )
    dv = compute_divergence(text, shape)
    assert dv.line_matcher_failures == []
    assert dv.raw_score == 0.0


def test_line_matcher_fails_on_missing() -> None:
    shape = ExpectedEvidenceShape(
        line_matchers=[
            LineMatcher(line_regex=r"^\d+/tcp", cardinality="one_or_more",
                        label="port_row"),
        ],
    )
    dv = compute_divergence("nothing useful here", shape)
    assert len(dv.line_matcher_failures) == 1


def test_section_matcher_finds_section() -> None:
    shape = ExpectedEvidenceShape(
        section_matchers=[
            SectionMatcher(
                header_regex=r"User\(s\) Identified:",
                cardinality="required",
                body_matchers=[
                    LineMatcher(line_regex=r"^\s*-\s+\S+", cardinality="one_or_more",
                                label="user_line"),
                ],
                label="users_section",
            ),
        ],
    )
    text = (
        "[i] User(s) Identified:\n"
        " - admin\n"
        " - editor\n"
    )
    dv = compute_divergence(text, shape)
    assert dv.section_matcher_failures == []


def test_section_matcher_missing_section() -> None:
    shape = ExpectedEvidenceShape(
        section_matchers=[
            SectionMatcher(header_regex=r"WONTAPPEAR", cardinality="required"),
        ],
    )
    dv = compute_divergence("unrelated", shape)
    assert len(dv.section_matcher_failures) == 1


# ---------- record + store ----------

def test_anomaly_id_format() -> None:
    a = mint_anomaly_id()
    assert a.startswith("A:") and len(a) == 12


def test_anomaly_store_round_trip(tmp_path: Path) -> None:
    s = AnomalyStore(tmp_path)
    rec = AnomalyRecord(
        id=mint_anomaly_id(), evidence_id="e:abc", crumb_id="c1",
        expected_shape_hash="sha256:x",
        divergence={"raw_score": 0.6}, threshold_at_emission=0.5,
        activation_id="act-1", detected_at=1.0,
    )
    s.append(rec)
    assert s.by_id(rec.id) == rec


# ---------- channels ----------

def _ctx(audit: Path, *, eng="eng-1", act="act-1", caller="agent") -> ChannelContext:
    return ChannelContext(
        engagement_id=eng, activation_id=act, caller=caller, audit_sink=audit,
    )


def test_anomaly_emit_happy_path(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_anomaly_channels(sessions_root=sessions_root)
    ctx = _ctx(eng_dir / "events.jsonl")
    res = chans["anomaly.emit"].call({
        "evidence_id": rec.id, "crumb_id": "c1",
        "expected_shape_hash": "sha256:x",
        "divergence": {"raw_score": 0.6, "missing_required": []},
        "threshold_at_emission": 0.5,
    }, ctx)
    assert res["dropped"] is False
    assert res["anomaly"]["status"] == "pending"


def test_anomaly_emit_unknown_evidence_rejected(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    chans = install_anomaly_channels(sessions_root=sessions_root)
    ctx = _ctx(eng_dir / "events.jsonl")
    with pytest.raises(ChannelRejection, match="evidence_id not found"):
        chans["anomaly.emit"].call({
            "evidence_id": "e:notreal00", "crumb_id": "c1",
            "expected_shape_hash": "sha256:x",
            "divergence": {}, "threshold_at_emission": 0.5,
        }, ctx)


def test_anomaly_emit_caps_per_crumb_per_activation(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    chans = install_anomaly_channels(
        sessions_root=sessions_root, cap_per_crumb_per_activation=3,
    )
    ctx = _ctx(eng_dir / "events.jsonl")
    for i in range(5):
        rec = binder.put(f"x{i}".encode(), source="tool:curl",
                         source_metadata={}, summary="s")
        out = chans["anomaly.emit"].call({
            "evidence_id": rec.id, "crumb_id": "c1",
            "expected_shape_hash": "sha256:x",
            "divergence": {"raw_score": 0.6}, "threshold_at_emission": 0.5,
        }, ctx)
        if i < 3:
            assert out["dropped"] is False
        else:
            assert out["dropped"] is True


def test_anomaly_promote_creates_lead(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_anomaly_channels(sessions_root=sessions_root)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    emitted = chans["anomaly.emit"].call({
        "evidence_id": rec.id, "crumb_id": "c1",
        "expected_shape_hash": "sha256:x",
        "divergence": {"raw_score": 0.6}, "threshold_at_emission": 0.5,
    }, ctx_a)
    aid = emitted["anomaly"]["id"]
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    res = chans["anomaly.promote"].call({
        "anomaly_id": aid, "title": "Suspicious response with debug header",
    }, ctx_o)
    assert res["anomaly"]["status"] == "promoted_to_lead"
    assert res["lead"]["origin"] == "anomaly_promoted"


def test_anomaly_dismiss_marks_dismissed(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s")
    chans = install_anomaly_channels(sessions_root=sessions_root)
    ctx_a = _ctx(eng_dir / "events.jsonl")
    out = chans["anomaly.emit"].call({
        "evidence_id": rec.id, "crumb_id": "c1",
        "expected_shape_hash": "sha256:x",
        "divergence": {"raw_score": 0.6}, "threshold_at_emission": 0.5,
    }, ctx_a)
    aid = out["anomaly"]["id"]
    ctx_o = _ctx(eng_dir / "events.jsonl", caller="operator")
    res = chans["anomaly.dismiss"].call(
        {"anomaly_id": aid, "reason": "false signal"}, ctx_o,
    )
    assert res["anomaly"]["status"] == "dismissed_operator"
