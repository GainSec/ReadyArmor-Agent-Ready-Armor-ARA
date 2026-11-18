"""Provenance enrichment: auto-correlate the response-bearing primary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ara.runtime.provenance import enrich_evidence
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.binder import sources as _srcs


def _ctx(act="act-1", eng="eng-1"):
    return SimpleNamespace(activation_id=act, engagement_id=eng)


def _session(tmp_path: Path):
    sd = tmp_path / "sessions" / "eng-1"
    sd.mkdir(parents=True)
    return sd, tmp_path  # session_dir, state_root


def test_autocorrelates_response_bearing_primary(tmp_path: Path) -> None:
    sd, state = _session(tmp_path)
    binder = EvidenceBinder(sd)
    tool = binder.put(b"STATUS 200 body", source="tool:terminal", source_metadata={},
                      summary="o", activation_id="act-1")
    body: dict = {"evidence_refs": []}
    added = enrich_evidence(body, binder=binder, ctx=_ctx(), session_dir=sd, state_root=state)
    assert tool.id in body["evidence_refs"]
    assert added["primary"] == [tool.id]
    # the bound primary is response-bearing
    assert _srcs.is_response_bearing(binder.get_record(tool.id).source)


def test_no_response_bearing_adds_no_primary(tmp_path: Path) -> None:
    sd, state = _session(tmp_path)
    binder = EvidenceBinder(sd)
    spawn = binder.put(b"cmd", source="shim:spawn", source_metadata={"command": "c"},
                       summary="c", activation_id="act-1")
    body: dict = {"evidence_refs": [spawn.id]}
    added = enrich_evidence(body, binder=binder, ctx=_ctx(), session_dir=sd, state_root=state)
    assert added["primary"] == []
    assert body["evidence_refs"] == [spawn.id]  # unchanged (nothing to correlate)


def test_skips_when_agent_already_cited_response_bearing(tmp_path: Path) -> None:
    sd, state = _session(tmp_path)
    binder = EvidenceBinder(sd)
    tool = binder.put(b"body", source="tool:curl", source_metadata={},
                      summary="o", activation_id="act-1")
    binder.put(b"other body", source="tool:terminal", source_metadata={},
               summary="o2", activation_id="act-1")
    body: dict = {"evidence_refs": [tool.id]}
    added = enrich_evidence(body, binder=binder, ctx=_ctx(), session_dir=sd, state_root=state)
    assert added["primary"] == []  # already has a response-bearing ref


def test_never_raises_on_bad_input(tmp_path: Path) -> None:
    sd, state = _session(tmp_path)
    binder = EvidenceBinder(sd)
    enrich_evidence({}, binder=binder, ctx=_ctx(act=None), session_dir=sd, state_root=state)
    enrich_evidence({"evidence_refs": None}, binder=binder, ctx=_ctx(), session_dir=sd, state_root=state)
