"""Mechanical checks for ARA task structured-channel completion.

The task runner can prompt an agent to emit structured ARA channels, but
completion status must be based on artifacts the supervisor recorded:
``events.jsonl`` channel rows and ``chunk_history.jsonl`` transitions.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import weapons as weapons_mod
from .forge.chunks.chunkdef import read_chunkdef


CANONICAL_TASK_CHANNELS = (
    "fact.assert",
    "lead.file",
    "finding.file",
    "evidence.search",
    "evidence.get",
    "chunks.emit_tag",
)

BAD_CHANNEL_ALIASES: dict[str, str] = {
    "mcp_ara_forge_fact_assert": "fact.assert",
    "mcp_ara_forge_lead_file": "lead.file",
    "mcp_ara_forge_finding_file": "finding.file",
    "mcp_ara_forge_evidence_search": "evidence.search",
    "mcp_ara_forge_evidence_get": "evidence.get",
    "mcp_ara_forge_chunks_emit_tag": "chunks.emit_tag",
    "mcp_ara_forge_chunk_emit_tag": "chunks.emit_tag",
}

KNOWLEDGE_CHANNELS = (
    "fact.assert",
    "lead.file",
    "finding.file",
    "evidence.search",
    "evidence.get",
)

TRANSITION_KINDS = {"swap", "jump", "rewind"}
CURRENT_CHUNK_KINDS = {"initialize", "swap", "jump", "rewind"}


@dataclass(frozen=True)
class ChannelCounts:
    total: Counter[str] = field(default_factory=Counter)
    accepted: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class ChannelGateStatus:
    required: bool
    status: str
    current_chunk: str | None
    allowed_channels: tuple[str, ...]
    required_channels: tuple[str, ...]
    missing_channels: tuple[str, ...]
    counts: ChannelCounts
    bad_aliases: tuple[tuple[str, str], ...] = ()
    transition_required: bool = False
    transition_present: bool = False
    reasons: tuple[str, ...] = ()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def session_channel_counts(session_dir: Path) -> ChannelCounts:
    total: Counter[str] = Counter()
    accepted: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    for row in _read_jsonl(Path(session_dir) / "events.jsonl"):
        kind = str(row.get("kind") or "")
        if not kind.startswith("channel."):
            continue
        body = row.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        name = str(body.get("channel") or kind[len("channel."):])
        total[name] += 1
        if body.get("status") == "rejected":
            rejected[name] += 1
        else:
            accepted[name] += 1
    return ChannelCounts(total=total, accepted=accepted, rejected=rejected)


def current_chunk_from_history(session_dir: Path) -> str | None:
    current: str | None = None
    for row in _read_jsonl(Path(session_dir) / "chunk_history.jsonl"):
        if row.get("kind") in CURRENT_CHUNK_KINDS:
            to_chunk = row.get("to_chunk")
            if to_chunk:
                current = str(to_chunk)
    return current


def has_chunk_transition(session_dir: Path) -> bool:
    for row in _read_jsonl(Path(session_dir) / "chunk_history.jsonl"):
        if row.get("kind") in TRANSITION_KINDS:
            return True
    return False


def scan_bad_channel_aliases(session_dir: Path) -> tuple[tuple[str, str], ...]:
    found: dict[str, str] = {}
    for name, canonical in BAD_CHANNEL_ALIASES.items():
        needle = name
        for artifact in ("transcript.jsonl", "events.jsonl", "session.json"):
            path = Path(session_dir) / artifact
            if not path.is_file():
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if needle in text:
                found[name] = canonical
                break
    return tuple(sorted(found.items()))


def _marker_contract(marker: dict[str, Any] | None) -> dict[str, Any]:
    if not marker:
        return {}
    contract = marker.get("channel_contract") or {}
    return contract if isinstance(contract, dict) else {}


def _weapon_chunk_channels(
    state_root: Path,
    marker: dict[str, Any] | None,
    current_chunk: str | None,
) -> tuple[str, ...]:
    if not current_chunk:
        return ()
    weapon_id = None
    if marker:
        value = marker.get("weapon_id")
        if value:
            weapon_id = str(value)
    if not weapon_id:
        weapon_id = weapons_mod.equipped_weapon_id(state_root)
    if not weapon_id:
        return ()
    chunk_path = weapons_mod.weapon_dir(state_root, weapon_id) / "chunks" / f"{current_chunk}.json"
    if not chunk_path.is_file():
        return ()
    try:
        chunk = read_chunkdef(chunk_path)
    except Exception:
        return ()
    return tuple(str(name) for name in chunk.tool_surface)


def _required_channels(
    *,
    contract: dict[str, Any],
    allowed_channels: tuple[str, ...],
) -> tuple[str, ...]:
    explicit = contract.get("required_channels")
    if isinstance(explicit, list) and explicit:
        return tuple(str(x) for x in explicit if str(x) in CANONICAL_TASK_CHANNELS)
    allowed_set = set(allowed_channels)
    required = [name for name in KNOWLEDGE_CHANNELS if name in allowed_set]
    if required:
        return tuple(required)
    return ("fact.assert", "lead.file", "finding.file", "evidence.search", "evidence.get")


def evaluate_task_channel_gate(
    state_root: Path,
    session_dir: Path | None,
    marker: dict[str, Any] | None,
    *,
    session_live: bool = False,
) -> ChannelGateStatus:
    required = bool(marker and marker.get("channel_required"))
    empty_counts = ChannelCounts()
    if session_dir is None or not required:
        return ChannelGateStatus(
            required=required,
            status="not-required",
            current_chunk=None,
            allowed_channels=(),
            required_channels=(),
            missing_channels=(),
            counts=empty_counts,
        )

    contract = _marker_contract(marker)
    current_chunk = current_chunk_from_history(session_dir)
    allowed_channels = _weapon_chunk_channels(state_root, marker, current_chunk)
    if not allowed_channels:
        allowed_channels = tuple(
            str(x) for x in contract.get("canonical_tools", CANONICAL_TASK_CHANNELS)
            if str(x) in CANONICAL_TASK_CHANNELS
        )
    required_channels = _required_channels(
        contract=contract,
        allowed_channels=allowed_channels,
    )
    counts = session_channel_counts(session_dir)
    missing_channels = tuple(
        name for name in required_channels if counts.accepted.get(name, 0) <= 0
    )
    bad_aliases = scan_bad_channel_aliases(session_dir)
    task_depth = str((marker or {}).get("task_depth") or "full")
    transition_required = bool(
        contract.get("chunk_transition_required", task_depth != "enum_only")
        and task_depth != "enum_only"
    )
    transition_present = has_chunk_transition(session_dir)

    reasons: list[str] = []
    if bad_aliases:
        reasons.append("channel_tool_mismatch")
    if missing_channels:
        reasons.append("structured_channel_output_missing")
    if transition_required and not transition_present:
        reasons.append("chunk_transition_missing")

    if not reasons:
        status = "ok"
    elif session_live:
        status = "pending"
    else:
        status = "blocked"

    return ChannelGateStatus(
        required=True,
        status=status,
        current_chunk=current_chunk,
        allowed_channels=allowed_channels,
        required_channels=required_channels,
        missing_channels=missing_channels,
        counts=counts,
        bad_aliases=bad_aliases,
        transition_required=transition_required,
        transition_present=transition_present,
        reasons=tuple(reasons),
    )
