"""Tests for the four v0.1 runtime hook modules:

- ``ara.runtime.tod`` (TOD evaluator + 4-way modal state).
- ``ara.runtime.rate_bucket`` (per-(eid, host) token bucket).
- ``ara.runtime.stop_watcher`` (stop-condition evaluator + latch).
- ``ara.runtime.knowledge_gap`` (recon precondition).

All four are pure-function-shaped with small persistent state (where
needed). These tests exercise the pure logic + the persistence
round-trip.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ara.runtime import (
    knowledge_gap as kg,
    rate_bucket as rb,
    stop_watcher as sw,
    tod as tod_mod,
)


# ---------------------------------------------------------------------------
# TOD — pure evaluation
# ---------------------------------------------------------------------------

def _utc_epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_in_any_window_unrestricted_when_empty() -> None:
    assert tod_mod.in_any_window([], _utc_epoch(2026, 5, 1, 12)) is True


def test_in_any_window_matches_weekday_and_time() -> None:
    # 2026-05-04 is a Monday.
    window = [{
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "start": "09:00", "end": "17:00", "tz": "UTC",
    }]
    # Mon 2026-05-04 14:00 UTC → in window.
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 4, 14)) is True
    # Sat 2026-05-02 14:00 UTC → out (wrong day).
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 2, 14)) is False
    # Mon 2026-05-04 18:00 UTC → out (after end).
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 4, 18)) is False
    # Mon 2026-05-04 17:00 UTC → out (end is exclusive).
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 4, 17)) is False


def test_in_any_window_handles_wrap_around_midnight() -> None:
    # 22:00–06:00 — wraps midnight.
    window = [{
        "days": ["fri"], "start": "22:00", "end": "06:00", "tz": "UTC",
    }]
    # Fri 2026-05-01 23:30 UTC → in (post-22:00 same day).
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 1, 23, 30)) is True
    # Sat 2026-05-02 02:00 UTC → in (post-midnight half, owned by Friday).
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 2, 2)) is True
    # Sat 2026-05-02 07:00 UTC → out.
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 2, 7)) is False


def test_in_any_window_supports_zoneinfo_tz() -> None:
    """Spec example: Mon–Fri 09–17 America/New_York. 14:00 UTC on
    Mon is 10:00 NYT (EDT in May)."""
    window = [{
        "days": ["mon"], "start": "09:00", "end": "17:00",
        "tz": "America/New_York",
    }]
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 4, 14)) is True
    # Mon 21:00 UTC = Mon 17:00 NYT (EDT) — exclusive end.
    assert tod_mod.in_any_window(window, _utc_epoch(2026, 5, 4, 21)) is False


def test_next_window_open_returns_none_when_inside() -> None:
    window = [{
        "days": ["mon"], "start": "09:00", "end": "17:00", "tz": "UTC",
    }]
    assert tod_mod.next_window_open(
        window, _utc_epoch(2026, 5, 4, 12),
    ) is None


def test_next_window_open_returns_future_open() -> None:
    window = [{
        "days": ["mon"], "start": "09:00", "end": "17:00", "tz": "UTC",
    }]
    # Sun 2026-05-03 12:00 UTC — next open is Mon 09:00 UTC = +21h.
    res = tod_mod.next_window_open(window, _utc_epoch(2026, 5, 3, 12))
    expected = _utc_epoch(2026, 5, 4, 9)
    assert res is not None
    assert abs(res - expected) <= 60.0


def test_next_window_open_returns_none_when_no_window_in_lookahead() -> None:
    # Single window only on the 1st of February 2099 — no chance.
    window = [{
        "days": ["mon"], "start": "09:00", "end": "10:00",
        "tz": "Asia/Pyongyang",
    }]
    res = tod_mod.next_window_open(
        window, _utc_epoch(2026, 5, 1, 12),
        lookahead_seconds=60,            # tiny window
    )
    assert res is None


# ---------------------------------------------------------------------------
# TOD — decision state + composite gate
# ---------------------------------------------------------------------------

_WIN_MON = [{
    "days": ["mon"], "start": "09:00", "end": "17:00", "tz": "UTC",
}]


def test_grant_decision_persists_and_loads(tmp_path: Path) -> None:
    rec = tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_EXTEND,
        granted_by="ops", ttl_seconds=3600,
        reason="active exploit chain mid-test",
        now_epoch=_utc_epoch(2026, 5, 4, 17, 30),
    )
    assert rec.decision == "continue_for_ttl"
    assert rec.expires_at is not None

    st = tod_mod.load_state(tmp_path, "eng1")
    assert st.current_state == "extended"
    assert st.active_decision is not None
    assert st.active_decision.ttl_seconds == 3600


def test_grant_decision_rejects_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        tod_mod.grant_decision(tmp_path, "eng1", "garbage")


def test_grant_decision_extend_requires_ttl(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        tod_mod.grant_decision(tmp_path, "eng1", tod_mod.DECISION_EXTEND)


def test_evaluate_gate_within_window_allows(tmp_path: Path) -> None:
    res = tod_mod.evaluate_gate(
        tmp_path, "eng1",
        tod_allowed=_WIN_MON, now_epoch=_utc_epoch(2026, 5, 4, 12),
    )
    assert res.allow is True
    assert res.state == "in_window"


def test_evaluate_gate_outside_window_blocks(tmp_path: Path) -> None:
    res = tod_mod.evaluate_gate(
        tmp_path, "eng1",
        tod_allowed=_WIN_MON, now_epoch=_utc_epoch(2026, 5, 4, 18),
    )
    assert res.allow is False
    assert res.requires_decision is True
    assert res.next_window_open_epoch is not None


def test_evaluate_gate_extend_overrides_window(tmp_path: Path) -> None:
    now = _utc_epoch(2026, 5, 4, 17, 30)
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_EXTEND,
        ttl_seconds=3600, now_epoch=now,
    )
    res = tod_mod.evaluate_gate(
        tmp_path, "eng1",
        tod_allowed=_WIN_MON, now_epoch=now + 60,
    )
    assert res.allow is True
    assert res.state == "extended"


def test_evaluate_gate_extend_expires_into_window_state(tmp_path: Path) -> None:
    now = _utc_epoch(2026, 5, 4, 17, 30)
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_EXTEND,
        ttl_seconds=600, now_epoch=now,
    )
    # +1h later — extend is expired; we're back to plain TOD which is also
    # outside the window (it ended at 17:00 — we're at 18:30).
    res = tod_mod.evaluate_gate(
        tmp_path, "eng1",
        tod_allowed=_WIN_MON, now_epoch=now + 3600,
    )
    assert res.allow is False
    assert res.state == "blocked"
    # Latched state was reset to in_window at expiry.
    st = tod_mod.load_state(tmp_path, "eng1")
    assert st.current_state == "in_window"
    assert st.active_decision is None


def test_evaluate_gate_pivot_allows_only_reads(tmp_path: Path) -> None:
    now = _utc_epoch(2026, 5, 4, 17, 30)
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_PIVOT,
        ttl_seconds=1800, now_epoch=now,
    )
    res_read = tod_mod.evaluate_gate(
        tmp_path, "eng1", tod_allowed=_WIN_MON,
        now_epoch=now + 60, op_kind="read",
    )
    assert res_read.allow is True
    res_active = tod_mod.evaluate_gate(
        tmp_path, "eng1", tod_allowed=_WIN_MON,
        now_epoch=now + 60, op_kind="active",
    )
    assert res_active.allow is False


def test_evaluate_gate_stop_blocks_everything(tmp_path: Path) -> None:
    now = _utc_epoch(2026, 5, 4, 17, 30)
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_STOP, now_epoch=now,
    )
    res = tod_mod.evaluate_gate(
        tmp_path, "eng1", tod_allowed=_WIN_MON,
        now_epoch=now + 60, op_kind="read",
    )
    assert res.allow is False
    assert res.next_window_open_epoch is not None


def test_evaluate_gate_interactive_marks_requires_decision(tmp_path: Path) -> None:
    now = _utc_epoch(2026, 5, 4, 17, 30)
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_INTERACTIVE, now_epoch=now,
    )
    res = tod_mod.evaluate_gate(
        tmp_path, "eng1", tod_allowed=_WIN_MON,
        now_epoch=now + 60, op_kind="active",
    )
    assert res.allow is False
    assert res.requires_decision is True


def test_reset_on_window_open_archives_decision(tmp_path: Path) -> None:
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_STOP,
        now_epoch=_utc_epoch(2026, 5, 4, 18),
    )
    tod_mod.reset_on_window_open(tmp_path, "eng1")
    st = tod_mod.load_state(tmp_path, "eng1")
    assert st.current_state == "in_window"
    assert st.active_decision is None
    assert len(st.history) == 1
    assert st.history[0]["decision"] == "stop_until_window"


# ---------------------------------------------------------------------------
# rate_bucket
# ---------------------------------------------------------------------------

def test_refill_caps_at_capacity() -> None:
    b = rb.Bucket(capacity=10.0, refill_rate=10.0, tokens=5.0, last_refill=100.0)
    rb.refill(b, 200.0)             # 100s × 10 RPS = 1000 tokens, capped.
    assert b.tokens == 10.0
    assert b.last_refill == 200.0


def test_try_consume_grants_when_enough_tokens() -> None:
    b = rb.Bucket(10.0, 10.0, 5.0, 100.0)
    res = rb.try_consume(b, 3.0, 100.5)        # +5 tokens, then -3
    assert res.granted is True
    assert res.bucket_after.tokens == pytest.approx(7.0, abs=0.01)


def test_try_consume_denies_with_wait_seconds() -> None:
    b = rb.Bucket(10.0, 10.0, 0.0, 100.0)
    res = rb.try_consume(b, 5.0, 100.0)
    assert res.granted is False
    assert res.wait_seconds == pytest.approx(0.5)
    assert res.bucket_after.tokens == 0.0


def test_try_consume_unlimited_when_capacity_zero() -> None:
    b = rb.Bucket(0.0, 0.0, 0.0, 100.0)
    res = rb.try_consume(b, 9999.0, 100.0)
    assert res.granted is True
    assert "unlimited" in res.reason


def test_seed_buckets_creates_per_host_and_global(tmp_path: Path) -> None:
    out = rb.seed_buckets(
        tmp_path, "eng1",
        rate_limits={"global_rps": 10, "per_host": {"portal.acme.com": 5}},
        now_epoch=100.0,
    )
    assert "__global__" in out and "portal.acme.com" in out
    assert out["__global__"].capacity == 10.0
    assert out["portal.acme.com"].capacity == 5.0


def test_seed_buckets_preserves_running_tokens_on_reseed(tmp_path: Path) -> None:
    rb.seed_buckets(
        tmp_path, "eng1",
        rate_limits={"global_rps": 10}, now_epoch=100.0,
    )
    res = rb.acquire(tmp_path, "eng1", "any-host", n=8.0, now_epoch=100.0)
    assert res.granted is True
    # Running budget: 10 - 8 = 2 tokens left.
    rb.seed_buckets(
        tmp_path, "eng1",
        rate_limits={"global_rps": 10}, now_epoch=100.5,
    )
    b = rb.get_bucket(tmp_path, "eng1", "__global__")
    assert b is not None
    # Should preserve running tokens (2.0), not reset to 10.0.
    assert b.tokens == pytest.approx(2.0, abs=0.01)


def test_acquire_unlimited_when_no_buckets(tmp_path: Path) -> None:
    res = rb.acquire(tmp_path, "eng1", "any-host", now_epoch=100.0)
    assert res.granted is True
    assert "no rate-limit bucket" in res.reason


def test_acquire_per_host_then_global(tmp_path: Path) -> None:
    rb.seed_buckets(
        tmp_path, "eng1",
        rate_limits={"global_rps": 100, "per_host": {"x": 5}},
        now_epoch=100.0,
    )
    # Drain per-host first.
    for _ in range(5):
        assert rb.acquire(tmp_path, "eng1", "x", now_epoch=100.0).granted is True
    res = rb.acquire(tmp_path, "eng1", "x", now_epoch=100.0)
    assert res.granted is False
    # Other host shares only the global bucket — still has 95.
    res2 = rb.acquire(tmp_path, "eng1", "y", now_epoch=100.0)
    assert res2.granted is True


def test_acquire_global_denial_after_host_grant(tmp_path: Path) -> None:
    rb.seed_buckets(
        tmp_path, "eng1",
        rate_limits={"global_rps": 1, "per_host": {"x": 100}},
        now_epoch=100.0,
    )
    # First call: host grants, global grants.
    assert rb.acquire(tmp_path, "eng1", "x", now_epoch=100.0).granted is True
    # Second call: host grants, global denies.
    res = rb.acquire(tmp_path, "eng1", "x", now_epoch=100.0)
    assert res.granted is False


# ---------------------------------------------------------------------------
# stop_watcher
# ---------------------------------------------------------------------------

def _summary(total: int = 0, **by_severity: int) -> sw.FindingsSummary:
    return sw.FindingsSummary(total=total, by_severity=dict(by_severity))


def test_evaluate_first_critical_trips_on_critical() -> None:
    v = sw.evaluate(
        ["first_critical"], _summary(total=1, critical=1), now_epoch=0,
    )
    assert v.tripped is True and v.condition == "first_critical"


def test_evaluate_first_critical_no_trip_without() -> None:
    v = sw.evaluate(
        ["first_critical"], _summary(total=3, high=2, medium=1),
        now_epoch=0,
    )
    assert v.tripped is False


def test_evaluate_max_findings() -> None:
    v = sw.evaluate(["max_findings:5"], _summary(total=5), now_epoch=0)
    assert v.tripped is True
    v2 = sw.evaluate(["max_findings:5"], _summary(total=4), now_epoch=0)
    assert v2.tripped is False


def test_evaluate_duration_exceeded() -> None:
    start = 1_000_000.0
    v = sw.evaluate(
        ["duration_exceeded:1h"], _summary(total=0),
        now_epoch=start + 3601, window_start_epoch=start,
    )
    assert v.tripped is True
    v_before = sw.evaluate(
        ["duration_exceeded:1h"], _summary(total=0),
        now_epoch=start + 100, window_start_epoch=start,
    )
    assert v_before.tripped is False


def test_evaluate_first_match_wins() -> None:
    # Both would trip; declaration order should pick the first.
    v = sw.evaluate(
        ["first_critical", "max_findings:1"],
        _summary(total=1, critical=1), now_epoch=0,
    )
    assert v.condition == "first_critical"


def test_evaluate_unknown_condition_skipped() -> None:
    v = sw.evaluate(
        ["unknown_thing", "first_critical"],
        _summary(total=1, critical=1), now_epoch=0,
    )
    assert v.tripped is True


def test_latch_persists_first_trip(tmp_path: Path) -> None:
    sw.evaluate_and_latch(
        tmp_path, "eng1", conditions=["first_critical"],
        summary=_summary(total=1, critical=1), now_epoch=100.0,
    )
    latch = sw.load_latch(tmp_path, "eng1")
    assert latch.tripped is True
    assert latch.condition == "first_critical"
    assert latch.tripped_at is not None


def test_latch_returns_same_verdict_on_second_call(tmp_path: Path) -> None:
    sw.evaluate_and_latch(
        tmp_path, "eng1", conditions=["first_critical"],
        summary=_summary(total=1, critical=1), now_epoch=100.0,
    )
    # Even with a clean summary later, the latch sticks.
    v = sw.evaluate_and_latch(
        tmp_path, "eng1", conditions=["first_critical"],
        summary=_summary(total=0), now_epoch=200.0,
    )
    assert v.tripped is True
    assert "latched" in v.reason


def test_reset_latch_clears(tmp_path: Path) -> None:
    sw.evaluate_and_latch(
        tmp_path, "eng1", conditions=["first_critical"],
        summary=_summary(total=1, critical=1), now_epoch=100.0,
    )
    sw.reset_latch(tmp_path, "eng1")
    latch = sw.load_latch(tmp_path, "eng1")
    assert latch.tripped is False


# ---------------------------------------------------------------------------
# knowledge_gap
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def count(self, shape: str) -> int:
        return self._counts.get(shape, 0)


def test_parse_produces_strings_and_dicts() -> None:
    out = kg.parse_produces([
        "subdomain.host",
        {"shape": "endpoint.url", "min_count": 5},
        "",                             # blank skipped
        {"shape": ""},                  # blank skipped
    ])
    assert len(out) == 2
    assert out[0].shape == "subdomain.host" and out[0].min_count == 1
    assert out[1].shape == "endpoint.url" and out[1].min_count == 5


def test_has_sufficient_coverage_skips_when_all_satisfied() -> None:
    store = _FakeStore({"subdomain.host": 5, "endpoint.url": 12})
    reqs = [
        kg.CoverageRequirement("subdomain.host", 1),
        kg.CoverageRequirement("endpoint.url", 10),
    ]
    res = kg.has_sufficient_coverage(reqs, store)
    assert res.skip is True
    assert set(res.matched) == {"subdomain.host", "endpoint.url"}
    assert "all 2 shapes already covered" in res.evidence


def test_has_sufficient_coverage_runs_when_any_missing() -> None:
    store = _FakeStore({"subdomain.host": 5})        # endpoint.url missing
    reqs = [
        kg.CoverageRequirement("subdomain.host", 1),
        kg.CoverageRequirement("endpoint.url", 1),
    ]
    res = kg.has_sufficient_coverage(reqs, store)
    assert res.skip is False
    assert res.matched == ["subdomain.host"]
    assert len(res.missing) == 1


def test_has_sufficient_coverage_runs_when_below_min_count() -> None:
    store = _FakeStore({"endpoint.url": 3})
    reqs = [kg.CoverageRequirement("endpoint.url", 10)]
    res = kg.has_sufficient_coverage(reqs, store)
    assert res.skip is False


def test_has_sufficient_coverage_no_requirements_runs() -> None:
    """Crumbs without ``produces`` declared can't be skipped — they
    might be doing work the fact store doesn't reflect."""
    res = kg.has_sufficient_coverage([], _FakeStore({}))
    assert res.skip is False


def test_override_token_for_crumb_id() -> None:
    assert kg.override_token_for("wstg-info-006") == \
        "RUN-RECON-DESPITE-KNOWLEDGE:wstg-info-006"
