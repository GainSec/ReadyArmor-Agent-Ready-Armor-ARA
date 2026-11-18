"""Cognitive-inhibition profiles — the runtime side of chunk gems.

A chunk's ``gems`` (``GemRef{layer, rule_subset, inhibition_level, scope}``)
describe how tightly each policy layer is gated while that chunk is active,
and — per ARA's cog-sci foundations — that gate *breathes* by phase:
``enum``/``writeup`` stay highly inhibited, ``probe`` disinhibits net/exec to
explore and exploit. The ROE boundary (``scope="strict"``) is never relaxed by
inhibition — disinhibition widens *what* the agent may attempt, never *where*.

This module derives the active chunk's profile from existing session state
(the current chunk in ``chunk_history.jsonl`` + the weapon's sealed chunkdef).
It is deliberately **fail-strict**: any layer left unresolved, any missing
state, any malformed value defaults to full inhibition (1.0) / strict scope, so
a broken profile can only ever be *more* restrictive, never less.
"""
from __future__ import annotations

import json
from pathlib import Path

from ara.forge.chunks.chunkdef import ChunkDef, read_chunkdef

# Policy layers a gem can gate. Order is stable for deterministic output.
LAYERS: tuple[str, ...] = ("fs", "exec", "net", "reply", "ask", "oc")
PROFILE_FILENAME = "chunk_inhibition.json"


def _fully_inhibited() -> dict:
    return {
        layer: {"inhibition_level": 1.0, "scope": "strict", "rule_subset": None}
        for layer in LAYERS
    }


def _clamp_inhibition(value: object) -> float:
    """Coerce to [0.0, 1.0]; anything invalid → 1.0 (fully inhibited = safe)."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    if v != v:  # NaN
        return 1.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def resolve_inhibition_profile(chunk: ChunkDef | None) -> dict:
    """Return ``{layer: {inhibition_level, scope, rule_subset}}`` for a chunk.

    Every layer starts fully inhibited; a chunk's gems only *lower* inhibition
    for the layers they name. Scope stays ``strict`` unless a gem explicitly
    opts a layer to ``open`` (defensively coerced — any non-``open`` value is
    treated as ``strict``).
    """
    profile = _fully_inhibited()
    if chunk is None:
        return profile
    for gem in (getattr(chunk, "gems", None) or []):
        layer = getattr(gem, "layer", None)
        if layer not in profile:
            continue
        raw_scope = getattr(gem, "scope", "strict")
        profile[layer] = {
            "inhibition_level": _clamp_inhibition(
                getattr(gem, "inhibition_level", 1.0)
            ),
            "scope": "open" if raw_scope == "open" else "strict",
            "rule_subset": getattr(gem, "rule_subset", None),
        }
    return profile


def _current_chunk_id(session_dir: Path) -> str | None:
    hist = Path(session_dir) / "chunk_history.jsonl"
    if not hist.is_file():
        return None
    chunk_id: str | None = None
    try:
        for line in hist.open(errors="ignore"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            to_chunk = row.get("to_chunk")
            if to_chunk:
                chunk_id = str(to_chunk)
    except OSError:
        return None
    return chunk_id


def active_inhibition_profile(
    state_root: Path,
    session_id: str,
    weapon_id: str,
) -> dict:
    """Derive the live inhibition profile from session + weapon state.

    Reads the current chunk from ``chunk_history.jsonl`` and the matching sealed
    chunkdef, then resolves. Fail-strict: any missing/unreadable input yields a
    fully-inhibited profile (identical to today's strict behavior).
    """
    state_root = Path(state_root)
    session_dir = state_root / "sessions" / str(session_id)
    chunk_id = _current_chunk_id(session_dir)
    if not chunk_id or not weapon_id:
        return _fully_inhibited()
    chunk_path = (
        state_root / "weapons" / str(weapon_id) / "chunks" / f"{chunk_id}.json"
    )
    if not chunk_path.is_file():
        return _fully_inhibited()
    try:
        chunk = read_chunkdef(chunk_path)
    except (OSError, ValueError, KeyError, TypeError):
        return _fully_inhibited()
    return resolve_inhibition_profile(chunk)


def write_active_profile(
    state_root: Path,
    session_id: str,
    weapon_id: str,
) -> dict:
    """Compute the active profile and persist it to session state (observable).

    Returns the profile. Best-effort persist — a write failure never raises.
    """
    profile = active_inhibition_profile(state_root, session_id, weapon_id)
    try:
        chunk_id = _current_chunk_id(
            Path(state_root) / "sessions" / str(session_id)
        )
        path = Path(state_root) / "sessions" / str(session_id) / PROFILE_FILENAME
        path.write_text(
            json.dumps({"chunk_id": chunk_id, "profile": profile}, indent=2)
        )
    except OSError:
        pass
    return profile
