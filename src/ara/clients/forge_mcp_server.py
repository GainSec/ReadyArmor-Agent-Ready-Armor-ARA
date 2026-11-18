"""ARA forge-channel MCP server.

Bridges Hermes (and any other MCP client) to ARA's forge-v1 channel
surface so a Python-runtime agent can call ``mats.search``,
``extract.proc``, ``fact.assert`` etc. as native tools.

Why this exists. ARA's forge channels live in
``runtime/channels/registry.py`` and are exposed to OpenClaw (Node) via
``src/guard/hooks/forge_channels.mjs`` injecting ``globalThis.ara
.channel.call(...)``. Hermes is Python and would never see that bridge.
This MCP server gives Hermes an alternative path: stdio MCP protocol →
``ForgeChannelClient`` → supervisor's ``_on_channel_call``.

Operator wires it into ``~/.hermes/config.yaml``::

    mcp_servers:
      ara-forge:
        command: python3.9
        args: ["-m", "ara.cli", "forge-mcp"]

The server reads ``ARA_SOCKET`` + ``ARA_SESSION_TOKEN`` from the env
that ``ara launch --contain`` sets, so it only works inside a contained
session — exactly what we want.

Protocol notes. Speaks the minimum MCP subset Hermes needs:
``initialize`` → ``initialized`` → ``tools/list`` → ``tools/call``.
JSON-RPC 2.0 over stdin/stdout, line-delimited (one JSON object per
line). Errors propagate as MCP error responses with the channel's
``error_kind`` in ``data.error_kind``.

Tool names are the channel names verbatim (e.g. ``fact.assert``).
Hermes treats ``.`` as part of the tool name, no normalization needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ara import debug_log
from ara.task_channel_adapt import adapt_task_channel_args as _adapt_task_channel_args
from .forge_channel_sdk import ChannelCallError, ForgeChannelClient


PROTOCOL_VERSION = "2024-11-05"


# Shared inputSchema for every extract.* channel. The supervisor's
# Channel.validate is authoritative, but the MCP client sends
# whatever the LLM constructs — and an LLM with a permissive schema
# will routinely send ``{}`` and trip the channel's "missing required
# field" rejection. So we declare the contract explicitly here.
#
# ``manifest_element_id`` ties the extraction to evidence (validated
# against the manifest); ``body`` is the structured payload describing
# what was extracted. ``forge_id`` and ``citation`` are optional.
_EXTRACT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "manifest_element_id": {
            "type": "string",
            "description": (
                "ID of the manifest element this extraction is grounded "
                "in. Get it from mats.search results or a prior mats.get."
            ),
        },
        "body": {
            "type": "object",
            "description": (
                "The actual extracted content as a structured dict. "
                "Shape depends on the alphabet — e.g. extract.proc "
                "expects {name, steps, ...}; extract.conc expects "
                "{name, rubric, ...}; extract.store expects {name, "
                "categories, ...}; extract.resource expects resource classes "
                "such as reference, wordlist, script, package, vendor_doc, "
                "or vuln_db with name, purpose/rationale, hints, optional "
                "locator, allowed_uses, and requires_approval_for. If the "
                "source names a resource without a verified URL, emit the "
                "intent without a locator so it remains needs_resolution. "
                "Must carry real content drawn from the element's text, "
                "not summaries."
            ),
            "additionalProperties": True,
        },
        "forge_id": {
            "type": "string",
            "description": (
                "Forge session id. Optional — auto-detects the sole "
                "EXHAUST session when not given."
            ),
        },
        "citation": {
            "type": "object",
            "description": (
                "Optional citation metadata. ``manifest_element_id`` "
                "and ``source: 'agent'`` are auto-populated."
            ),
            "additionalProperties": True,
        },
    },
    "required": ["manifest_element_id", "body"],
    "additionalProperties": False,
}


# Forge-v1 channels exposed to MCP clients. Schemas describe what
# each channel actually requires; the supervisor still validates
# authoritatively on the receiving side.
_TOOLS: list[dict] = [
    {
        "name": "mats.search",
        "description": (
            "Search the forge session's deposited materials (mats) by "
            "keyword. Returns ranked matches with manifest_element_ids "
            "you can fetch via mats.get. Use to discover content before "
            "asserting facts or emitting extractions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "forge_id": {
                    "type": "string",
                    "description": (
                        "Forge session id. Include this whenever the "
                        "operator prompt names a forge id; it disambiguates "
                        "when multiple forge sessions are in EXHAUST."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": True,
        },
    },
    {
        "name": "mats.get",
        "description": (
            "Fetch the full text + metadata of a specific deposited "
            "material by manifest_element_id. Use after mats.search to "
            "ground extractions in real source bytes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "manifest_element_id": {"type": "string"},
                "forge_id": {
                    "type": "string",
                    "description": (
                        "Forge session id. Include this whenever the "
                        "operator prompt names a forge id; it disambiguates "
                        "when multiple forge sessions are in EXHAUST."
                    ),
                },
            },
            "required": ["manifest_element_id"],
            "additionalProperties": True,
        },
    },
    {
        "name": "extract.proc",
        "description": (
            "Emit a procedure extraction (PROC alphabet entry). Body "
            "must carry the structured procedure content — typically a "
            "named, ordered step pattern with requires/action/emits."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.conc",
        "description": (
            "Emit a conceptual shape / template (CONC alphabet) — "
            "rubrics, fix libraries, severity matrices, anything "
            "structural drawn from a manifest element."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.fact_shape",
        "description": (
            "Register a fact-shape (FACT alphabet schema): the "
            "structured contract for predicates that subsequent "
            "fact.assert calls must satisfy at engagement time."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.store",
        "description": (
            "Emit an internal store entry (FACT alphabet). Body "
            "describes where a fact lives inside the weapon's knowledge "
            "model: predicates, evidence stores, endpoints, file paths, "
            "or named internal collections."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.resource",
        "description": (
            "Emit a forge-curated runtime resource for the weapon. Use "
            "for references or assets the task may need later, separate "
            "from target scope: reference, wordlist, script, package, "
            "vendor_doc, vuln_db. Body should include id, class, name, "
            "purpose/rationale, hints, allowed_uses, "
            "requires_approval_for, and locator only when the URL is "
            "grounded by the mats. Missing or failed locators remain "
            "visible for operator resolution and do not grant access."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.line",
        "description": (
            "Emit a scope-line (META alphabet): explicit in-scope / "
            "out-of-scope boundaries the manifest implies."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.rhema_rule",
        "description": (
            "Emit a rhema (prompt-mediation) rule extracted from "
            "manifest content. Gets composed into the weapon's "
            "policy/rhema.md at synthesize time. Body must include "
            "action (deny|approval|rewrite|shadow_rewrite), layer "
            "(reply|ask|url|...), and pattern (string or 'regex:...'). "
            "rationale is strongly encouraged so operators can audit."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.bulwark_rule",
        "description": (
            "Emit a bulwark policy rule. Composed into "
            "policy/bulwark.md under '## armor: weapon' so it layers "
            "on top of standing rules at equip time. Body must include "
            "action (deny|approval|allow), layer (fs|exec|net|url|...), "
            "and match (path glob, command name, host:port, etc.). "
            "rationale + scope optional."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "extract.walls_path",
        "description": (
            "Emit a WALLS entry (resource-centric deny rule). Composed "
            "into policy/WALLS.md grouped by kind. Body must include "
            "kind (deny_path|deny_command|approval_path) and value "
            "(the path glob or command name). Use this for paths the "
            "manifest declares sensitive (config files, credential "
            "stores) or commands it forbids agents from running."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "exhaust.status",
        "description": (
            "Declare at the END of a turn whether you fully derived this "
            "turn's elements. Body: {batch_complete: true|false, note?}. "
            "true = done, the driver advances; false = more to derive, the "
            "driver gives you another cycle on the same elements."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "forge_id": {"type": "string"},
                "batch_complete": {"type": "boolean"},
                "note": {"type": "string"},
            },
            "required": ["batch_complete"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose.alphabet",
        "description": (
            "Declare an alphabet for the new pack. Body: {name, shape, "
            "description, ...shape-native fields}. Shapes:\n"
            "- categorical/template_set: fields:[{name,type}] + entry rows "
            "via extract.conc.\n"
            "- vector (a set of SCORED dimensions, e.g. a CVSS metric "
            "vector): REQUIRES axes:[{name, min, max, description}]. Ships "
            "axes, NOT entry rows.\n"
            "- matrix (a value looked up by >=1 dimension, e.g. metric x "
            "value -> weight): REQUIRES axes:[dimension_name, ...] AND "
            "cell_fields:[{name,type}] describing each cell; emit the cells "
            "as extract.conc rows carrying coords + cell."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "propose.binding",
        "description": (
            "Bind a finding-record field to an alphabet. Body: "
            "{field, rule:'FIELD_IN_ALPHABET', alphabet}."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "propose.chunk",
        "description": (
            "Propose an investigation chunk template for the pack "
            "(serialised ChunkDef). Usually left to the offsec baseline."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "propose.detection",
        "description": (
            "Propose manifest report-section detection keywords. Body: "
            "{report_section_keywords:[...]}."
        ),
        "inputSchema": _EXTRACT_INPUT_SCHEMA,
    },
    {
        "name": "reconcile.merge",
        "description": (
            "Collapse semantic-duplicate alphabets into one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "targets": {"type": "array", "items": {"type": "string"},
                            "description": "alphabet names to merge (>=2)"},
                "into": {"type": "string",
                         "description": "the surviving alphabet name"},
                "rationale": {"type": "string"},
                "forge_id": {"type": "string"},
            },
            "required": ["targets", "into", "forge_id"],
        },
    },
    {
        "name": "reconcile.split",
        "description": (
            "Split one alphabet into source-scoped alphabets when its content "
            "comes from genuinely different/incompatible source tables."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "the alphabet to split"},
                "into": {
                    "type": "array",
                    "description": "one entry per source scale",
                    "items": {
                        "type": "object",
                        "properties": {
                            "new_name": {"type": "string"},
                            "element_ids": {"type": "array",
                                            "items": {"type": "string"}},
                        },
                        "required": ["new_name", "element_ids"],
                    },
                },
                "rationale": {"type": "string"},
                "forge_id": {"type": "string"},
            },
            "required": ["name", "into", "forge_id"],
        },
    },
    {
        "name": "reconcile.resolve_cell",
        "description": (
            "Pick the authoritative value where one matrix coordinate has "
            "conflicting cells. The kept value must be one that actually "
            "appears in a source mat — do not invent a compromise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "alphabet": {"type": "string"},
                "coords": {"type": "object", "additionalProperties": True,
                           "description": "{axis: value, ...} the contested coord"},
                "keep_cell": {"type": "object", "additionalProperties": True,
                              "description": "{field: value, ...} the value to keep"},
                "rationale": {"type": "string"},
                "forge_id": {"type": "string"},
            },
            "required": ["alphabet", "coords", "keep_cell", "forge_id"],
        },
    },
    {
        "name": "reconcile.drop",
        "description": (
            "Last resort — drop a genuinely unusable alphabet (rationale "
            "required). Prefer merge/split/resolve_cell first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "rationale": {"type": "string"},
                "forge_id": {"type": "string"},
            },
            "required": ["name", "rationale", "forge_id"],
        },
    },
    {
        "name": "fact.assert",
        "description": (
            "Assert a single grounded fact with predicate + value, an "
            "asserted_by_crumb (the procedure crumb that produced it), "
            "and either evidence_refs (binder evidence ids) or depends_on "
            "(prior fact ids). Validated against the active fact_shape."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "predicate": {"type": "string"},
                "value": {},
                "asserted_by_crumb": {"type": "string"},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source": {"type": "string"},
                "note": {"type": "string"},
                "prior_fact": {"type": "string"},
            },
            "required": ["predicate", "value", "asserted_by_crumb"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lead.file",
        "description": (
            "File a lead — a hypothesis / probe to investigate further. "
            "Used during engagement, not forge authoring. Requires title "
            "and reasoning, plus evidence_refs or fact_refs unless marked "
            "not_mechanically_testable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 120},
                "reasoning": {"type": "string"},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "fact_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "proposed_test": {"type": "string"},
                "not_mechanically_testable": {"type": "boolean"},
                "origin": {"type": "string"},
                "derived_from_crumb": {"type": "string"},
                "affected_assets": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "value_tier": {"type": "number"},
                "attack_surface_tier": {"type": "number"},
                "scope_weight": {"type": "number"},
                "priority_agent_proposed": {"type": "string"},
                "negation_predicates": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "enrichments": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "notes": {"type": "string"},
            },
            "required": ["title", "reasoning"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finding.file",
        "description": (
            "File a finding — a CONFIRMED vulnerability ready for the client "
            "report. File this the moment you have confirmed an issue (don't "
            "wait for writeup). Cite the evidence that proves it and say how "
            "you confirmed it; the finding is mechanically re-checked against "
            "that evidence, so a claim its own evidence contradicts (e.g. an "
            "'unauthenticated /admin' page whose captured response is a login "
            "form) is rejected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                          "description": "short finding title (<=120 chars)"},
                "category": {"type": "string",
                             "description": "vulnerability category (a weapon "
                             "enum value, or a free word — coerced to 'other')"},
                "severity": {"type": "string",
                             "enum": ["info", "low", "medium", "high", "critical"],
                             "description": "severity word"},
                "description": {"type": "string",
                                "description": "what the issue is (put the impact "
                                "in the `impact` field, not here)"},
                "impact": {
                    "type": "string",
                    "description": "the concrete impact — what an attacker gains "
                    "in THIS environment. Shown in its own Impact field on the "
                    "report; do NOT bury it in description.",
                },
                "affected_assets": {
                    "type": "array", "items": {"type": "string"},
                    "description": "the exact affected URLs / endpoints / params / "
                    "hosts.",
                },
                "remediation": {
                    "type": "string",
                    "description": "how to fix it (the remediation). Shown in its "
                    "own Remediation field.",
                },
                "evidence_refs": {
                    "type": "array", "items": {"type": "string"},
                    "description": "REQUIRED: ids of captured evidence (from "
                    "evidence.search/get) that demonstrate the finding. The "
                    "verification check runs against these bytes.",
                },
                "confirmation_method": {
                    "type": "string",
                    "description": "how you confirmed it — name the concrete, "
                    "checkable identifiers present in the cited evidence (URL "
                    "path, HTTP status, leaked field). Markers are auto-derived "
                    "from this and must appear in the evidence.",
                },
                "verification": {
                    "type": "object",
                    "description": "optional explicit markers instead of prose: "
                    "{must_contain:[...], must_not_contain:[...]} strings the "
                    "cited evidence must / must not contain.",
                },
                "not_mechanically_testable": {
                    "type": "boolean",
                    "description": "set true ONLY if the finding genuinely can't "
                    "be shown from captured bytes; it is then flagged unverified.",
                },
                "lead_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "evidence_refs"],
            "additionalProperties": True,
        },
    },
    {
        "name": "evidence.search",
        "description": (
            "Search evidence captured in the active ARA session. Returns "
            "evidence ids, summaries, metadata, scores, and excerpts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source": {"type": "string"},
                "since": {"type": "number"},
                "k": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "evidence.get",
        "description": (
            "Fetch a bounded preview of captured evidence by evidence_id. "
            "Use returned ids as evidence_refs in fact.assert and lead.file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "kb": {"type": "integer", "minimum": 1, "maximum": 64},
            },
            "required": ["evidence_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "chunks.emit_tag",
        "description": (
            "Emit a weapon chunk exit tag to advance the active chunk. "
            "Valid exit tags for the current chunk are listed in the forge "
            "brief under Active chunk exits. Tags outside that list are "
            "rejected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"tag": {"type": "string"}},
            "required": ["tag"],
            "additionalProperties": True,
        },
    },
]


_TASK_TOOL_NAMES = {
    "fact.assert",
    "lead.file",
    "finding.file",
    "evidence.search",
    "evidence.get",
    "chunks.emit_tag",
}

# Pack-forge: the contained codex+pen agent sees ONLY these — the vocabulary
# proposal channels + mats read + the two policy channels (bulwark for a
# concrete destructive pattern the mats name, rhema for advisory guidance).
# It never sees the weapon extract.* knowledge set.
_PACK_TOOL_NAMES = {
    "mats.search",
    "mats.get",
    "exhaust.status",
    "propose.alphabet",
    "extract.conc",
    "propose.binding",
    "propose.chunk",
    "propose.detection",
    "extract.bulwark_rule",
    "extract.rhema_rule",
}

# The propose.* / reconcile.* channels are pack-only — never exposed to a
# weapon-forge (forge mode) agent. extract.conc is shared (weapon + pack).
_PACK_ONLY_TOOL_NAMES = {
    "propose.alphabet",
    "propose.binding",
    "propose.chunk",
    "propose.detection",
    "reconcile.merge",
    "reconcile.split",
    "reconcile.resolve_cell",
    "reconcile.drop",
}

# RECONCILE step: the agent EDITS existing vocabulary (resolve conflicts, merge
# dups, split incompatible sources) — it does not mine new vocabulary, so it
# sees only mats read + the reconcile.* decision channels + status.
_RECONCILE_TOOL_NAMES = {
    "mats.get",
    "reconcile.merge",
    "reconcile.split",
    "reconcile.resolve_cell",
    "reconcile.drop",
    "exhaust.status",
}

# WORKFLOW-DERIVE step: the agent derives the investigation chunk graph from
# methodology/testing-phase content in the mats (grounded) + its own knowledge
# of standard methodologies for gaps. It only reads mats and emits chunk nodes.
_WORKFLOW_TOOL_NAMES = {
    "mats.search",
    "mats.get",
    "propose.chunk",
    "exhaust.status",
}


def _mode_from_env(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    mode = str(env.get("ARA_FORGE_MCP_MODE") or "").strip().lower()
    if mode in {"task", "engagement"}:
        return "task"
    if mode == "pack":
        return "pack"
    if mode == "reconcile":
        return "reconcile"
    if mode == "workflow":
        return "workflow"
    if str(env.get("ARA_TASK_SUPERVISOR") or "") == "armory-hermes-loop":
        return "task"
    return "forge"


def _tools_for_mode(environ: dict[str, str] | None = None) -> list[dict]:
    mode = _mode_from_env(environ)
    if mode == "task":
        return [tool for tool in _TOOLS if tool["name"] in _TASK_TOOL_NAMES]
    if mode == "pack":
        return [tool for tool in _TOOLS if tool["name"] in _PACK_TOOL_NAMES]
    if mode == "reconcile":
        return [tool for tool in _TOOLS if tool["name"] in _RECONCILE_TOOL_NAMES]
    if mode == "workflow":
        return [tool for tool in _TOOLS if tool["name"] in _WORKFLOW_TOOL_NAMES]
    # forge (weapon) mode: full tool set minus the pack-only proposal channels
    return [tool for tool in _TOOLS if tool["name"] not in _PACK_ONLY_TOOL_NAMES]


def _fact_evidence_query(body: dict) -> str:
    """Build a conservative evidence.search query for an ungrounded fact."""
    pieces: list[str] = []
    for value in (body.get("value"), body.get("predicate")):
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, sort_keys=True)
        else:
            text = str(value)
        pieces.extend(re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,80}", text))
    cleaned: list[str] = []
    for piece in pieces:
        token = piece.strip("._/-")
        if len(token) < 2 or token in cleaned:
            continue
        cleaned.append(token)
        if len(cleaned) >= 8:
            break
    return " ".join(cleaned)


def _remember_evidence_ids(recent: list[str], result: dict, *, limit: int = 12) -> None:
    ids: list[str] = []
    if isinstance(result.get("record"), dict):
        ev_id = result["record"].get("id")
        if isinstance(ev_id, str) and ev_id:
            ids.append(ev_id)
    for row in result.get("results") or []:
        if not isinstance(row, dict):
            continue
        rec = row.get("record")
        if isinstance(rec, dict):
            ev_id = rec.get("id")
            if isinstance(ev_id, str) and ev_id:
                ids.append(ev_id)
    if not ids:
        return
    prepend = "record" in result
    for ev_id in ids:
        if ev_id in recent:
            recent.remove(ev_id)
        if prepend:
            recent.insert(0, ev_id)
        else:
            recent.append(ev_id)
    del recent[limit:]


def _latest_session_evidence_refs(*, limit: int = 3) -> list[str]:
    """Return newest evidence ids for the active contained session.

    This is a last-resort bridge for Hermes task runs: target subprocess
    events can be bound by the supervisor before the agent has explicitly
    searched or fetched evidence. We still attach real binder evidence, not
    a prompt-only assertion.
    """
    state_root = os.environ.get("ARA_STATE_ROOT")
    session_id = os.environ.get("ARA_SESSION_ID")
    if not state_root or not session_id:
        return []
    db_path = Path(state_root) / "sessions" / session_id / "evidence" / "index.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id FROM evidence ORDER BY captured_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        str(row[0])
        for row in rows
        if row and isinstance(row[0], str) and row[0]
    ]


class _Server:
    def __init__(self) -> None:
        self.initialized = False
        self._mode = _mode_from_env()
        self._recent_evidence_refs: list[str] = []
        sock_path = os.environ.get("ARA_SOCKET")
        token = os.environ.get("ARA_SESSION_TOKEN")
        if not sock_path:
            raise RuntimeError(
                "ARA_SOCKET not set — forge-mcp must run inside an "
                "`ara launch --contain` session."
            )
        self.client = ForgeChannelClient(Path(sock_path), session_token=token)

    async def serve(self) -> None:
        # Bind stdin/stdout via asyncio for line-by-line read/write.
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin,
        )
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._handle(msg)
        finally:
            await self.client.close()

    async def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            self._respond(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ara-forge", "version": "0.1.0"},
            })
            return
        if method == "notifications/initialized":
            self.initialized = True
            return  # no response on notifications
        if method == "tools/list":
            tools = _tools_for_mode({"ARA_FORGE_MCP_MODE": self._mode})
            debug_log.emit(
                "forge-mcp", "tools.list",
                state_root=os.environ.get("ARA_STATE_ROOT"),
                session_id=os.environ.get("ARA_SESSION_ID"),
                mode=self._mode,
                tools=[tool.get("name") for tool in tools],
            )
            self._respond(msg_id, {"tools": tools})
            return
        if method == "tools/call":
            await self._tool_call(msg_id, msg.get("params") or {})
            return
        if method is None and msg_id is not None:
            return  # unrecognized message, ignore
        # Unknown method — return MCP method-not-found per spec
        if msg_id is not None:
            self._error(msg_id, -32601, f"method not found: {method}")

    async def _tool_call(self, msg_id: Any, params: dict) -> None:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            self._error(msg_id, -32602, "tools/call: missing/invalid name")
            return
        mode = getattr(self, "_mode", "forge")
        available = {
            tool["name"]
            for tool in _tools_for_mode({"ARA_FORGE_MCP_MODE": mode})
        }
        if name not in available:
            debug_log.emit(
                "forge-mcp", "tool.reject_unavailable",
                state_root=os.environ.get("ARA_STATE_ROOT"),
                session_id=os.environ.get("ARA_SESSION_ID"),
                mode=mode,
                name=name,
                available=sorted(available),
            )
            self._error(
                msg_id,
                -32601,
                f"tool not available in {mode} MCP mode: {name}",
            )
            return
        if isinstance(args, dict):
            args = _adapt_task_channel_args(
                name, args, self._recent_evidence_refs,
            )
        debug_log.emit(
            "forge-mcp", "tool.call_start",
            state_root=os.environ.get("ARA_STATE_ROOT"),
            session_id=os.environ.get("ARA_SESSION_ID"),
            mode=mode,
            name=name,
            args=args,
        )
        if isinstance(args, dict):
            if (
                name == "fact.assert"
                and not args.get("evidence_refs")
                and not args.get("depends_on")
            ):
                query = _fact_evidence_query(args)
                if query:
                    try:
                        search_result = await self.client.call(
                            "evidence.search", {"query": query, "k": 5},
                            timeout=20.0,
                        )
                    except Exception as e:
                        search_result = None
                        debug_log.emit_exception(
                            "forge-mcp", "tool.fact_evidence_search_error", e,
                            state_root=os.environ.get("ARA_STATE_ROOT"),
                            session_id=os.environ.get("ARA_SESSION_ID"),
                            name=name,
                            query=query,
                        )
                        debug_log.emit(
                            "forge-mcp", "tool.fact_evidence_search_failed",
                            state_root=os.environ.get("ARA_STATE_ROOT"),
                            session_id=os.environ.get("ARA_SESSION_ID"),
                            name=name,
                            query=query,
                        )
                    if isinstance(search_result, dict):
                        _remember_evidence_ids(
                            self._recent_evidence_refs, search_result,
                        )
                        args = _adapt_task_channel_args(
                            name, args, self._recent_evidence_refs,
                        )
                if not args.get("evidence_refs") and not args.get("depends_on"):
                    fallback = _latest_session_evidence_refs(limit=3)
                    if fallback:
                        _remember_evidence_ids(
                            self._recent_evidence_refs,
                            {"results": [{"record": {"id": ev}} for ev in fallback]},
                        )
                        args = _adapt_task_channel_args(
                            name, args, self._recent_evidence_refs,
                        )
            if (
                name == "lead.file"
                and not args.get("evidence_refs")
                and not args.get("fact_refs")
            ):
                fallback = _latest_session_evidence_refs(limit=3)
                if fallback:
                    _remember_evidence_ids(
                        self._recent_evidence_refs,
                        {"results": [{"record": {"id": ev}} for ev in fallback]},
                    )
                    args = _adapt_task_channel_args(
                        name, args, self._recent_evidence_refs,
                    )
        try:
            result = await self.client.call(name, args, timeout=60.0)
        except ChannelCallError as e:
            debug_log.emit(
                "forge-mcp", "tool.channel_error",
                state_root=os.environ.get("ARA_STATE_ROOT"),
                session_id=os.environ.get("ARA_SESSION_ID"),
                mode=mode,
                name=name,
                args=args,
                error_kind=e.error_kind,
                channel=e.channel,
                error=e.message,
            )
            self._error(
                msg_id,
                -32000,
                f"{e.error_kind}: {e.message}",
                data={
                    "error_kind": e.error_kind,
                    "channel": e.channel,
                },
            )
            return
        except Exception as e:  # noqa: BLE001
            debug_log.emit_exception(
                "forge-mcp", "tool.error",
                e,
                state_root=os.environ.get("ARA_STATE_ROOT"),
                session_id=os.environ.get("ARA_SESSION_ID"),
                mode=mode,
                name=name,
                args=args,
            )
            self._error(msg_id, -32603, f"internal: {type(e).__name__}: {e}")
            return
        if name in ("evidence.search", "evidence.get") and isinstance(result, dict):
            _remember_evidence_ids(self._recent_evidence_refs, result)
        debug_log.emit(
            "forge-mcp", "tool.call_ok",
            state_root=os.environ.get("ARA_STATE_ROOT"),
            session_id=os.environ.get("ARA_SESSION_ID"),
            mode=mode,
            name=name,
            args=args,
            result=result,
        )
        self._respond(msg_id, {
            "content": [{
                "type": "text",
                "text": json.dumps(result, indent=2),
            }],
            "isError": False,
            "structuredContent": result,
        })

    # ---------- JSON-RPC framing ----------

    def _respond(self, msg_id: Any, result: Any) -> None:
        if msg_id is None:
            return
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id: Any, code: int, message: str,
               *, data: Any = None) -> None:
        if msg_id is None:
            return
        err: dict = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"jsonrpc": "2.0", "id": msg_id, "error": err})

    def _write(self, msg: dict) -> None:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def run() -> int:
    """Entry point for ``ara forge-mcp`` and direct ``python -m`` use."""
    try:
        asyncio.run(_Server().serve())
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[forge-mcp] fatal: {type(e).__name__}: {e}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
