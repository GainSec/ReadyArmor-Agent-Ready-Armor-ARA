"""Normalize task-channel payloads at ARA runtime boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable


_FACT_SOURCES = {"agent_assertion", "crumb_emit", "operator_input", "import"}
_LEAD_ORIGINS = {
    "agent_hypothesis",
    "anomaly_promoted",
    "operator_input",
    "operator_seeded_agent_enriched",
}


def _note_append(existing: object, addition: str) -> str:
    text = str(existing or "").strip()
    return f"{text}; {addition}" if text else addition


def _clean_excerpt(value: object, *, limit: int = 120) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = " ".join(text.split())
    return text[:limit]


def drop_unresolved_evidence_refs(
    args: dict,
    evidence_exists: Callable[[str], bool],
) -> dict:
    """Drop stale agent-invented refs before normal evidence fallback runs."""
    body = dict(args)
    refs = body.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        return body
    kept: list[str] = []
    dropped: list[str] = []
    for ref in refs:
        ref_id = str(ref or "").strip()
        if not ref_id:
            continue
        try:
            exists = bool(evidence_exists(ref_id))
        except Exception:
            exists = False
        if exists:
            kept.append(ref_id)
        else:
            dropped.append(ref_id)
    if not dropped:
        return body
    if kept:
        body["evidence_refs"] = kept
    else:
        body.pop("evidence_refs", None)
    return body


def adapt_task_channel_args(
    name: str,
    args: dict,
    recent_evidence_refs: list[str],
) -> dict:
    """Normalize Hermes task-channel payloads before strict validation."""
    body = dict(args)
    recent = list(recent_evidence_refs[:3])
    if name == "fact.assert":
        if "asserted_by_crumb" not in body:
            body["asserted_by_crumb"] = "mcp:fact.assert"
        if not body.get("evidence_refs") and not body.get("depends_on") and recent:
            body["evidence_refs"] = recent
        source = str(body.get("source") or "agent_assertion")
        if source not in _FACT_SOURCES:
            body["note"] = _note_append(body.get("note"), f"original source={source}")
            body["source"] = "agent_assertion"
        allowed = {
            "predicate", "value", "asserted_by_crumb", "evidence_refs",
            "depends_on", "confidence", "source", "note", "prior_fact",
        }
        return {k: v for k, v in body.items() if k in allowed}
    if name == "lead.file":
        if not body.get("title"):
            for key in (
                "hypothesis", "summary", "observation", "description",
                "note", "reasoning", "proposed_test", "next_action",
            ):
                title = _clean_excerpt(
                    body.get(key),
                    limit=80 if key == "reasoning" else 120,
                )
                if title:
                    body["title"] = title
                    break
        if not body.get("reasoning"):
            pieces = []
            for key in ("hypothesis", "target", "phase", "next_action"):
                if body.get(key):
                    pieces.append(f"{key}: {body[key]}")
            if pieces:
                body["reasoning"] = "; ".join(pieces)
            elif body.get("title"):
                body["reasoning"] = str(body["title"])
        if not body.get("proposed_test") and body.get("next_action"):
            body["proposed_test"] = str(body["next_action"])
        if not body.get("affected_assets") and body.get("target"):
            body["affected_assets"] = [str(body["target"])]
        if not body.get("evidence_refs") and not body.get("fact_refs") and recent:
            body["evidence_refs"] = recent
        origin = str(body.get("origin") or "agent_hypothesis")
        if origin not in _LEAD_ORIGINS:
            body["notes"] = _note_append(body.get("notes"), f"original origin={origin}")
            body["origin"] = "agent_hypothesis"
        extras = [
            f"{k}={body[k]}"
            for k in ("confidence", "phase")
            if k in body
        ]
        if extras:
            body["notes"] = _note_append(body.get("notes"), "original " + ", ".join(extras))
        allowed = {
            "title", "reasoning", "evidence_refs", "fact_refs",
            "proposed_test", "not_mechanically_testable", "origin",
            "derived_from_crumb", "affected_assets", "value_tier",
            "attack_surface_tier", "scope_weight", "priority_agent_proposed",
            "negation_predicates", "enrichments", "notes",
        }
        return {k: v for k, v in body.items() if k in allowed}
    return body


# Backwards-compatible private alias used by older tests/importers.
_adapt_task_channel_args = adapt_task_channel_args
