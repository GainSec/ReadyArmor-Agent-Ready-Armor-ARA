"""Tests for runtime.facts (record, store, neighbors, channels)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext, ChannelRejection
from ara.runtime.facts.channels import install_fact_channels
from ara.runtime.facts.neighbors import NeighborIndex
from ara.runtime.facts.record import Fact, content_hash, mint_id
from ara.runtime.facts.store import FactStore


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


# ---------- record ----------

def test_mint_id_format() -> None:
    fid = mint_id(
        predicate="t", value=1, asserted_by_crumb="c",
        evidence_refs=[], activation_id="a", asserted_at=1.0,
    )
    assert fid.startswith("f:")
    assert len(fid) == 2 + 10


def test_content_hash_stable_under_evidence_order() -> None:
    a = content_hash(predicate="p", value=1, asserted_by_crumb="c",
                     evidence_refs=["e:1", "e:2"], activation_id="a")
    b = content_hash(predicate="p", value=1, asserted_by_crumb="c",
                     evidence_refs=["e:2", "e:1"], activation_id="a")
    assert a == b


def test_fact_round_trip() -> None:
    f = Fact(
        id="f:abc1234567", predicate="p", value=42,
        asserted_by_crumb="c", evidence_refs=["e:abc"],
        chunk_at_time="recon", activation_id="act-1", asserted_at=time.time(),
    )
    f2 = Fact.from_jsonable(f.to_jsonable())
    assert f == f2


# ---------- store ----------

def _make_fact(**over):
    base = dict(
        predicate="target.tech.cms", value="wordpress",
        asserted_by_crumb="probe.cms", evidence_refs=["e:abc1234567"],
        chunk_at_time="recon", activation_id="act-1",
        asserted_at=time.time(),
    )
    base.update(over)
    return Fact(
        id=mint_id(
            predicate=base["predicate"], value=base["value"],
            asserted_by_crumb=base["asserted_by_crumb"],
            evidence_refs=base["evidence_refs"],
            activation_id=base["activation_id"],
            asserted_at=base["asserted_at"],
        ),
        **base,
    )


def test_store_append_and_read(tmp_path: Path) -> None:
    s = FactStore(tmp_path)
    f1 = _make_fact()
    s.append(f1)
    assert s.path.exists()
    assert s.by_id(f1.id) == f1
    s2 = FactStore(tmp_path)  # fresh load
    assert len(s2.all()) == 1


def test_store_dedup_on_content_hash(tmp_path: Path) -> None:
    s = FactStore(tmp_path)
    f1 = _make_fact(asserted_at=1.0)
    s.append(f1)
    # Different timestamp → different id, but same content
    f2 = _make_fact(asserted_at=2.0)
    out = s.append(f2)
    assert out.id == f1.id  # returned the prior
    assert len(s.all()) == 1


def test_store_by_predicate_prefix_and_exact(tmp_path: Path) -> None:
    s = FactStore(tmp_path)
    s.append(_make_fact(predicate="target.tech.cms", value="wp"))
    s.append(_make_fact(predicate="target.tech.lang", value="php"))
    s.append(_make_fact(predicate="target.host", value="example.com"))
    assert {f.predicate for f in s.by_predicate("target.tech")} == {
        "target.tech.cms", "target.tech.lang"
    }
    assert len(s.by_predicate("target.tech.cms", exact=True)) == 1


def test_store_supersede(tmp_path: Path) -> None:
    s = FactStore(tmp_path)
    orig = _make_fact(predicate="target.tech.cms", value="wp")
    s.append(orig)
    new = s.supersede(
        orig.id, new_value="wordpress-6.5",
        asserted_by_crumb="probe.cms",
        evidence_refs=["e:abc1234567"],
        chunk_at_time="recon",
        activation_id="act-2",
    )
    assert new.prior_fact == orig.id
    assert orig.id in new.depends_on
    hist = s.history("target.tech.cms")
    assert [f.value for f in hist] == ["wp", "wordpress-6.5"]


def test_store_by_evidence_and_by_crumb(tmp_path: Path) -> None:
    s = FactStore(tmp_path)
    s.append(_make_fact(predicate="a", value=1, evidence_refs=["e:111", "e:222"]))
    s.append(_make_fact(predicate="b", value=2, evidence_refs=["e:222"],
                        asserted_by_crumb="other"))
    assert len(s.by_evidence("e:222")) == 2
    assert len(s.by_crumb("other")) == 1


# ---------- neighbors ----------

def test_neighbor_index_categories(tmp_path: Path) -> None:
    s = FactStore(tmp_path)
    base_ts = time.time()
    f1 = _make_fact(predicate="p1", value=1, asserted_at=base_ts,
                    evidence_refs=["e:111"], asserted_by_crumb="c-shared")
    s.append(f1)
    f2 = _make_fact(predicate="p2", value=2, asserted_at=base_ts + 1,
                    evidence_refs=["e:111"], asserted_by_crumb="c-shared")
    s.append(f2)
    f3 = _make_fact(predicate="p3", value=3, asserted_at=base_ts + 2,
                    evidence_refs=["e:333"], asserted_by_crumb="other")
    s.append(f3)

    idx = NeighborIndex(s)
    nbh = idx.for_fact(f1.id, temporal_window_s=10.0)
    assert f2.id in nbh.causal       # same crumb
    assert f3.id not in nbh.causal
    assert f2.id in nbh.evidential   # shared evidence
    assert f2.id in nbh.temporal_after
    assert f3.id in nbh.temporal_after


def test_neighbor_semantic_walk_capped(tmp_path: Path) -> None:
    s = FactStore(tmp_path)
    a = _make_fact(predicate="a", value=1, evidence_refs=["e:111"])
    s.append(a)
    b = _make_fact(predicate="b", value=2, evidence_refs=["e:111"], asserted_at=time.time() + 0.1)
    s.append(b)
    # b depends on a
    s.supersede(
        a.id, new_value=99, asserted_by_crumb="c",
        evidence_refs=["e:111"], chunk_at_time="recon", activation_id="act-1",
    )
    # the superseded chain produced one downstream from `a`
    idx = NeighborIndex(s)
    nbh = idx.for_fact(a.id, semantic_depth=1)
    assert len(nbh.semantic_downstream) >= 1


# ---------- channels ----------

def _ctx_with_audit(sink: Path, *, engagement_id: str, activation_id: str) -> ChannelContext:
    return ChannelContext(
        engagement_id=engagement_id,
        activation_id=activation_id,
        caller="agent",
        audit_sink=sink,
    )


def _put_evidence(session_dir: Path, content: bytes = b"evidence") -> str:
    binder = EvidenceBinder(session_dir)
    rec = binder.put(content, source="tool:curl", source_metadata={},
                     summary="test")
    return rec.id


def test_assert_channel_happy_path(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng = "eng-1"
    session_dir = sessions_root / eng
    session_dir.mkdir(parents=True)
    ev_id = _put_evidence(session_dir)
    channels = install_fact_channels(sessions_root=sessions_root)
    ch = channels["fact.assert"]
    ctx = _ctx_with_audit(session_dir / "events.jsonl",
                          engagement_id=eng, activation_id="act-1")
    out = ch.call({
        "predicate": "target.host", "value": "example.com",
        "asserted_by_crumb": "probe.host",
        "evidence_refs": [ev_id],
    }, ctx)
    assert out["fact"]["predicate"] == "target.host"


def test_assert_channel_rejects_missing_evidence_and_deps(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng = "eng-1"
    session_dir = sessions_root / eng
    session_dir.mkdir(parents=True)
    channels = install_fact_channels(sessions_root=sessions_root)
    ch = channels["fact.assert"]
    ctx = _ctx_with_audit(session_dir / "events.jsonl",
                          engagement_id=eng, activation_id="act-1")
    with pytest.raises(ChannelRejection, match="empty"):
        ch.call({
            "predicate": "p", "value": 1, "asserted_by_crumb": "c",
            "evidence_refs": [], "depends_on": [],
        }, ctx)


def test_assert_channel_rejects_unresolved_evidence(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng = "eng-1"
    session_dir = sessions_root / eng
    session_dir.mkdir(parents=True)
    channels = install_fact_channels(sessions_root=sessions_root)
    ch = channels["fact.assert"]
    ctx = _ctx_with_audit(session_dir / "events.jsonl",
                          engagement_id=eng, activation_id="act-1")
    with pytest.raises(ChannelRejection, match="evidence_ref not found"):
        ch.call({
            "predicate": "p", "value": 1, "asserted_by_crumb": "c",
            "evidence_refs": ["e:notexist00"],
        }, ctx)


def test_query_channel_returns_facts(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng = "eng-1"
    session_dir = sessions_root / eng
    session_dir.mkdir(parents=True)
    ev_id = _put_evidence(session_dir)
    channels = install_fact_channels(sessions_root=sessions_root)
    assert_ch = channels["fact.assert"]
    query_ch = channels["facts.query"]
    ctx = _ctx_with_audit(session_dir / "events.jsonl",
                          engagement_id=eng, activation_id="act-1")
    for i in range(3):
        assert_ch.call({
            "predicate": f"target.tech.{i}", "value": i,
            "asserted_by_crumb": "c", "evidence_refs": [ev_id],
        }, ctx)
    out = query_ch.call({"predicate": "target.tech"}, ctx)
    assert len(out["facts"]) == 3


def test_neighbors_channel_returns_categories(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng = "eng-1"
    session_dir = sessions_root / eng
    session_dir.mkdir(parents=True)
    ev_id = _put_evidence(session_dir)
    channels = install_fact_channels(sessions_root=sessions_root)
    assert_ch = channels["fact.assert"]
    n_ch = channels["facts.neighbors"]
    ctx = _ctx_with_audit(session_dir / "events.jsonl",
                          engagement_id=eng, activation_id="act-1")
    f1 = assert_ch.call({
        "predicate": "p1", "value": 1, "asserted_by_crumb": "shared",
        "evidence_refs": [ev_id],
    }, ctx)["fact"]
    assert_ch.call({
        "predicate": "p2", "value": 2, "asserted_by_crumb": "shared",
        "evidence_refs": [ev_id],
    }, ctx)
    out = n_ch.call({"fact_id": f1["id"]}, ctx)
    assert out["center"] == f1["id"]
    assert len(out["causal"]) == 1
    assert len(out["evidential"]) == 1
