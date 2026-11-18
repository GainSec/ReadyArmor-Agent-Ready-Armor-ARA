"""Audit #25 — shadow-pair similarity scoring + promote-ready
aggregation. Operator still clicks to promote; this just surfaces
candidates automatically."""

from __future__ import annotations

from ara.armory.views import (
    _aggregate_promote_readiness,
    _score_shadow_pair,
)


def _pair(rule_id: str, orig: str, variant: str) -> dict:
    return {
        "link": {
            "rule_id": rule_id,
            "original_response_body_text": orig,
        },
        "variant_response": {"body_text": variant},
    }


def test_score_identical_is_one():
    assert _score_shadow_pair(_pair("r", "abc", "abc")) == 1.0


def test_score_disjoint_is_zero():
    s = _score_shadow_pair(_pair("r", "abc", "xyz"))
    assert s == 0.0


def test_score_returns_none_when_missing_side():
    assert _score_shadow_pair({"link": {"rule_id": "r"},
                                "variant_response": {}}) is None
    assert _score_shadow_pair({"link": {}, "variant_response": {}}) is None


def test_aggregate_groups_by_rule_and_averages():
    pairs = [
        _pair("rule-A", "hello world", "hello world"),   # 1.0
        _pair("rule-A", "hello there", "hello world"),   # ~0.7
        _pair("rule-B", "abc", "xyz"),                    # 0.0
    ]
    agg = _aggregate_promote_readiness(pairs, min_samples=1)
    by_rid = {a["rule_id"]: a for a in agg}
    assert by_rid["rule-A"]["samples"] == 2
    assert 0.7 < by_rid["rule-A"]["avg"] <= 1.0
    assert by_rid["rule-B"]["samples"] == 1
    assert by_rid["rule-B"]["avg"] == 0.0


def test_promote_ready_requires_samples_and_avg():
    pairs_low_n = [
        _pair("rule-X", "same content", "same content") for _ in range(2)
    ]
    agg = _aggregate_promote_readiness(
        pairs_low_n, min_samples=5, min_avg_similarity=0.85,
    )
    assert agg and agg[0]["promote_ready"] is False

    pairs_high = [
        _pair("rule-Y", "same content", "same content") for _ in range(10)
    ]
    agg = _aggregate_promote_readiness(
        pairs_high, min_samples=5, min_avg_similarity=0.85,
    )
    assert agg and agg[0]["promote_ready"] is True


def test_aggregate_skips_pairs_with_no_scoreable_data():
    pairs = [
        {"link": {"rule_id": "r"}, "variant_response": {}},  # no variant body
        _pair("r", "hello", "hello"),
    ]
    agg = _aggregate_promote_readiness(pairs, min_samples=1)
    assert agg and agg[0]["samples"] == 1
