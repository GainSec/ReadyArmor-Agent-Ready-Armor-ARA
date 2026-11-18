"""Tests for the v0.1 §8 crumb-intent channel — knowledge-gap precondition.

Channel is consulted before firing a recon-class crumb. Returns
``fire`` or ``skip`` based on whether the crumb's ``produces``
shapes are already covered in the engagement's fact store.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ara import tokens as tk
from ara.runtime.anomaly.crumb_tracker import CrumbTracker
from ara.runtime.channels.base import ChannelContext
from ara.runtime.chunks.crumb_intent import (
    CrumbIntentChannel, FactStorePredicateAdapter,
)


class _FakeStore:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def count(self, shape: str) -> int:
        return self._counts.get(shape, 0)


def _ctx() -> ChannelContext:
    return ChannelContext(caller="agent", engagement_id="eng1",
                          activation_id="act1", audit_sink=None)


def _channel(
    tmp_path: Path, store_counts: dict[str, int],
    *, with_tracker: bool = False,
) -> CrumbIntentChannel:
    tracker = CrumbTracker() if with_tracker else None
    return CrumbIntentChannel(
        fact_store=_FakeStore(store_counts),
        state_root=tmp_path, crumb_tracker=tracker,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_no_produces_always_fires(tmp_path: Path) -> None:
    ch = _channel(tmp_path, {})
    res = ch.execute(
        {"crumb_id": "wstg-info-006", "produces": []},
        _ctx(),
    )
    assert res["decision"] == "fire"


def test_skip_when_all_produces_covered(tmp_path: Path) -> None:
    ch = _channel(tmp_path, {"subdomain.host": 8, "endpoint.url": 42})
    res = ch.execute(
        {"crumb_id": "wstg-info-006",
         "produces": ["subdomain.host", "endpoint.url"]},
        _ctx(),
    )
    assert res["decision"] == "skip"
    assert res["evidence"] == "from-mats"
    assert set(res["matched_shapes"]) == {"subdomain.host", "endpoint.url"}


def test_fire_when_any_produce_missing(tmp_path: Path) -> None:
    ch = _channel(tmp_path, {"subdomain.host": 8})  # endpoint.url empty
    res = ch.execute(
        {"crumb_id": "wstg-info-006",
         "produces": ["subdomain.host", "endpoint.url"]},
        _ctx(),
    )
    assert res["decision"] == "fire"
    assert "endpoint.url" in res["missing_shapes"]


def test_fire_when_below_min_count(tmp_path: Path) -> None:
    ch = _channel(tmp_path, {"endpoint.url": 3})
    res = ch.execute(
        {"crumb_id": "wstg-info-006",
         "produces": [{"shape": "endpoint.url", "min_count": 10}]},
        _ctx(),
    )
    assert res["decision"] == "fire"


# ---------------------------------------------------------------------------
# Operator override token
# ---------------------------------------------------------------------------

def test_override_token_forces_fire_even_when_covered(tmp_path: Path) -> None:
    """If RUN-RECON-DESPITE-KNOWLEDGE:<crumb_id> is set, the channel
    consumes it (one-time) and returns fire even with full coverage."""
    tk.set_token(
        "RUN-RECON-DESPITE-KNOWLEDGE:wstg-info-006",
        reason="operator wants verification re-recon",
        state_root=tmp_path,
    )
    ch = _channel(tmp_path, {"subdomain.host": 100, "endpoint.url": 100})
    res = ch.execute(
        {"crumb_id": "wstg-info-006",
         "produces": ["subdomain.host", "endpoint.url"]},
        _ctx(),
    )
    assert res["decision"] == "fire"
    assert "override token" in res["reason"]
    assert "RUN-RECON-DESPITE-KNOWLEDGE:wstg-info-006" in res["token_consumed"]
    # Token consumed (one-time).
    assert not tk.has_token(
        "RUN-RECON-DESPITE-KNOWLEDGE:wstg-info-006", state_root=tmp_path,
    )


def test_override_token_for_different_crumb_does_not_apply(tmp_path: Path) -> None:
    tk.set_token(
        "RUN-RECON-DESPITE-KNOWLEDGE:other-crumb",
        reason="for different crumb",
        state_root=tmp_path,
    )
    ch = _channel(tmp_path, {"subdomain.host": 100})
    res = ch.execute(
        {"crumb_id": "wstg-info-006",
         "produces": ["subdomain.host"]},
        _ctx(),
    )
    # Crumb is fully covered, no per-crumb token → skip.
    assert res["decision"] == "skip"
    # Other crumb's token NOT consumed.
    assert tk.has_token(
        "RUN-RECON-DESPITE-KNOWLEDGE:other-crumb", state_root=tmp_path,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_missing_crumb_id_rejects(tmp_path: Path) -> None:
    from ara.runtime.channels.base import ChannelRejection
    ch = _channel(tmp_path, {})
    with pytest.raises(ChannelRejection):
        ch.execute({"crumb_id": "", "produces": []}, _ctx())


def test_dict_produces_with_min_count(tmp_path: Path) -> None:
    ch = _channel(tmp_path, {"endpoint.url": 12})
    res = ch.execute(
        {"crumb_id": "x",
         "produces": [{"shape": "endpoint.url", "min_count": 10}]},
        _ctx(),
    )
    assert res["decision"] == "skip"


# ---------------------------------------------------------------------------
# Activation behavior — fire decisions update the CrumbTracker
# ---------------------------------------------------------------------------

def test_fire_decision_activates_crumb_tracker(tmp_path: Path) -> None:
    ch = _channel(tmp_path, {}, with_tracker=True)
    res = ch.execute(
        {"crumb_id": "wstg-info-006", "produces": ["subdomain.host"]},
        _ctx(),
    )
    assert res["decision"] == "fire"
    assert ch.crumb_tracker.current() == "wstg-info-006"


def test_skip_decision_does_not_activate_tracker(tmp_path: Path) -> None:
    ch = _channel(tmp_path, {"subdomain.host": 5}, with_tracker=True)
    res = ch.execute(
        {"crumb_id": "wstg-info-006", "produces": ["subdomain.host"]},
        _ctx(),
    )
    assert res["decision"] == "skip"
    # Tracker stays at None — agent did NOT activate this crumb.
    assert ch.crumb_tracker.current() is None


def test_override_token_fire_also_activates(tmp_path: Path) -> None:
    tk.set_token(
        "RUN-RECON-DESPITE-KNOWLEDGE:wstg-info-006",
        reason="verify recon", state_root=tmp_path,
    )
    ch = _channel(tmp_path, {"subdomain.host": 100}, with_tracker=True)
    res = ch.execute(
        {"crumb_id": "wstg-info-006", "produces": ["subdomain.host"]},
        _ctx(),
    )
    assert res["decision"] == "fire"
    assert "token_consumed" in res
    assert ch.crumb_tracker.current() == "wstg-info-006"


def test_no_tracker_still_works(tmp_path: Path) -> None:
    """Channel without tracker still functions as a pure knowledge-gap
    check — useful for tests + scripted callers."""
    ch = _channel(tmp_path, {"subdomain.host": 5})  # no tracker
    res = ch.execute(
        {"crumb_id": "x", "produces": ["subdomain.host"]},
        _ctx(),
    )
    assert res["decision"] == "skip"


# ---------------------------------------------------------------------------
# FactStorePredicateAdapter — wraps real FactStore for knowledge_gap
# ---------------------------------------------------------------------------

class _FakeRealFactStore:
    """Mimics the runtime FactStore.by_predicate interface."""
    def __init__(self) -> None:
        self.predicates: dict[str, int] = {}

    def by_predicate(self, predicate: str, *, exact: bool = False) -> list:
        if not exact:
            return [None] * sum(
                v for k, v in self.predicates.items() if k.startswith(predicate)
            )
        return [None] * self.predicates.get(predicate, 0)


def test_predicate_adapter_counts_via_by_predicate() -> None:
    real = _FakeRealFactStore()
    real.predicates["subdomain.host"] = 5
    real.predicates["endpoint.url"] = 0
    adapter = FactStorePredicateAdapter(real)
    assert adapter.count("subdomain.host") == 5
    assert adapter.count("endpoint.url") == 0
    assert adapter.count("missing.shape") == 0


def test_predicate_adapter_swallows_errors() -> None:
    class _Broken:
        def by_predicate(self, *a, **kw):
            raise RuntimeError("boom")
    adapter = FactStorePredicateAdapter(_Broken())
    # Best-effort: errors → 0 so the gate degrades to "fire" rather
    # than "skip" (safer default — runs recon when fact store is
    # broken rather than silently skipping).
    assert adapter.count("anything") == 0
