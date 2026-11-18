"""Tests for ``ara.runtime.trim_gate`` — the composite enforcement gate.

The gate composes the four v0.1 substrate modules (tod / rate_bucket /
stop_watcher) plus the tool classification registry. Each underlying
module is unit-tested separately; these tests exercise the *composition*
+ verdict shape so the supervisor (the eventual caller) can rely on
exactly one entry point with a stable contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ara import tool_registry as tr
from ara.runtime import (
    rate_bucket as rb,
    stop_watcher as sw,
    tod as tod_mod,
    trim_gate as tg,
)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _trim(**overrides) -> tg.TrimView:
    base = {
        "engagement_id": "eng1",
        "env": "nonprod",
        "posture": "blackbox",
        "active_testing": True,
        "tod_allowed": [],
        "rate_limits": {},
        "stop_conditions": [],
    }
    base.update(overrides)
    return tg.TrimView(**base)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_no_constraints_allows_everything(tmp_path: Path) -> None:
    v = tg.evaluate_call(
        tmp_path, trim=_trim(),
        op_kind="active", host="x.example",
        now_epoch=_utc(2026, 5, 4, 12),
    )
    assert v.allow is True
    assert v.gate == "composite"
    assert v.requires_token is None


def test_known_classified_tool_within_contract_allows(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True, intrusive=True),
        confidence=3, reasoning="version probe",
    )
    v = tg.evaluate_call(
        tmp_path, trim=_trim(active_testing=True),
        tool="nmap", args=["-sV", "host"],
        now_epoch=_utc(2026, 5, 4, 12),
    )
    assert v.allow is True


# ---------------------------------------------------------------------------
# TOD gate (highest priority)
# ---------------------------------------------------------------------------

def test_tod_blocks_when_outside_window(tmp_path: Path) -> None:
    win = [{"days": ["mon"], "start": "09:00", "end": "17:00", "tz": "UTC"}]
    v = tg.evaluate_call(
        tmp_path, trim=_trim(tod_allowed=win),
        op_kind="active",
        now_epoch=_utc(2026, 5, 4, 18),
    )
    assert v.allow is False
    assert v.gate == "tod"
    assert v.next_window_open_epoch is not None


def test_tod_extend_decision_lets_active_through(tmp_path: Path) -> None:
    win = [{"days": ["mon"], "start": "09:00", "end": "17:00", "tz": "UTC"}]
    now = _utc(2026, 5, 4, 18)
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_EXTEND,
        ttl_seconds=3600, now_epoch=now,
    )
    v = tg.evaluate_call(
        tmp_path, trim=_trim(tod_allowed=win),
        op_kind="active",
        now_epoch=now + 60,
    )
    assert v.allow is True


def test_tod_pivot_decision_blocks_active_allows_read(tmp_path: Path) -> None:
    win = [{"days": ["mon"], "start": "09:00", "end": "17:00", "tz": "UTC"}]
    now = _utc(2026, 5, 4, 18)
    tod_mod.grant_decision(
        tmp_path, "eng1", tod_mod.DECISION_PIVOT,
        ttl_seconds=1800, now_epoch=now,
    )
    v_active = tg.evaluate_call(
        tmp_path, trim=_trim(tod_allowed=win),
        op_kind="active", now_epoch=now + 60,
    )
    assert v_active.allow is False
    v_read = tg.evaluate_call(
        tmp_path, trim=_trim(tod_allowed=win),
        op_kind="read", now_epoch=now + 60,
    )
    assert v_read.allow is True


# ---------------------------------------------------------------------------
# Stop-watcher gate
# ---------------------------------------------------------------------------

def test_stop_condition_tripped_blocks(tmp_path: Path) -> None:
    summary = sw.FindingsSummary(total=1, by_severity={"critical": 1})
    v = tg.evaluate_call(
        tmp_path, trim=_trim(stop_conditions=["first_critical"]),
        op_kind="active",
        now_epoch=_utc(2026, 5, 4, 12),
        findings_summary=summary,
    )
    assert v.allow is False
    assert v.gate == "stop_watcher"
    assert "first critical" in v.reason


def test_stop_condition_persists_across_calls(tmp_path: Path) -> None:
    """Latch survives — second call after a clean summary still blocks."""
    sw.evaluate_and_latch(
        tmp_path, "eng1", conditions=["first_critical"],
        summary=sw.FindingsSummary(total=1, by_severity={"critical": 1}),
        now_epoch=_utc(2026, 5, 4, 11),
    )
    v = tg.evaluate_call(
        tmp_path, trim=_trim(stop_conditions=["first_critical"]),
        op_kind="active",
        now_epoch=_utc(2026, 5, 4, 12),
        findings_summary=sw.FindingsSummary(total=0),
    )
    assert v.allow is False
    assert v.gate == "stop_watcher"


# ---------------------------------------------------------------------------
# Tool classification gate
# ---------------------------------------------------------------------------

def test_unknown_tool_triggers_classification(tmp_path: Path) -> None:
    v = tg.evaluate_call(
        tmp_path, trim=_trim(),
        tool="nmap", args=["-sV", "host"],
        now_epoch=_utc(2026, 5, 4, 12),
    )
    assert v.allow is True
    assert v.requires_classification is True
    assert v.classification_prompt is not None
    assert "nmap" in v.classification_prompt
    assert v.classification_key == "nmap | -sV*"


def test_reclassify_flag_re_triggers_classification(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="",
    )
    tr.operator_request_reclassify(tmp_path, "nmap | -sV*")
    v = tg.evaluate_call(
        tmp_path, trim=_trim(),
        tool="nmap", args=["-sV"],
        now_epoch=_utc(2026, 5, 4, 12),
    )
    assert v.requires_classification is True


def test_pending_first_use_blocks(tmp_path: Path) -> None:
    """1/3 confidence → STATUS_PENDING_FIRST_USE → block until operator approves."""
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=1, reasoning="unsure",
    )
    v = tg.evaluate_call(
        tmp_path, trim=_trim(),
        tool="nmap", args=["-sV"],
        now_epoch=_utc(2026, 5, 4, 12),
    )
    assert v.allow is False
    assert v.gate == "tool_registry"
    assert "operator approval required" in v.reason


def test_passive_engagement_denies_active_tool(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="active scan",
    )
    v = tg.evaluate_call(
        tmp_path, trim=_trim(active_testing=False),
        tool="nmap", args=["-sV"],
        now_epoch=_utc(2026, 5, 4, 12),
    )
    assert v.allow is False
    assert "active_testing=false" in v.reason


def test_destructive_on_prod_requires_token(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="sqlmap | -u --batch*",
        classification=tr.Classification(active=True, destructive=True),
        confidence=3, reasoning="",
    )
    v = tg.evaluate_call(
        tmp_path, trim=_trim(env="prod", active_testing=True),
        tool="sqlmap", args=["-u", "https://x", "--batch"],
        now_epoch=_utc(2026, 5, 4, 12),
    )
    assert v.allow is True
    assert v.requires_token == "PROD-DESTRUCTIVE-APPROVAL"


# ---------------------------------------------------------------------------
# Rate-limit gate
# ---------------------------------------------------------------------------

def test_rate_limit_overflow_queue_returns_wait(tmp_path: Path) -> None:
    rb.seed_buckets(
        tmp_path, "eng1",
        rate_limits={"global_rps": 1}, now_epoch=100.0,
    )
    # First call drains the bucket.
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(),
        confidence=3, reasoning="",
    )
    tg.evaluate_call(
        tmp_path, trim=_trim(rate_limits={"global_rps": 1, "overflow": "queue"}),
        tool="x", args=[], host="any-host", now_epoch=100.0,
    )
    # Second call: bucket empty, overflow=queue → allow with wait_seconds.
    v = tg.evaluate_call(
        tmp_path, trim=_trim(rate_limits={"global_rps": 1, "overflow": "queue"}),
        tool="x", args=[], host="any-host", now_epoch=100.0,
    )
    assert v.allow is True
    assert v.wait_seconds > 0
    assert v.gate == "rate_bucket"


def test_rate_limit_overflow_deny_blocks(tmp_path: Path) -> None:
    rb.seed_buckets(
        tmp_path, "eng1",
        rate_limits={"global_rps": 1}, now_epoch=100.0,
    )
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(),
        confidence=3, reasoning="",
    )
    # Drain
    tg.evaluate_call(
        tmp_path, trim=_trim(rate_limits={"global_rps": 1, "overflow": "deny"}),
        tool="x", args=[], host="any-host", now_epoch=100.0,
    )
    v = tg.evaluate_call(
        tmp_path, trim=_trim(rate_limits={"global_rps": 1, "overflow": "deny"}),
        tool="x", args=[], host="any-host", now_epoch=100.0,
    )
    assert v.allow is False
    assert v.gate == "rate_bucket"


# ---------------------------------------------------------------------------
# Order-of-evaluation
# ---------------------------------------------------------------------------

def test_tod_outranks_classification(tmp_path: Path) -> None:
    """Out-of-window TOD should fire before the classification subtask
    is scheduled — no point asking the agent to classify a tool we'll
    refuse anyway."""
    win = [{"days": ["mon"], "start": "09:00", "end": "17:00", "tz": "UTC"}]
    v = tg.evaluate_call(
        tmp_path, trim=_trim(tod_allowed=win),
        tool="nmap", args=["-sV"],          # would otherwise trigger classify
        now_epoch=_utc(2026, 5, 4, 18),
    )
    assert v.gate == "tod"
    assert v.requires_classification is False


def test_stop_outranks_classification(tmp_path: Path) -> None:
    summary = sw.FindingsSummary(total=1, by_severity={"critical": 1})
    v = tg.evaluate_call(
        tmp_path, trim=_trim(stop_conditions=["first_critical"]),
        tool="nmap", args=["-sV"],
        now_epoch=_utc(2026, 5, 4, 12),
        findings_summary=summary,
    )
    assert v.gate == "stop_watcher"


# ---------------------------------------------------------------------------
# TrimView adapter
# ---------------------------------------------------------------------------

def test_view_from_trim_meta_projection() -> None:
    """A duck-typed object with the right fields projects cleanly."""
    class _Fake:
        engagement_id = "eng1"
        env = "prod"
        posture = "whitebox"
        active_testing = True
        tod_allowed = [{"days": ["mon"]}]
        rate_limits = {"global_rps": 5}
        stop_conditions = ["first_critical"]
    v = tg.view_from_trim_meta(_Fake(), paranoid=True)
    assert v.engagement_id == "eng1"
    assert v.env == "prod"
    assert v.posture == "whitebox"
    assert v.active_testing is True
    assert v.tod_allowed == [{"days": ["mon"]}]
    assert v.rate_limits == {"global_rps": 5}
    assert v.stop_conditions == ["first_critical"]
    assert v.paranoid_tool_gating is True


def test_view_from_trim_meta_handles_missing_fields() -> None:
    class _Bare:
        engagement_id = "eng1"
    v = tg.view_from_trim_meta(_Bare())
    assert v.env == "nonprod"
    assert v.posture == "blackbox"
    assert v.active_testing is False
    assert v.tod_allowed == []
    assert v.rate_limits == {}
    assert v.stop_conditions == []


# ---------------------------------------------------------------------------
# view_for_active_trim — supervisor turn-key helper
# ---------------------------------------------------------------------------

def test_view_for_active_trim_returns_none_when_no_armor(tmp_path: Path) -> None:
    assert tg.view_for_active_trim(tmp_path, None) is None
    assert tg.view_for_active_trim(tmp_path, "ghost") is None


def test_view_for_active_trim_returns_none_when_no_trim_applied(
    tmp_path: Path,
) -> None:
    from ara import armors as armors_mod
    armors_mod.create_armor(tmp_path, "blacksmith")
    assert tg.view_for_active_trim(tmp_path, "blacksmith") is None


def test_view_for_active_trim_loads_applied_trim(tmp_path: Path) -> None:
    from ara import armors as armors_mod
    from ara import trims as trims_mod
    armors_mod.create_armor(tmp_path, "blacksmith")
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith", engagement={
            "engagement_id": "acme",
            "operator_id": "ops",
            "target": {"primary": "https://x", "scope_allows": ["x"],
                       "env": "prod"},
            "posture": "whitebox", "active_testing": True,
            "rate_limits": {"global_rps": 10},
        },
    )
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    view = tg.view_for_active_trim(tmp_path, "blacksmith")
    assert view is not None
    assert view.engagement_id == "acme"
    assert view.env == "prod"
    assert view.posture == "whitebox"
    assert view.active_testing is True
    assert view.rate_limits == {"global_rps": 10}
