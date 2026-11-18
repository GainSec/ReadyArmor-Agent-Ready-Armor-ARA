"""Tests for forge.chunks (linter, templates) + runtime.chunks.freshness."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ara.forge.chunks.chunkdef import ChunkDef, ExitTarget, GemRef, read_chunkdef, write_chunkdef
from ara.forge.chunks.linter import lint_graph
from ara.forge.chunks.templates import PROBE_TEMPLATE
from ara.runtime import audit as raudit
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.chunks.freshness import evidence_in_current_activation


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()


def test_chunkdef_round_trip(tmp_path: Path) -> None:
    c = ChunkDef(
        id="enum", title="Enumerate",
        prompt_fragment="enumerate scope",
        gems=[GemRef(layer="net", rule_subset="scope_strict")],
        tool_surface=["evidence.search"],
        exits={
            "<done>": ExitTarget(tag="<done>", next_chunk_id="probe",
                                  requires_caller="agent"),
        },
        is_entry=True,
    )
    p = tmp_path / "enum.json"
    write_chunkdef(p, c)
    c2 = read_chunkdef(p)
    assert c2 == c


def test_write_chunkdef_preserves_exit_declaration_order(tmp_path: Path) -> None:
    c = ChunkDef(
        id="probe",
        title="Probe",
        prompt_fragment="probe",
        exits={
            "<hypothesis_filed>": ExitTarget(
                tag="<hypothesis_filed>",
                next_chunk_id="probe",
            ),
            "<readied_for_writeup>": ExitTarget(
                tag="<readied_for_writeup>",
                next_chunk_id="intel",
            ),
            "<investigation_done>": ExitTarget(
                tag="<investigation_done>",
                next_chunk_id="enum",
            ),
        },
    )
    p = tmp_path / "probe.json"

    write_chunkdef(p, c)
    data = json.loads(p.read_text())

    assert list(data["exits"]) == [
        "<hypothesis_filed>",
        "<readied_for_writeup>",
        "<investigation_done>",
    ]


def _make_minimal_graph() -> list[ChunkDef]:
    enum = ChunkDef(
        id="enum", title="Enumerate", prompt_fragment="x",
        exits={
            "<done>": ExitTarget(tag="<done>", next_chunk_id="probe",
                                  requires_caller="agent"),
        },
        is_entry=True,
    )
    probe = ChunkDef(
        id="probe", title="Probe", prompt_fragment="x",
        exits={
            "<done>": ExitTarget(tag="<done>", next_chunk_id="writeup",
                                  requires_caller="agent"),
            "<back>": ExitTarget(tag="<back>", next_chunk_id="enum",
                                  requires_caller="agent"),
        },
    )
    writeup = ChunkDef(
        id="writeup", title="Writeup", prompt_fragment="x",
        exits={
            "<finalize>": ExitTarget(tag="<finalize>", next_chunk_id="end",
                                      requires_caller="operator"),
            "<back>": ExitTarget(tag="<back>", next_chunk_id="probe",
                                  requires_caller="agent"),
        },
        can_emit_finding=True,
    )
    end = ChunkDef(id="end", title="End", prompt_fragment="x", is_terminal=True)
    return [enum, probe, writeup, end]


def test_lint_passes_on_valid_graph() -> None:
    failures = lint_graph(_make_minimal_graph())
    assert failures == [], f"unexpected failures: {failures}"


def test_lint_no_entry_fails() -> None:
    graph = _make_minimal_graph()
    # Strip is_entry from all
    graph = [
        c if not c.is_entry else ChunkDef(**{**c.__dict__, "is_entry": False})
        for c in graph
    ]
    failures = lint_graph(graph)
    assert any(f.rule == "ENTRY_CHUNK_REACHABLE" for f in failures)


def test_lint_unreachable_chunk() -> None:
    graph = _make_minimal_graph()
    orphan = ChunkDef(id="orphan", title="x", prompt_fragment="x", is_terminal=True)
    failures = lint_graph(graph + [orphan])
    assert any(f.rule == "ALL_CHUNKS_REACHABLE" for f in failures)


def test_lint_dead_end() -> None:
    chunk = ChunkDef(
        id="lonely", title="x", prompt_fragment="x",
        is_entry=True,
    )  # no exits, not terminal
    failures = lint_graph([chunk])
    assert any(f.rule == "NO_DEAD_ENDS" for f in failures)


def test_lint_exit_target_missing() -> None:
    enum = ChunkDef(
        id="enum", title="x", prompt_fragment="x",
        exits={"<done>": ExitTarget(tag="<done>", next_chunk_id="ghost")},
        is_entry=True,
    )
    failures = lint_graph([enum])
    assert any(f.rule == "EXIT_TARGETS_EXIST" for f in failures)


def test_lint_caller_invalid() -> None:
    enum = ChunkDef(
        id="enum", title="x", prompt_fragment="x",
        exits={"<done>": ExitTarget(
            tag="<done>", next_chunk_id="enum",
            requires_caller="random",  # invalid
        )},
        is_entry=True,
    )
    failures = lint_graph([enum])
    assert any(f.rule == "CALLER_TAGS_VALID" for f in failures)


def test_lint_no_mandatory_production_fails() -> None:
    enum = ChunkDef(
        id="enum", title="x", prompt_fragment="x",
        exits={"<go>": ExitTarget(tag="<go>", next_chunk_id="writeup",
                                   requires_caller="agent")},
        is_entry=True,
    )
    writeup = ChunkDef(
        id="writeup", title="x", prompt_fragment="x",
        exits={
            # All exits go to other writeup-class chunks
            "<a>": ExitTarget(tag="<a>", next_chunk_id="writeup_b",
                               requires_caller="agent"),
        },
        can_emit_finding=True,
    )
    writeup_b = ChunkDef(
        id="writeup_b", title="x", prompt_fragment="x",
        exits={
            "<a>": ExitTarget(tag="<a>", next_chunk_id="writeup",
                               requires_caller="agent"),
        },
        can_emit_finding=True,
    )
    failures = lint_graph([enum, writeup, writeup_b])
    assert any(f.rule == "NO_MANDATORY_PRODUCTION" for f in failures)


def test_probe_template_cannot_emit_finding() -> None:
    assert PROBE_TEMPLATE.can_emit_finding is False
    assert "finding.file" not in PROBE_TEMPLATE.tool_surface


# ---------- freshness ----------

def test_freshness_capture_in_current_activation(tmp_path: Path) -> None:
    binder = EvidenceBinder(tmp_path)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s",
                     activation_id="a-now")
    body = {"evidence_refs": [rec.id]}
    assert evidence_in_current_activation(
        body, binder=binder, audit_log_path=None, current_activation_id="a-now",
    ) is True
    assert evidence_in_current_activation(
        body, binder=binder, audit_log_path=None, current_activation_id="a-stale",
    ) is False


def test_freshness_via_audit_log(tmp_path: Path) -> None:
    binder = EvidenceBinder(tmp_path)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s",
                     activation_id="a-old")
    audit = tmp_path / "events.jsonl"
    # Record an evidence.get audit event in current activation referencing the ev_id
    entry = {
        "ts": time.time(),
        "kind": "channel.evidence.get",
        "body": {
            "channel": "evidence.get",
            "ctx": {"activation_id": "a-now"},
            "status": "ok",
            "body_hash": "sha256:x",
        },
        "prev_hash": "0" * 64,
    }
    audit.write_text(json.dumps(entry) + "\n")
    body = {"evidence_refs": [rec.id]}
    assert evidence_in_current_activation(
        body, binder=binder, audit_log_path=audit, current_activation_id="a-now",
    ) is True
    assert evidence_in_current_activation(
        body, binder=binder, audit_log_path=audit, current_activation_id="a-other",
    ) is False


def test_freshness_no_evidence_returns_false(tmp_path: Path) -> None:
    binder = EvidenceBinder(tmp_path)
    body = {"evidence_refs": []}
    assert evidence_in_current_activation(
        body, binder=binder, audit_log_path=None, current_activation_id="a",
    ) is False
