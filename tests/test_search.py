"""Tests for forge.index (3.1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ara.forge.index.builder import (
    FTSIndex,
    IndexEntry,
    index_facts,
    index_leads,
    index_manifest,
    index_weapon_components,
)
from ara.forge.index.calibration import (
    CalibrationQuery,
    precision_at_k,
    recall_at_k,
    run_calibration,
)
from ara.forge.index.dispatcher import SearchDispatcher
from ara.forge.index.embedder import (
    LocalOnnxEmbedder,
    NullEmbedder,
    make_embedder,
)
from ara.forge.index.synonyms import SynonymStore, read, write


# ---------- embedder ----------

def test_null_embedder_pin_descriptor() -> None:
    e = NullEmbedder()
    pin = e.pin_descriptor()
    assert pin["provider"] == "null"
    assert pin["dim"] is None
    assert e.verify_pin(pin) is True
    assert e.verify_pin({"provider": "local-onnx"}) is False


def test_make_embedder_factory() -> None:
    assert isinstance(make_embedder("null"), NullEmbedder)
    with pytest.raises(ValueError):
        make_embedder("unknown")


def test_local_onnx_embedder_imports_lazily() -> None:
    """If onnxruntime is installed, construction succeeds; otherwise it
    raises a helpful RuntimeError. Either way, importing the module
    doesn't raise.
    """
    try:
        e = LocalOnnxEmbedder()
        assert e.dim == 384
        # Placeholder embedding returns deterministic zero vectors
        out = e.embed(["hello"])
        assert len(out) == 1 and len(out[0]) == 384
    except RuntimeError as e:
        assert "requires onnxruntime" in str(e)


# ---------- synonyms ----------

def test_synonyms_expand_query() -> None:
    s = SynonymStore()
    s.add("xss", ["cross-site", "cross_site_scripting", "reflected"])
    s.add("sqli", ["sql_injection", "sql injection"])
    expanded = s.expand_query("xss")
    assert '"xss"' in expanded
    assert '"reflected"' in expanded
    assert " OR " in expanded


def test_synonyms_alias_to_canonical() -> None:
    s = SynonymStore()
    s.add("xss", ["reflected"])
    expanded = s.expand_query("reflected")
    # Variant in query should expand to the cluster
    assert '"xss"' in expanded
    assert '"reflected"' in expanded


def test_synonyms_persistence(tmp_path: Path) -> None:
    s = SynonymStore()
    s.add("auth", ["authn", "authentication"])
    p = tmp_path / "syn.json"
    write(p, s)
    s2 = read(p)
    assert "auth" in s2.canonical
    assert s2.alias_to_canonical.get("authn") == "auth"


# ---------- builder ----------

def test_fts_round_trip(tmp_path: Path) -> None:
    idx = FTSIndex(tmp_path / "fts.db")
    idx.add(IndexEntry(
        artifact_id="c:001", kind="crumb",
        summary="probe sql injection", body="check sqli on /search",
    ))
    assert idx.count() == 1
    assert idx.count(kind="crumb") == 1
    rows = idx.search('"sqli"', k=10)
    assert len(rows) == 1
    assert rows[0]["artifact_id"] == "c:001"
    idx.close()


def test_fts_kind_filter(tmp_path: Path) -> None:
    idx = FTSIndex(tmp_path / "fts.db")
    idx.add_many([
        IndexEntry("c:001", "crumb", "sqli", "sql injection"),
        IndexEntry("f:001", "fact", "sqli", "sql injection"),
    ])
    crumbs = idx.search('"sqli"', kind="crumb")
    facts = idx.search('"sqli"', kind="fact")
    assert len(crumbs) == 1 and crumbs[0]["kind"] == "crumb"
    assert len(facts) == 1 and facts[0]["kind"] == "fact"
    idx.close()


def test_index_weapon_components_extracts_buckets() -> None:
    draft = {
        "crumbs": [
            {"crumb_id": "c1", "name": "probe sqli", "tags": ["recon", "auth"]},
        ],
        "shapes": [{"shape_id": "s1", "name": "FindingFields"}],
        "stores": [{"store_id": "st1", "name": "payloads.sqli"}],
        "lines": [{"line_id": "l1", "rule": "out_of_scope: example.com/blog"}],
    }
    entries = index_weapon_components(draft)
    kinds = {e.kind for e in entries}
    assert kinds == {"crumb", "shape", "store", "line"}
    crumb = next(e for e in entries if e.kind == "crumb")
    assert "recon" in crumb.tags


def test_index_facts_summary_uses_predicate_value() -> None:
    facts = [{"id": "f:1", "predicate": "target.host", "value": "example.com"}]
    entries = index_facts(facts)
    assert entries[0].summary.startswith("target.host = example.com")


def test_index_leads() -> None:
    leads = [{"id": "L:1", "title": "IDOR on /api",
              "reasoning": "seq ids", "priority_computed": "high"}]
    entries = index_leads(leads)
    assert entries[0].kind == "lead"
    assert "IDOR" in entries[0].summary
    assert entries[0].tags == "high"


def test_index_manifest() -> None:
    elements = [{
        "id": "elem_001", "kind": "md_section",
        "extracted_text": "section body",
        "metadata": {"heading": "Introduction"},
    }]
    entries = index_manifest(elements)
    assert entries[0].summary == "Introduction"


# ---------- dispatcher ----------

def test_dispatcher_runs_fts_only_with_null_embedder(tmp_path: Path) -> None:
    idx = FTSIndex(tmp_path / "fts.db")
    idx.add_many([
        IndexEntry("c:001", "crumb", "probe sqli", "check sql injection"),
        IndexEntry("c:002", "crumb", "probe xss", "reflected cross-site"),
    ])
    syn = SynonymStore()
    syn.add("sqli", ["sql_injection"])
    disp = SearchDispatcher(idx, synonyms=syn, embedder=NullEmbedder())
    resp = disp.search("sqli")
    ids = [h.id for h in resp.hits]
    assert "c:001" in ids
    assert resp.embedding_used is False


def test_dispatcher_handles_empty_query(tmp_path: Path) -> None:
    idx = FTSIndex(tmp_path / "fts.db")
    idx.add(IndexEntry("c:001", "crumb", "x", "y"))
    disp = SearchDispatcher(idx)
    resp = disp.search("")
    assert resp.hits == []


def test_dispatcher_falls_back_on_bad_match_expr(tmp_path: Path) -> None:
    idx = FTSIndex(tmp_path / "fts.db")
    idx.add(IndexEntry("c:001", "crumb", "probe", "body about sqli"))
    disp = SearchDispatcher(idx)
    # FTS keywords as bare query — dispatcher should fall back to literal.
    resp = disp.search("OR AND NEAR")
    # Literal-quoted produces no hit but should not throw
    assert resp.elapsed_ms >= 0.0


# ---------- calibration ----------

def test_precision_at_k_recall_at_k() -> None:
    returned = ["a", "b", "c", "d", "e"]
    expected = ["a", "c", "z"]
    assert precision_at_k(returned, expected, 5) == 2 / 5
    assert recall_at_k(returned, expected, 10) == 2 / 3


def test_precision_at_k_empty() -> None:
    assert precision_at_k([], ["a"], 5) == 0.0
    assert recall_at_k(["a"], [], 5) == 1.0


def test_run_calibration_passes_with_aligned_index(tmp_path: Path) -> None:
    idx = FTSIndex(tmp_path / "fts.db")
    idx.add_many([
        IndexEntry("c:sqli", "crumb", "probe sqli", "sql injection check"),
        IndexEntry("c:xss", "crumb", "probe xss", "reflected cross-site"),
        IndexEntry("c:idor", "crumb", "probe idor", "insecure direct object reference"),
    ])
    disp = SearchDispatcher(idx)
    # Single-expected matches: precision@5 = 1/5 = 0.2 even on a perfect hit.
    queries = [
        CalibrationQuery(query="sqli", expected_top_ids=["c:sqli"], kind="crumb",
                         min_precision_at_5=0.2),
        CalibrationQuery(query="xss", expected_top_ids=["c:xss"], kind="crumb",
                         min_precision_at_5=0.2),
    ]
    report = run_calibration(disp, queries, weapon_id="w", seal_id="s",
                             global_min_p_at_5=0.2)
    assert report.queries_passed == 2
    assert report.queries_failed == 0
    assert report.seal_blocked is False
    assert report.mean_precision_at_5 > 0.0


def test_run_calibration_blocks_on_low_precision(tmp_path: Path) -> None:
    idx = FTSIndex(tmp_path / "fts.db")
    idx.add(IndexEntry("c:irrelevant", "crumb", "x", "y"))
    disp = SearchDispatcher(idx)
    queries = [
        CalibrationQuery(query="sqli", expected_top_ids=["c:does_not_exist"]),
    ]
    report = run_calibration(disp, queries, weapon_id="w", seal_id="s",
                             global_min_p_at_5=0.6)
    assert report.seal_blocked is True
    assert report.queries_failed == 1
