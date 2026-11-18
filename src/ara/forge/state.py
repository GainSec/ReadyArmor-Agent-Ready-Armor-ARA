"""Path helpers for forge state directories.

Layout (per orchestrator skeleton §2)::

    <state_root>/
    ├── forge/<forge_id>/
    ├── weapons/<weapon_id>/
    └── sessions/<session_id>/
"""
from __future__ import annotations

from pathlib import Path


def forge_dir(state_root: Path, forge_id: str) -> Path:
    return Path(state_root) / "forge" / forge_id


def weapon_dir(state_root: Path, weapon_id: str) -> Path:
    return Path(state_root) / "weapons" / weapon_id


def session_dir(state_root: Path, session_id: str) -> Path:
    return Path(state_root) / "sessions" / session_id


def forge_audit_path(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "audit.jsonl"


def forge_mats_dir(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "mats"


def forge_manifest_path(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "manifest.jsonl"


def forge_extractions_path(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "extractions.jsonl"


def forge_session_path(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "session.json"


def forge_turn_status_path(state_root: Path, forge_id: str) -> Path:
    """Where the agent's per-turn 'batch_complete / more to derive' signal is
    written (exhaust.status channel), so the driver can cycle vs advance."""
    return forge_dir(state_root, forge_id) / "turn_status.json"


def forge_candidates_path(state_root: Path, forge_id: str) -> Path:
    """Separate pool for the stateless fallback's candidate proposals — NEVER
    the draft. The agent reviews these (keep/add/skip) after its own pass;
    synthesize ignores this file."""
    return forge_dir(state_root, forge_id) / "stateless_candidates.jsonl"


def forge_reconcile_decisions_path(state_root: Path, forge_id: str) -> Path:
    """Append-only log of the reconcile agent's decisions (merge/split/
    resolve_cell/drop). The RECONCILE driver reads new lines after each turn
    and applies them to the pack draft."""
    return forge_dir(state_root, forge_id) / "reconcile_decisions.jsonl"


def forge_phase_path(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "phase.txt"


def forge_weapon_draft_path(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "weapon_draft.json"


def forge_proposal_path(state_root: Path, forge_id: str) -> Path:
    return forge_dir(state_root, forge_id) / "proposal.json"


def session_events_path(state_root: Path, session_id: str) -> Path:
    return session_dir(state_root, session_id) / "events.jsonl"


def ensure_forge_dirs(state_root: Path, forge_id: str) -> Path:
    d = forge_dir(state_root, forge_id)
    (d / "mats").mkdir(parents=True, exist_ok=True)
    return d


def ensure_weapon_dirs(state_root: Path, weapon_id: str) -> Path:
    d = weapon_dir(state_root, weapon_id)
    for sub in ("crumbs", "shapes", "stores", "lines", "chunks", "style", "search"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def ensure_session_dirs(state_root: Path, session_id: str) -> Path:
    d = session_dir(state_root, session_id)
    (d / "evidence").mkdir(parents=True, exist_ok=True)
    return d
