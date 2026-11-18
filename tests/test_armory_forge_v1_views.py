"""Tests for armory.forge_v1_views — engagement / weapons / forge pages."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ara import weapons as weapons_mod
from ara.armory.forge_v1_views import (
    handle_equip,
    handle_unequip,
    render_engagement,
    render_forge,
    render_forge_detail,
    render_weapon_detail,
    render_weapons,
)


def _seed_session(state_root: Path, sid: str = "s") -> Path:
    sd = state_root / "sessions" / sid
    sd.mkdir(parents=True)
    return sd


def _seed_jsonl(p: Path, *rows: dict) -> None:
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _seal_weapon(state_root: Path, wid: str = "w-test", *,
                  with_chunks: bool = False,
                  with_alphabets: bool = False,
                  with_policy: bool = False,
                  with_resources: bool = False) -> Path:
    wdir = weapons_mod.weapon_dir(state_root, wid)
    wdir.mkdir(parents=True)
    weapon_json = {"weapon_target_id": wid}
    if with_resources:
        weapon_json["resources"] = [
            {
                "id": "res_github_wordlists",
                "class": "wordlist",
                "name": "SecLists wordlists",
                "locator": "https://github.com/danielmiessler/SecLists",
                "status": "resolved",
                "resolution": {"source": "mat_locator"},
                "allowed_uses": ["reference", "fetch"],
                "requires_approval_for": ["execute"],
                "rationale": "Public wordlists are useful for web app recon.",
            },
            {
                "id": "res_payload_hints",
                "class": "wordlist",
                "name": "Payload hints",
                "status": "needs_resolution",
                "hints": ["Payload repo", "operator supplied URL"],
                "allowed_uses": ["reference", "fetch"],
                "requires_approval_for": ["execute"],
            }
        ]
    (wdir / "weapon.json").write_text(json.dumps(weapon_json))
    (wdir / "seal.json").write_text(json.dumps({
        "weapon_id": wid, "weapon_target_id": wid, "weapon_hash": "0" * 64,
        "operator_id": "op", "sealed_at": 1.0, "warnings": [],
    }))
    if with_chunks:
        cdir = wdir / "chunks"
        cdir.mkdir()
        (cdir / "enum.json").write_text(json.dumps({
            "id": "enum", "title": "Enum", "prompt_fragment": "x",
            "tool_surface": ["fact.assert"],
            "exits": {"<go>": {"tag": "<go>", "next_chunk_id": "probe",
                                "requires_caller": "agent"}},
            "is_entry": True, "is_terminal": False,
            "can_emit_finding": False,
        }))
        (cdir / "probe.json").write_text(json.dumps({
            "id": "probe", "title": "Probe", "prompt_fragment": "x",
            "tool_surface": ["lead.file"], "exits": {},
            "is_entry": False, "is_terminal": True,
            "can_emit_finding": False,
        }))
    if with_alphabets:
        adir = wdir / "alphabets"
        adir.mkdir()
        (adir / "category.json").write_text(json.dumps({
            "shape": "categorical",
            "entries": [
                {"id": "xss.reflected", "label": "Reflected XSS"},
                {"id": "sqli", "label": "SQL injection"},
            ],
        }))
        (adir / "severity_rubric.json").write_text(json.dumps({
            "shape": "matrix", "axes": ["data_class", "access"],
            "cells": [{"coords": {"data_class": "PII", "access": "read"},
                       "score": 6.0, "bucket": "medium"}],
        }))
        (adir / "fix_library.json").write_text(json.dumps({
            "shape": "template_set",
            "templates": [{"id": "fix.x", "title": "Encode",
                            "applicable_categories": ["xss.reflected"]}],
        }))
        (adir / "impact.json").write_text(json.dumps({
            "shape": "vector",
            "axes": [
                {"name": "confidentiality", "min": 0, "max": 3},
                {"name": "integrity", "min": 0, "max": 3},
                {"name": "availability", "min": 0, "max": 3},
            ],
        }))
    if with_policy:
        pdir = wdir / "policy"
        pdir.mkdir()
        (pdir / "WALLS.md").write_text("## deny_path\n~/.aws/**\n")
        (pdir / "bulwark.md").write_text("# bulwark\n")
    (wdir / "channel_catalog.json").write_text(json.dumps({
        "weapon_id": wid, "channels": [
            {"name": "fact.assert", "sink_subsystem": "facts.jsonl",
             "callers": ["agent", "system"]},
        ], "edges": [],
    }))
    return wdir


# ---------- /engagement ----------

def test_render_engagement_no_weapon(tmp_path: Path) -> None:
    html = render_engagement(tmp_path, "tok")
    assert "No weapon equipped" in html
    assert "page-engagement" in html
    assert "Start task" in html
    assert "Stop task" in html
    assert "Direct chat" not in html
    assert "Backup Hermes chat" not in html


def test_render_engagement_with_weapon_no_session(tmp_path: Path) -> None:
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    html = render_engagement(tmp_path, "tok")
    assert "wstg-v1" in html
    assert "No active session yet" in html
    assert "Worker command" in html
    assert "hermes" in html


def test_render_engagement_ignores_empty_non_task_live_session(tmp_path: Path) -> None:
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    gateway_dir = tmp_path / "sessions" / "hermes-gateway"
    gateway_dir.mkdir(parents=True)
    (gateway_dir / "session.json").write_text(json.dumps({"pid": os.getpid()}))

    html = render_engagement(tmp_path, "tok")

    assert "No active session yet" in html
    assert '<span class="forge-phase-pill">standby</span>' in html
    assert "Active session" not in html


def test_render_engagement_full_state(tmp_path: Path) -> None:
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "chunk_history.jsonl",
        {"ts": 1.0, "kind": "initialize", "to_chunk": "enum",
         "activation_id": "a1"},
        {"ts": 2.0, "kind": "swap", "from_chunk": "enum", "to_chunk": "probe",
         "tag": "<go>", "caller": "agent", "activation_id": "a2"},
    )
    _seed_jsonl(
        sd / "activations.jsonl",
        {"ts": 1.0, "activation_id": "a1", "reason": "initial",
         "chunk_id": "enum"},
        {"ts": 2.0, "activation_id": "a2", "reason": "chunk_swap",
         "chunk_id": "probe"},
    )
    (sd / "activation.txt").write_text("a2")
    _seed_jsonl(sd / "facts.jsonl", {"id": "f:1"})
    _seed_jsonl(sd / "leads.jsonl",
                 {"id": "L:1", "status": "open"})
    _seed_jsonl(sd / "findings.jsonl",
                 {"id": "F:1", "status": "signed_off",
                  "severity_name": "high"})
    _seed_jsonl(sd / "anomalies.jsonl",
                 {"id": "A:1", "status": "pending"})
    _seed_jsonl(
        sd / "events.jsonl",
        {"ts": 1, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok"}},
        {"ts": 2, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "rejected"}},
    )
    # evidence index
    db_dir = sd / "evidence"
    db_dir.mkdir()
    conn = sqlite3.connect(db_dir / "index.db")
    try:
        conn.execute("CREATE TABLE evidence (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO evidence (id) VALUES ('e:abc')")
        conn.commit()
    finally:
        conn.close()

    html = render_engagement(tmp_path, "tok")
    assert "wstg-v1" in html
    assert "probe" in html  # active chunk
    assert "Activations" in html
    assert "fact.assert" in html
    # Counts cards
    assert ">findings<" in html or "findings" in html
    assert "1" in html  # at least one count


# ---------- /weapons ----------

def test_render_weapons_empty(tmp_path: Path) -> None:
    html = render_weapons(tmp_path, "tok")
    assert "None yet" in html


def test_render_weapons_lists_sealed(tmp_path: Path) -> None:
    _seal_weapon(tmp_path, "w-a")
    _seal_weapon(tmp_path, "w-b")
    weapons_mod.set_equipped_weapon(tmp_path, "w-a")
    html = render_weapons(tmp_path, "tok")
    assert "w-a" in html and "w-b" in html
    assert "equipped" in html
    # equip / unequip controls render
    assert "/weapons/unequip?t=tok" in html


def test_render_weapon_detail_full(tmp_path: Path) -> None:
    _seal_weapon(tmp_path, "w-full",
                  with_chunks=True, with_alphabets=True, with_policy=True,
                  with_resources=True)
    html = render_weapon_detail(tmp_path, "w-full", "tok")
    assert "/weapons/equip?t=tok" in html
    # Header + back link
    assert "w-full" in html
    # Alphabets section
    assert "category" in html
    assert "xss.reflected" in html
    assert "severity_rubric" in html
    assert "fix_library" in html
    assert "impact" in html
    # Chunks
    assert "enum" in html
    assert "probe" in html
    # Channel catalog
    assert "fact.assert" in html
    # Policy
    assert "WALLS.md" in html
    assert "~/.aws" in html
    # Forge-curated resources
    assert "Weapon resources" in html
    assert "SecLists wordlists" in html
    assert "https://github.com/danielmiessler/SecLists" in html
    assert "resolved" in html
    assert "reference" in html
    assert "execute" in html
    assert "Payload hints" in html
    assert "needs URL" in html
    assert "operator URL needed" in html
    assert "/weapons/w-full/resources/res_payload_hints/request-url?t=tok" in html
    assert "Request URL approval" in html
    # Seal
    assert "weapon_hash" in html


def test_render_weapon_detail_accepts_iso_sealed_at(tmp_path: Path) -> None:
    wdir = _seal_weapon(tmp_path, "w-seed")
    seal_path = wdir / "seal.json"
    seal = json.loads(seal_path.read_text())
    seal["sealed_at"] = "2026-04-26T00:00:00Z"
    seal_path.write_text(json.dumps(seal))

    html = render_weapon_detail(tmp_path, "w-seed", "tok")

    assert "2026-04-26T00:00:00Z" in html
    assert 'class="ara-pixel-art ara-pixel-art-weapon"' in html
    assert "weapon_hash" in html


def test_render_weapon_detail_unknown(tmp_path: Path) -> None:
    html = render_weapon_detail(tmp_path, "ghost", "tok")
    assert "not found" in html.lower()


# ---------- /forge ----------

def _seed_forge_session(state_root: Path, fid: str,
                         phase: str = "synthesize") -> Path:
    fdir = state_root / "forge" / fid
    fdir.mkdir(parents=True)
    (fdir / "session.json").write_text(json.dumps({
        "forge_id": fid, "weapon_target_id": "w-x",
        "phase": phase, "operator_id": "op",
        "last_advanced_at": 1.0,
    }))
    return fdir


def test_render_forge_empty(tmp_path: Path) -> None:
    html = render_forge(tmp_path, "tok")
    assert "No forge sessions yet" in html


def test_render_forge_lists_sessions(tmp_path: Path) -> None:
    _seed_forge_session(tmp_path, "f-a", phase="exhaust")
    _seed_forge_session(tmp_path, "f-b", phase="sealed")
    html = render_forge(tmp_path, "tok")
    assert "f-a" in html and "f-b" in html
    assert "exhaust" in html
    assert "sealed" in html


def test_render_forge_index_uses_workbench_cards(tmp_path: Path) -> None:
    fdir = _seed_forge_session(tmp_path, "f-a", phase="exhaust")
    mats = fdir / "mats"
    mats.mkdir()
    (mats / "scope.md").write_text("scope")
    _seed_jsonl(
        fdir / "manifest.jsonl",
        {"id": "elem_a", "kind": "report_section"},
        {"id": "elem_b", "kind": "procedure"},
    )
    _seed_jsonl(
        fdir / "extractions.jsonl",
        {"channel": "mats.search", "kind": "procedure"},
    )

    html = render_forge(tmp_path, "tok")

    assert 'class="forge-workbench"' in html
    assert 'class="forge-session-hand"' in html
    assert 'forge-session-card forge-mini-weapon' in html
    assert 'ara-pixel-art ara-pixel-art-weapon' in html
    assert "Phase flow" in html
    assert "Hermes exhaust" in html
    assert "1 mat" in html
    assert "2 manifest" in html
    assert "1 extraction" in html


def test_render_forge_detail_with_manifest_and_extractions(tmp_path: Path) -> None:
    fdir = _seed_forge_session(tmp_path, "f-1")
    _seed_jsonl(
        fdir / "manifest.jsonl",
        {"id": "elem_a", "kind": "report_section"},
        {"id": "elem_b", "kind": "code_block"},
        {"id": "elem_c", "kind": "report_section",
         "dropped_by_operator": True},
    )
    _seed_jsonl(
        fdir / "extractions.jsonl",
        {"channel": "extract.proc", "manifest_element_id": "elem_a",
         "body": {}, "extraction_id": "x_1",
         "citation": {"manifest_element_id": "elem_a"}},
        {"channel": "extract.conc", "manifest_element_id": "elem_a",
         "body": {"alphabet_name": "category", "entry": {"id": "xss.reflected"}},
         "extraction_id": "x_2",
         "citation": {"manifest_element_id": "elem_a", "auto": True}},
    )
    _seed_jsonl(
        fdir / "audit.jsonl",
        {"ts": 1, "kind": "session.created",
         "body": {}, "prev_hash": "0" * 64},
    )
    html = render_forge_detail(tmp_path, "f-1", "tok")
    assert "f-1" in html
    assert "Manifest" in html
    assert "report_section" in html
    assert "Extractions" in html
    assert "1 auto-extracted" in html or "auto-extracted" in html
    assert "Audit" in html


def test_render_forge_detail_does_not_offer_force_seal(tmp_path: Path) -> None:
    _seed_forge_session(tmp_path, "f-review", phase="review")

    html = render_forge_detail(tmp_path, "f-review", "tok")

    assert "Seal weapon" in html
    assert "force soft seal blockers" not in html


def test_render_forge_detail_shows_live_hearth_for_exhaust(tmp_path: Path) -> None:
    fdir = _seed_forge_session(tmp_path, "f-live", phase="exhaust")
    mats = fdir / "mats"
    mats.mkdir()
    (mats / "scope.md").write_text("scope")
    _seed_jsonl(
        fdir / "manifest.jsonl",
        {"id": "elem_a", "kind": "report_section"},
        {"id": "elem_b", "kind": "code_block"},
        {"id": "elem_c", "kind": "procedure"},
    )
    _seed_jsonl(
        fdir / "extractions.jsonl",
        {"channel": "extract.proc", "manifest_element_id": "elem_a",
         "body": {}, "extraction_id": "x_1"},
    )

    html = render_forge_detail(tmp_path, "f-live", "tok")

    assert "forge-hearth forge-hearth-live" in html
    assert 'aria-label="Forge hearth status"' in html
    assert "Hammering exhaust" in html
    assert "1 / 3" in html
    assert "33%" in html
    assert "forge-hearth-hammer" in html
    assert "forge-hearth-ore" in html


def test_render_forge_detail_shows_sealed_hearth_done(tmp_path: Path) -> None:
    fdir = _seed_forge_session(tmp_path, "f-done", phase="sealed")
    _seed_jsonl(
        fdir / "manifest.jsonl",
        {"id": "elem_a", "kind": "report_section"},
    )
    _seed_jsonl(
        fdir / "extractions.jsonl",
        {"channel": "extract.proc", "manifest_element_id": "elem_a",
         "body": {}, "extraction_id": "x_1"},
    )

    html = render_forge_detail(tmp_path, "f-done", "tok")

    assert "forge-hearth forge-hearth-done" in html
    assert "Weapon sealed" in html
    assert "1 / 1" in html
    assert "100%" in html
    assert "forge-hearth-finished-blade" in html


def test_render_forge_detail_unknown(tmp_path: Path) -> None:
    html = render_forge_detail(tmp_path, "ghost", "tok")
    assert "not found" in html.lower()


# ---------- equip / unequip handlers ----------

def test_handle_equip_unsealed_returns_error(tmp_path: Path) -> None:
    ok, msg = handle_equip(tmp_path, "ghost")
    assert not ok
    assert "not sealed" in msg


def test_handle_equip_writes_overlay_and_sets_marker(tmp_path: Path) -> None:
    _seal_weapon(tmp_path, "w-h", with_policy=True)
    ok, msg = handle_equip(tmp_path, "w-h")
    assert ok
    assert "WALLS.md" in msg
    assert weapons_mod.equipped_weapon_id(tmp_path) == "w-h"
    walls = (tmp_path / "WALLS.md").read_text()
    assert "BEGIN w-h" in walls


def test_handle_unequip_strips_overlay_and_clears_marker(tmp_path: Path) -> None:
    _seal_weapon(tmp_path, "w-h", with_policy=True)
    handle_equip(tmp_path, "w-h")
    ok, msg = handle_unequip(tmp_path)
    assert ok
    assert "WALLS.md" in msg
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_handle_unequip_when_nothing_equipped(tmp_path: Path) -> None:
    ok, msg = handle_unequip(tmp_path)
    assert not ok
    assert "No weapon equipped" in msg


# ---------- nav wiring ----------

def test_nav_includes_forge_v1_pages() -> None:
    from ara.armory.views import _NAV_GROUPS
    keys = {k for _, items in _NAV_GROUPS for k, *_ in items}
    assert "engagement" in keys
    assert "weapons" in keys
    assert "forge" in keys


# ---------- chunk graph viewer ----------

def test_render_chunk_graph_emits_cytoscape_payload() -> None:
    from ara.armory.forge_v1_views import _render_chunk_graph
    chunks = [
        {"id": "enum", "title": "Enum",
         "is_entry": True, "is_terminal": False,
         "exits": {"<go>": {"next_chunk_id": "probe",
                              "requires_caller": "agent"}}},
        {"id": "probe", "title": "Probe",
         "is_entry": False, "is_terminal": True,
         "can_emit_finding": True, "exits": {}},
    ]
    html = _render_chunk_graph(chunks)
    assert "ara-chunk-graph" in html
    assert "cytoscape" in html
    # node + edge data
    assert '"id": "enum"' in html
    assert '"id": "probe"' in html
    assert '"source": "enum"' in html
    assert '"target": "probe"' in html
    assert "caller-agent" in html


def test_render_weapon_detail_includes_chunk_graph(tmp_path: Path) -> None:
    _seal_weapon(tmp_path, "w-graph", with_chunks=True)
    html = render_weapon_detail(tmp_path, "w-graph", "tok")
    assert "ara-chunk-graph" in html
    assert "cytoscape" in html


# ---------- /engagement/findings ----------

from ara.armory.forge_v1_views import (
    handle_anomaly_dismiss,
    handle_anomaly_promote,
    handle_finding_rescind,
    handle_finding_sign_off,
    handle_lead_action,
    render_anomalies,
    render_finding_detail,
    render_findings,
    render_leads,
)


def test_render_findings_no_session(tmp_path: Path) -> None:
    html = render_findings(tmp_path, "tok")
    assert "No active session" in html


def test_render_findings_lists_and_filters(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "findings.jsonl",
        {"id": "F:1", "title": "High XSS", "category": "xss.reflected",
         "status": "draft", "severity_name": "high", "severity_score": 7.5,
         "created_at": 1.0},
        {"id": "F:2", "title": "Low info", "category": "info",
         "status": "signed_off", "severity_name": "low",
         "severity_score": 2.0, "created_at": 2.0},
    )
    html = render_findings(tmp_path, "tok")
    assert "F:1" in html and "F:2" in html
    # Severity-desc sort: F:1 (7.5) before F:2 (2.0)
    assert html.index("F:1") < html.index("F:2")
    # Filter pills present
    assert "draft" in html and "signed_off" in html

    filtered = render_findings(tmp_path, "tok", status_filter="draft")
    assert "F:1" in filtered
    assert "F:2" not in filtered


def test_render_findings_flash(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_findings(tmp_path, "tok", flash="Signed off F:1")
    assert "Signed off F:1" in html


def test_render_finding_detail_draft_shows_actions(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "findings.jsonl",
        {"id": "F:abc", "title": "T", "category": "xss",
         "status": "draft", "severity_name": "medium",
         "severity_score": 5.0, "created_at": 1.0,
         "severity_cell": ["PII", "read"], "impact": {},
         "affected_assets": ["app"], "evidence_refs": ["e:1"],
         "fact_refs": ["f:1"], "lead_refs": ["L:1"],
         "provenance_chain": ["c1"], "activation_id": "a1",
         "content_hash": "h" * 64},
    )
    html = render_finding_detail(tmp_path, "F:abc", "tok")
    assert "F:abc" in html
    assert "/engagement/findings/F:abc/sign_off" in html
    assert "/engagement/findings/F:abc/rescind" in html
    assert "PII" in html
    assert "h" * 16 in html  # content_hash echoed


def test_render_finding_detail_signed_off_only_rescind(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "findings.jsonl",
        {"id": "F:s", "title": "T", "category": "x",
         "status": "signed_off", "severity_name": "low",
         "severity_score": 2.0, "created_at": 1.0},
    )
    html = render_finding_detail(tmp_path, "F:s", "tok")
    assert "/engagement/findings/F:s/rescind" in html
    assert "/sign_off" not in html


def test_render_finding_detail_unknown(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_finding_detail(tmp_path, "F:ghost", "tok")
    assert "Finding not found" in html


# ---------- /engagement/leads ----------

def test_render_leads_no_session(tmp_path: Path) -> None:
    html = render_leads(tmp_path, "tok")
    assert "No active session" in html


def test_render_leads_lists_with_actions_for_open(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "leads.jsonl",
        {"id": "L:1", "title": "Open lead", "status": "open",
         "priority_computed": "high", "created_at": 1.0},
        {"id": "L:2", "title": "Done lead", "status": "promoted",
         "priority_computed": "low", "created_at": 2.0},
    )
    html = render_leads(tmp_path, "tok")
    assert "L:1" in html and "L:2" in html
    # Open lead has action forms; promoted does not
    assert "/engagement/leads/L:1/investigate" in html
    assert "/engagement/leads/L:1/promote" in html
    assert "/engagement/leads/L:1/dismiss" in html
    assert "/engagement/leads/L:2/investigate" not in html
    # Open sorts before promoted
    assert html.index("L:1") < html.index("L:2")


def test_render_leads_status_filter(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "leads.jsonl",
        {"id": "L:1", "title": "x", "status": "open",
         "priority_computed": "high", "created_at": 1.0},
        {"id": "L:2", "title": "y", "status": "promoted",
         "priority_computed": "low", "created_at": 2.0},
    )
    html = render_leads(tmp_path, "tok", status_filter="promoted")
    assert "L:2" in html
    assert "L:1" not in html


# ---------- /engagement/anomalies ----------

def test_render_anomalies_no_session(tmp_path: Path) -> None:
    html = render_anomalies(tmp_path, "tok")
    assert "No active session" in html


def test_render_anomalies_pending_and_history(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "anomalies.jsonl",
        {"id": "A:1", "status": "pending", "crumb_id": "c1",
         "evidence_id": "e:1", "divergence": {"raw_score": 3.42},
         "threshold_at_emission": 2.0, "detected_at": 1.0},
        {"id": "A:2", "status": "promoted_to_lead", "crumb_id": "c2",
         "evidence_id": "e:2", "divergence": {"raw_score": 5.0},
         "threshold_at_emission": 2.0, "detected_at": 2.0},
    )
    html = render_anomalies(tmp_path, "tok")
    assert "Pending" in html
    assert "History" in html
    assert "A:1" in html and "A:2" in html
    assert "/engagement/anomalies/A:1/promote" in html
    assert "/engagement/anomalies/A:1/dismiss" in html
    # History row has no action forms
    assert "/engagement/anomalies/A:2/promote" not in html
    assert "1 pending of 2 total" in html


def test_render_anomalies_empty(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_anomalies(tmp_path, "tok")
    assert "No anomalies recorded" in html


# ---------- mutation handler error paths ----------
# Channel-call success paths are exercised by test_findings / test_leads /
# test_anomaly_autofirer; here we cover the no-session and unknown-action
# branches that are armory-specific.

def test_handle_finding_sign_off_no_session(tmp_path: Path) -> None:
    ok, msg = handle_finding_sign_off(tmp_path, "F:x", "op")
    assert not ok
    assert "No active session" in msg


def test_handle_finding_rescind_no_session(tmp_path: Path) -> None:
    ok, msg = handle_finding_rescind(tmp_path, "F:x", "reason")
    assert not ok
    assert "No active session" in msg


def test_handle_lead_action_unknown(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    ok, msg = handle_lead_action(tmp_path, "L:1", "vaporize")
    assert not ok
    assert "Unknown lead action" in msg


def test_handle_lead_action_no_session(tmp_path: Path) -> None:
    ok, msg = handle_lead_action(tmp_path, "L:1", "investigate")
    assert not ok
    assert "No active session" in msg


def test_handle_anomaly_promote_no_session(tmp_path: Path) -> None:
    ok, msg = handle_anomaly_promote(tmp_path, "A:1", None)
    assert not ok
    assert "No active session" in msg


def test_handle_anomaly_dismiss_no_session(tmp_path: Path) -> None:
    ok, msg = handle_anomaly_dismiss(tmp_path, "A:1", None)
    assert not ok
    assert "No active session" in msg


# ---------- /forge/packs (built-in pack browser) ----------

from ara.armory.forge_v1_views import render_pack_detail, render_packs


def test_render_packs_lists_offsec(tmp_path: Path) -> None:
    html = render_packs(tmp_path, "tok")
    assert "offsec" in html
    assert "/forge/packs/offsec" in html


def test_render_pack_detail_offsec(tmp_path: Path) -> None:
    html = render_pack_detail(tmp_path, "offsec", "tok")
    assert "offsec" in html
    # Alphabets section + at least one of the offsec ones
    assert "Alphabets" in html
    assert "category" in html
    # Chunk templates + cytoscape graph
    assert "Chunk templates" in html
    assert "ara-chunk-graph" in html
    # Manifest detection + policy defaults present in offsec pack
    assert "Manifest detection" in html or "Policy defaults" in html


def test_render_pack_detail_unknown(tmp_path: Path) -> None:
    html = render_pack_detail(tmp_path, "ghost", "tok")
    assert "Pack not found" in html


def test_render_forge_links_to_packs(tmp_path: Path) -> None:
    html = render_forge(tmp_path, "tok")
    assert "/forge/packs" in html


def test_render_forge_includes_weapon_creation_preview(tmp_path: Path) -> None:
    html = render_forge(tmp_path, "tok")

    assert "/static/armor-card.css" in html
    assert 'class="ara-creation-preview ara-creation-preview-weapon"' in html
    assert 'class="ara-pixel-art ara-pixel-art-weapon"' in html
    assert "Create weapon preview" in html


def test_list_builtin_packs_includes_offsec() -> None:
    from ara.forge.alphabets.pack import list_builtin_packs
    assert "offsec" in list_builtin_packs()


# ---------- calibration card ----------

def _write_calibration_report(wdir: Path, *, blocked: bool) -> None:
    cases = [{
        "query": "sqli", "expected": ["c:sqli"],
        "returned": ["c:sqli", "c:xss"],
        "precision_at_5": 0.2 if not blocked else 0.0,
        "recall_at_10": 1.0 if not blocked else 0.0,
        "passed": not blocked,
    }]
    (wdir / "calibration_report.json").write_text(json.dumps({
        "weapon_id": wdir.name, "seal_id": "s",
        "embedder": "null", "cases": cases,
        "queries_total": 1,
        "queries_passed": 0 if blocked else 1,
        "queries_failed": 1 if blocked else 0,
        "mean_precision_at_5": 0.0 if blocked else 0.2,
        "mean_recall_at_10": 0.0 if blocked else 1.0,
        "seal_blocked": blocked,
        "blocking_reasons": (
            ["query 'sqli': precision@5=0.00 < required 0.20"]
            if blocked else []
        ),
        "ran_at": 1.0, "elapsed_ms": 0.0,
    }))


def test_render_weapon_detail_with_passing_calibration(tmp_path: Path) -> None:
    wdir = _seal_weapon(tmp_path, "w-cal-ok")
    _write_calibration_report(wdir, blocked=False)
    html = render_weapon_detail(tmp_path, "w-cal-ok", "tok")
    assert "Calibration" in html
    assert "passed" in html
    assert "1 / 1 queries" in html
    assert "0.200" in html
    assert "ara weapon calibrate w-cal-ok" in html


def test_render_weapon_detail_with_blocked_calibration(tmp_path: Path) -> None:
    wdir = _seal_weapon(tmp_path, "w-cal-bad")
    _write_calibration_report(wdir, blocked=True)
    html = render_weapon_detail(tmp_path, "w-cal-bad", "tok")
    assert "seal blocked" in html
    assert "Blocking reasons" in html
    assert "precision@5" in html
    assert "fail" in html


def test_render_weapon_detail_without_calibration(tmp_path: Path) -> None:
    _seal_weapon(tmp_path, "w-no-cal")
    html = render_weapon_detail(tmp_path, "w-no-cal", "tok")
    # Card omitted entirely when no report
    assert "Calibration" not in html


# ---------- operator-review queue ----------

from ara.armory.forge_v1_views import (
    handle_finding_queue_for_review,
    handle_finding_return_to_draft,
)


def test_render_findings_filter_pills_include_operator_review(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_findings(tmp_path, "tok")
    assert "operator_review" in html
    # As pill href
    assert "status=operator_review" in html


def test_render_findings_filter_by_operator_review(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "findings.jsonl",
        {"id": "F:1", "title": "draft", "category": "x",
         "status": "draft", "severity_name": "low",
         "severity_score": 2.0, "created_at": 1.0},
        {"id": "F:2", "title": "in review", "category": "x",
         "status": "operator_review", "severity_name": "high",
         "severity_score": 7.5, "created_at": 2.0},
    )
    html = render_findings(tmp_path, "tok", status_filter="operator_review")
    assert "F:2" in html
    assert "F:1" not in html


def test_render_finding_detail_draft_shows_queue_for_review(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "findings.jsonl",
        {"id": "F:d", "title": "x", "category": "x",
         "status": "draft", "severity_name": "low",
         "severity_score": 2.0, "created_at": 1.0},
    )
    html = render_finding_detail(tmp_path, "F:d", "tok")
    assert "/engagement/findings/F:d/queue_for_review" in html
    # return_to_draft only on operator_review status
    assert "/return_to_draft" not in html


def test_render_finding_detail_review_shows_signoff_and_return(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "findings.jsonl",
        {"id": "F:r", "title": "x", "category": "x",
         "status": "operator_review", "severity_name": "med",
         "severity_score": 5.0, "created_at": 1.0},
    )
    html = render_finding_detail(tmp_path, "F:r", "tok")
    assert "/engagement/findings/F:r/sign_off" in html
    assert "/engagement/findings/F:r/return_to_draft" in html
    assert "/engagement/findings/F:r/rescind" in html
    # No queue_for_review on already-queued finding
    assert "/queue_for_review" not in html


def test_handle_finding_queue_for_review_no_session(tmp_path: Path) -> None:
    ok, msg = handle_finding_queue_for_review(tmp_path, "F:x")
    assert not ok
    assert "No active session" in msg


def test_handle_finding_return_to_draft_no_session(tmp_path: Path) -> None:
    ok, msg = handle_finding_return_to_draft(tmp_path, "F:x")
    assert not ok
    assert "No active session" in msg


# ---------- channel-catalog graph ----------

def test_render_catalog_graph_emits_cytoscape_payload() -> None:
    from ara.armory.forge_v1_views import _render_catalog_graph
    chans = [
        {"name": "fact.assert", "sink_subsystem": "facts.jsonl",
         "callers": ["agent"]},
        {"name": "lead.confirm", "sink_subsystem": "leads.jsonl",
         "callers": ["system"]},
        {"name": "finding.sign_off", "sink_subsystem": "findings.jsonl",
         "callers": ["operator"]},
    ]
    edges = [
        {"from": "finding.sign_off", "to": "lead.confirm",
         "kind": "system_call"},
    ]
    html = _render_catalog_graph(chans, edges)
    assert "ara-catalog-graph" in html
    assert "cytoscape" in html
    assert '"id": "fact.assert"' in html
    assert '"source": "finding.sign_off"' in html
    assert '"target": "lead.confirm"' in html
    # Subsystem classes
    assert "subsys-fact" in html
    assert "subsys-lead" in html
    assert "subsys-finding" in html


def test_render_weapon_detail_includes_catalog_graph(tmp_path: Path) -> None:
    _seal_weapon(tmp_path, "w-cat")
    html = render_weapon_detail(tmp_path, "w-cat", "tok")
    # _seal_weapon writes a channel_catalog.json with one channel
    assert "Channel catalog" in html
    assert "ara-catalog-graph" in html


def test_render_catalog_graph_handles_no_edges() -> None:
    from ara.armory.forge_v1_views import _render_catalog_graph
    html = _render_catalog_graph(
        [{"name": "fact.assert", "sink_subsystem": "f", "callers": []}],
        [],
    )
    assert "ara-catalog-graph" in html
    assert "fact.assert" in html


def test_render_catalog_graph_skips_malformed_edges() -> None:
    from ara.armory.forge_v1_views import _render_catalog_graph
    html = _render_catalog_graph(
        [{"name": "x.y", "sink_subsystem": "x", "callers": []}],
        [{"from": None, "to": "x.y"}, {"to": "x.y"}],
    )
    # Both malformed — no edge serialized
    assert '"source"' not in html


# ---------- /engagement/leads/<id> drill-down ----------

from ara.armory.forge_v1_views import (
    render_anomaly_detail,
    render_lead_detail,
)


def test_render_lead_detail_no_session(tmp_path: Path) -> None:
    html = render_lead_detail(tmp_path, "L:1", "tok")
    assert "No active session" in html


def test_render_lead_detail_unknown(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_lead_detail(tmp_path, "L:ghost", "tok")
    assert "Lead not found" in html


def test_render_lead_detail_full(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "leads.jsonl",
        {"id": "L:abc", "title": "Probe /search for XSS",
         "reasoning": "user-controlled query reflected unencoded",
         "proposed_test": "curl '/search?q=<script>'",
         "status": "open", "origin": "agent",
         "activation_id": "act-1", "derived_from_crumb": "c:enum",
         "evidence_refs": ["e:1", "e:2"], "fact_refs": ["f:1"],
         "affected_assets": ["www.example.com"],
         "priority_computed": "high", "priority_agent_proposed": "medium",
         "priority_operator_override": None,
         "stability_s": 60.0,
         "created_at": 1.0, "last_reviewed_at": 2.0,
         "review_history": [
             {"at": 2.0, "kind": "agent_review",
              "by": "agent", "note": "still relevant"},
         ],
         "enrichments": [
             {"field": "asset", "status": "approved",
              "actor": "operator", "proposed_value": "auth-svc"},
         ],
        },
    )
    html = render_lead_detail(tmp_path, "L:abc", "tok")
    assert "L:abc" in html
    assert "Probe /search for XSS" in html
    assert "user-controlled query reflected" in html
    assert "curl" in html and "search?q=" in html
    assert "Reasoning" in html
    assert "Proposed test" in html
    assert "e:1, e:2" in html
    assert "f:1" in html
    assert "www.example.com" in html
    # action forms for open leads
    assert "/engagement/leads/L:abc/investigate" in html
    assert "/engagement/leads/L:abc/promote" in html
    assert "/engagement/leads/L:abc/dismiss" in html
    # review history surfaced
    assert "agent_review" in html
    assert "still relevant" in html
    # enrichments surfaced
    assert "Enrichments" in html
    assert "auth-svc" in html


def test_render_lead_detail_closed_no_actions(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "leads.jsonl",
        {"id": "L:c", "title": "x", "reasoning": "y",
         "status": "promoted", "origin": "agent",
         "activation_id": "a", "evidence_refs": [], "fact_refs": [],
         "priority_computed": "low",
         "stability_s": 0, "created_at": 1.0, "last_reviewed_at": 1.0,
         "review_history": [], "enrichments": [],
        },
    )
    html = render_lead_detail(tmp_path, "L:c", "tok")
    assert "L:c" in html
    assert "/engagement/leads/L:c/investigate" not in html


def test_render_leads_links_to_detail(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "leads.jsonl",
        {"id": "L:1", "title": "x", "status": "open",
         "priority_computed": "low", "created_at": 1.0},
    )
    html = render_leads(tmp_path, "tok")
    assert "/engagement/leads/L:1?t=tok" in html


# ---------- /engagement/anomalies/<id> drill-down ----------

def test_render_anomaly_detail_no_session(tmp_path: Path) -> None:
    html = render_anomaly_detail(tmp_path, "A:1", "tok")
    assert "No active session" in html


def test_render_anomaly_detail_unknown(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_anomaly_detail(tmp_path, "A:ghost", "tok")
    assert "Anomaly not found" in html


def test_render_anomaly_detail_pending_with_divergence(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "anomalies.jsonl",
        {"id": "A:1", "status": "pending",
         "crumb_id": "c:probe", "evidence_id": "e:42",
         "expected_shape_hash": "deadbeef",
         "divergence": {
             "raw_score": 4.2,
             "missing_required": ["status_code"],
             "pattern_mismatches": {"body": "expected json"},
             "unexpected_fields": ["x_debug"],
             "structural_violations": [],
             "line_matcher_failures": [],
             "section_matcher_failures": [],
         },
         "threshold_at_emission": 2.0,
         "activation_id": "act-1", "detected_at": 1.0},
    )
    html = render_anomaly_detail(tmp_path, "A:1", "tok")
    assert "A:1" in html
    assert "c:probe" in html and "e:42" in html
    assert "4.200" in html
    # divergence sub-sections
    assert "missing_required" in html
    assert "pattern_mismatches" in html
    assert "unexpected_fields" in html
    assert "x_debug" in html
    # action forms for pending
    assert "/engagement/anomalies/A:1/promote" in html
    assert "/engagement/anomalies/A:1/dismiss" in html


def test_render_anomaly_detail_closed_no_actions(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "anomalies.jsonl",
        {"id": "A:c", "status": "promoted_to_lead",
         "crumb_id": "c", "evidence_id": "e",
         "expected_shape_hash": "h",
         "divergence": {"raw_score": 1.0},
         "threshold_at_emission": 2.0,
         "activation_id": "a", "detected_at": 1.0,
         "promoted_lead_id": "L:42"},
    )
    html = render_anomaly_detail(tmp_path, "A:c", "tok")
    assert "L:42" in html
    assert "/engagement/anomalies/A:c/promote" not in html


def test_render_anomalies_links_to_detail(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "anomalies.jsonl",
        {"id": "A:1", "status": "pending",
         "crumb_id": "c", "evidence_id": "e",
         "divergence": {"raw_score": 1.0},
         "threshold_at_emission": 2.0,
         "detected_at": 1.0},
    )
    html = render_anomalies(tmp_path, "tok")
    assert "/engagement/anomalies/A:1?t=tok" in html


# ---------- forge audit chain viewer ----------

from ara.armory.forge_v1_views import render_evidence, render_forge_audit


def _seed_chained_audit(fdir: Path, *bodies: dict) -> None:
    """Append entries through ara.runtime.audit.append so prev_hash chains."""
    from ara.runtime import audit as _raudit
    _raudit.reset_registry_for_tests()
    p = fdir / "audit.jsonl"
    for i, b in enumerate(bodies):
        _raudit.append(p, b, kind=f"k_{i}")


def test_render_forge_audit_no_log(tmp_path: Path) -> None:
    fdir = tmp_path / "forge" / "f-x"
    fdir.mkdir(parents=True)
    html = render_forge_audit(tmp_path, "f-x", "tok")
    assert "No audit.jsonl" in html


def test_render_forge_audit_verified(tmp_path: Path) -> None:
    fdir = tmp_path / "forge" / "f-1"
    fdir.mkdir(parents=True)
    _seed_chained_audit(
        fdir,
        {"reason": "session.created"},
        {"reason": "deposit.completed"},
    )
    html = render_forge_audit(tmp_path, "f-1", "tok")
    assert "verified" in html
    assert "2 events" in html
    # rows
    assert "k_0" in html and "k_1" in html
    # body details rendered
    assert "session.created" in html


def test_render_forge_audit_broken_chain(tmp_path: Path) -> None:
    fdir = tmp_path / "forge" / "f-b"
    fdir.mkdir(parents=True)
    _seed_chained_audit(fdir, {"reason": "ok"})
    # Append a malformed line that breaks the chain
    (fdir / "audit.jsonl").open("a").write(
        '{"prev_hash": "deadbeef", "self_hash": "x", "kind": "bad", '
        '"body": {}}\n'
    )
    html = render_forge_audit(tmp_path, "f-b", "tok")
    assert "broken" in html
    assert "chain broke" in html


def test_forge_detail_links_to_audit(tmp_path: Path) -> None:
    """Audit card on forge detail should link through to /audit."""
    fdir = tmp_path / "forge" / "f-link"
    fdir.mkdir(parents=True)
    (fdir / "session.json").write_text(json.dumps({
        "forge_id": "f-link", "weapon_target_id": "w",
        "phase": "deposit", "operator_id": "op",
    }))
    _seed_chained_audit(fdir, {"reason": "x"})
    html = render_forge_detail(tmp_path, "f-link", "tok")
    assert "/forge/f-link/audit" in html


# ---------- /engagement/evidence ----------

def _seed_evidence_db(session_dir: Path, rows: list[dict]) -> None:
    db_dir = session_dir / "evidence"
    db_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_dir / "index.db")
    try:
        conn.execute(
            "CREATE TABLE evidence ("
            "id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
            "captured_at REAL NOT NULL, "
            "captured_during_activation_id TEXT, "
            "source TEXT NOT NULL, source_metadata_json TEXT, "
            "summary TEXT NOT NULL, size_bytes INTEGER NOT NULL, "
            "content_type TEXT, masked INTEGER DEFAULT 0)"
        )
        for r in rows:
            conn.execute(
                "INSERT INTO evidence VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r.get("sha256", "0" * 64),
                 r["captured_at"],
                 r.get("activation_id"),
                 r["source"], r.get("source_metadata_json", "{}"),
                 r["summary"], r.get("size_bytes", 100),
                 r.get("content_type"), int(r.get("masked", 0))),
            )
        conn.commit()
    finally:
        conn.close()


def test_render_evidence_no_session(tmp_path: Path) -> None:
    html = render_evidence(tmp_path, "tok")
    assert "No active session" in html


def test_render_evidence_no_db(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_evidence(tmp_path, "tok")
    assert "No evidence captured" in html


def test_render_evidence_lists_records(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_evidence_db(sd, [
        {"id": "e:1", "captured_at": 1.0, "activation_id": "a1",
         "source": "tool:curl", "summary": "GET /search response"},
        {"id": "e:2", "captured_at": 2.0, "activation_id": "a2",
         "source": "tool:nmap", "summary": "port scan output"},
    ])
    html = render_evidence(tmp_path, "tok")
    assert "e:1" in html and "e:2" in html
    assert "tool:curl" in html and "tool:nmap" in html
    assert "GET /search response" in html
    assert "Top sources" in html


def test_render_evidence_filter_by_source(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_evidence_db(sd, [
        {"id": "e:1", "captured_at": 1.0, "source": "tool:curl",
         "summary": "x", "activation_id": "a"},
        {"id": "e:2", "captured_at": 2.0, "source": "tool:nmap",
         "summary": "y", "activation_id": "a"},
    ])
    html = render_evidence(tmp_path, "tok", source_filter="tool:curl")
    assert "<code>e:1</code>" in html
    assert "<code>e:2</code>" not in html
    assert "clear filters" in html


def test_render_evidence_filter_by_activation(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_evidence_db(sd, [
        {"id": "e:1", "captured_at": 1.0, "source": "tool:curl",
         "summary": "x", "activation_id": "a1"},
        {"id": "e:2", "captured_at": 2.0, "source": "tool:curl",
         "summary": "y", "activation_id": "a2"},
    ])
    html = render_evidence(tmp_path, "tok", activation_filter="a2")
    assert "<code>e:2</code>" in html
    assert "<code>e:1</code>" not in html


# ---------- /engagement/activations ----------

from ara.armory.forge_v1_views import render_activations, render_events


def test_render_activations_no_session(tmp_path: Path) -> None:
    html = render_activations(tmp_path, "tok")
    assert "No active session" in html


def test_render_activations_empty(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_activations(tmp_path, "tok")
    assert "No activations recorded" in html


def test_render_activations_lists_with_durations(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "activations.jsonl",
        {"ts": 1.0, "activation_id": "a1", "reason": "initial",
         "chunk_id": "enum"},
        {"ts": 4.0, "activation_id": "a2", "reason": "chunk_swap",
         "chunk_id": "probe"},
    )
    _seed_jsonl(
        sd / "chunk_history.jsonl",
        {"ts": 1.0, "kind": "initialize", "to_chunk": "enum",
         "activation_id": "a1"},
        {"ts": 4.0, "kind": "swap", "from_chunk": "enum",
         "to_chunk": "probe", "activation_id": "a2"},
    )
    (sd / "activation.txt").write_text("a2")
    html = render_activations(tmp_path, "tok")
    assert "a1" in html and "a2" in html
    assert "current" in html  # badge on current activation
    assert "initial" in html
    assert "chunk_swap" in html
    # Filter chips
    assert "By reason" in html
    assert "By chunk" in html


def test_render_activations_filter_by_reason(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "activations.jsonl",
        {"ts": 1.0, "activation_id": "a1", "reason": "initial",
         "chunk_id": "enum"},
        {"ts": 2.0, "activation_id": "a2", "reason": "chunk_swap",
         "chunk_id": "probe"},
    )
    html = render_activations(tmp_path, "tok", reason_filter="initial")
    assert "<code>a1</code>" in html
    assert "<code>a2</code>" not in html
    assert "clear filters" in html


def test_render_activations_filter_by_chunk(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "activations.jsonl",
        {"ts": 1.0, "activation_id": "a1", "reason": "x",
         "chunk_id": "enum"},
        {"ts": 2.0, "activation_id": "a2", "reason": "x",
         "chunk_id": "probe"},
    )
    html = render_activations(tmp_path, "tok", chunk_filter="probe")
    assert "<code>a2</code>" in html
    assert "<code>a1</code>" not in html


def test_render_activations_no_filter_match(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "activations.jsonl",
        {"ts": 1.0, "activation_id": "a1", "reason": "x",
         "chunk_id": "enum"},
    )
    html = render_activations(tmp_path, "tok", reason_filter="ghost")
    assert "No activations match" in html


def test_engagement_activations_card_links_to_full_page(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_engagement
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    sd = _seed_session(tmp_path)
    _seed_jsonl(sd / "activations.jsonl",
                 {"ts": 1.0, "activation_id": "a1", "reason": "x",
                  "chunk_id": "enum"})
    (sd / "activation.txt").write_text("a1")
    html = render_engagement(tmp_path, "tok")
    assert "/engagement/activations?t=tok" in html


# ---------- /engagement/events ----------

def test_render_events_no_session(tmp_path: Path) -> None:
    html = render_events(tmp_path, "tok")
    assert "No active session" in html


def test_render_events_empty(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    html = render_events(tmp_path, "tok")
    assert "No events recorded" in html


def test_render_events_lists_channel_calls(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "events.jsonl",
        {"ts": 1.0, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok",
                  "caller": "agent", "activation_id": "a1"}},
        {"ts": 2.0, "kind": "channel.lead.file",
         "body": {"channel": "lead.file", "status": "rejected",
                  "caller": "agent", "activation_id": "a1",
                  "error": "missing reasoning"}},
        {"ts": 3.0, "kind": "audit.session.created",
         "body": {}},  # non-channel — should be filtered out
    )
    html = render_events(tmp_path, "tok")
    assert "fact.assert" in html
    assert "lead.file" in html
    assert "missing reasoning" in html
    assert "ok" in html and "rejected" in html
    # 2 channel events of 3 total log lines
    assert "2 channel events" in html
    assert "3 total" in html


def test_render_events_filter_by_channel(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "events.jsonl",
        {"ts": 1.0, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok",
                  "activation_id": "a"}},
        {"ts": 2.0, "kind": "channel.lead.file",
         "body": {"channel": "lead.file", "status": "ok",
                  "activation_id": "a"}},
    )
    html = render_events(tmp_path, "tok", channel_filter="lead.file")
    assert "lead.file" in html
    # No fact.assert row visible — check via td<code> match
    assert "<code>fact.assert</code>" not in html
    assert "clear filters" in html


def test_render_events_filter_by_status_and_activation(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "events.jsonl",
        {"ts": 1.0, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok",
                  "activation_id": "a1"}},
        {"ts": 2.0, "kind": "channel.lead.file",
         "body": {"channel": "lead.file", "status": "rejected",
                  "activation_id": "a2"}},
    )
    html = render_events(tmp_path, "tok", status_filter="rejected")
    assert "<code>lead.file</code>" in html
    assert "<code>fact.assert</code>" not in html

    html2 = render_events(tmp_path, "tok", activation_filter="a1")
    assert "<code>fact.assert</code>" in html2
    assert "<code>lead.file</code>" not in html2


def test_render_events_no_filter_match(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "events.jsonl",
        {"ts": 1.0, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok"}},
    )
    html = render_events(tmp_path, "tok", channel_filter="ghost")
    assert "No events match" in html


def test_engagement_events_card_links_to_full_page(tmp_path: Path) -> None:
    from ara.armory.forge_v1_views import render_engagement
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd / "events.jsonl",
        {"ts": 1.0, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok"}},
    )
    html = render_engagement(tmp_path, "tok")
    assert "/engagement/events?t=tok" in html


def test_render_evidence_no_matches(tmp_path: Path) -> None:
    sd = _seed_session(tmp_path)
    _seed_evidence_db(sd, [
        {"id": "e:1", "captured_at": 1.0, "source": "tool:curl",
         "summary": "x", "activation_id": "a"},
    ])
    html = render_evidence(tmp_path, "tok", source_filter="ghost")
    assert "No evidence matches" in html
