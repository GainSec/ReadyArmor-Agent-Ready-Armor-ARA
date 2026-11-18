"""Armor tempering proposals.

Temper promotes selected trim/task learning into reviewed armor or trim-fit
changes. The first implementation is deliberately deterministic: it reads
existing trim artifacts, writes a proposal JSON, and only mutates armor files
after an operator seals and applies the proposal.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ara import armors as armors_mod
from ara import temper_controls
from ara import temper_ingest
from ara import trims as trims_mod
from ara import weapons as weapons_mod


TEMPER_SCHEMA_VERSION = "0.1"

_ABS_BEGIN_PREFIX = "<!-- ARA-TEMPER-ABSOLUTE-BEGIN"
_ABS_END = "<!-- ARA-TEMPER-ABSOLUTE-END -->"
_BW_BEGIN_PREFIX = "# ARA-TEMPER-BULWARK-BEGIN"
_BW_END = "# ARA-TEMPER-BULWARK-END"
_RHEMA_BEGIN_PREFIX = "<!-- ARA-TEMPER-RHEMA-BEGIN"
_RHEMA_END = "<!-- ARA-TEMPER-RHEMA-END -->"

_ACTIVE_ABS_BEGIN_PREFIX = "<!-- ARA-ACTIVE-TEMPER-ABSOLUTE-BEGIN"
_ACTIVE_ABS_END = "<!-- ARA-ACTIVE-TEMPER-ABSOLUTE-END -->"
_ACTIVE_BW_BEGIN_PREFIX = "# ARA-ACTIVE-TEMPER-BULWARK-BEGIN"
_ACTIVE_BW_END = "# ARA-ACTIVE-TEMPER-BULWARK-END"
_ACTIVE_RHEMA_BEGIN_PREFIX = "<!-- ARA-ACTIVE-TEMPER-RHEMA-BEGIN"
_ACTIVE_RHEMA_END = "<!-- ARA-ACTIVE-TEMPER-RHEMA-END -->"


class TemperStateError(RuntimeError):
    pass


@dataclass
class TemperItem:
    destination: str
    title: str
    source: str
    rationale: str
    risk: str
    default_decision: str
    proposed_text: str
    thread_type: str = ""
    control: dict = field(default_factory=dict)
    scope: list[str] = field(default_factory=list)
    methodology_context: dict = field(default_factory=dict)
    safety_constraints: dict = field(default_factory=dict)
    operator_decision: str = ""
    applied_path: str = ""
    citations: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TemperItem":
        return cls(
            destination=data["destination"],
            title=data.get("title") or data["destination"],
            source=data.get("source") or "",
            rationale=data.get("rationale") or "",
            risk=data.get("risk") or "",
            default_decision=data.get("default_decision") or "keep_for_review",
            proposed_text=data.get("proposed_text") or "",
            thread_type=data.get("thread_type") or "",
            control=dict(data.get("control") or {}),
            scope=[
                str(item).strip()
                for item in list(data.get("scope") or data.get("scopes") or [])
                if str(item).strip()
            ],
            methodology_context=dict(data.get("methodology_context") or {}),
            safety_constraints=dict(data.get("safety_constraints") or {}),
            operator_decision=data.get("operator_decision") or "",
            applied_path=data.get("applied_path") or "",
            citations=list(data.get("citations") or []),
        )


@dataclass
class TemperProposal:
    proposal_id: str
    target_armor: str
    source_trims: list[str]
    source_weapon: str | None = None
    source_forge: str | None = None
    mode: str = "full_temper"
    operator_notes: str = ""
    created_at: float = 0.0
    status: str = "pending"
    sealed_at: float | None = None
    sealed_by: str = ""
    rejected_at: float | None = None
    rejected_by: str = ""
    applied_at: float | None = None
    applied_by: str = ""
    apply_mode: str = ""
    schema_version: str = TEMPER_SCHEMA_VERSION
    items: list[TemperItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["items"] = [item.to_dict() for item in self.items]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TemperProposal":
        return cls(
            proposal_id=data["proposal_id"],
            target_armor=data["target_armor"],
            source_trims=list(data.get("source_trims") or []),
            source_weapon=data.get("source_weapon"),
            source_forge=data.get("source_forge"),
            mode=data.get("mode") or "full_temper",
            operator_notes=data.get("operator_notes") or "",
            created_at=float(data.get("created_at") or 0.0),
            status=data.get("status") or "pending",
            sealed_at=data.get("sealed_at"),
            sealed_by=data.get("sealed_by") or "",
            rejected_at=data.get("rejected_at"),
            rejected_by=data.get("rejected_by") or "",
            applied_at=data.get("applied_at"),
            applied_by=data.get("applied_by") or "",
            apply_mode=data.get("apply_mode") or "",
            schema_version=data.get("schema_version") or TEMPER_SCHEMA_VERSION,
            items=[
                TemperItem.from_dict(item)
                for item in list(data.get("items") or [])
            ],
        )


def proposals_dir(state_root: Path, armor: str) -> Path:
    return Path(state_root) / "armors" / armor / "temper" / "proposals"


def proposal_path(state_root: Path, armor: str, proposal_id: str) -> Path:
    return proposals_dir(state_root, armor) / f"{proposal_id}.json"


def active_temper_path(state_root: Path, armor: str) -> Path:
    return Path(state_root) / "armors" / armor / "temper-active"


def active_temper_materialized_dir(
    state_root: Path, armor: str, proposal_id: str,
) -> Path:
    return (
        Path(state_root)
        / "armors" / armor / "temper" / "active" / proposal_id
    )


def task_absolute_projection_path(state_root: Path, task_id: str) -> Path:
    return Path(state_root) / "task-runs" / task_id / "absolute.md"


def task_temper_projection_manifest_path(state_root: Path, task_id: str) -> Path:
    return Path(state_root) / "task-runs" / task_id / "temper-projection.json"


def active_temper_id(state_root: Path, armor: str) -> str | None:
    path = active_temper_path(state_root, armor)
    if not path.is_file():
        return None
    value = path.read_text().strip()
    return value or None


def _set_active_temper(
    state_root: Path, armor: str, proposal_id: str | None,
) -> None:
    path = active_temper_path(state_root, armor)
    path.parent.mkdir(parents=True, exist_ok=True)
    if proposal_id:
        path.write_text(proposal_id + "\n")
    elif path.exists():
        path.unlink()


def _find_line_start_marker(s: str, marker: str, cursor: int) -> int:
    pos = cursor
    while pos < len(s):
        idx = s.find(marker, pos)
        if idx < 0:
            return -1
        if idx == 0 or s[idx - 1] == "\n":
            return idx
        pos = idx + 1
    return -1


def _strip_marked_block(text: str, begin_marker: str, end_marker: str) -> str:
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        begin = _find_line_start_marker(text, begin_marker, cursor)
        if begin < 0:
            out.append(text[cursor:])
            break
        prefix = text[cursor:begin]
        if prefix.endswith("\n\n"):
            prefix = prefix[:-1]
        out.append(prefix)
        end = _find_line_start_marker(text, end_marker, begin)
        if end < 0:
            cursor = len(text)
            break
        cursor = end + len(end_marker)
        if cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    return "".join(out)


def _active_begin_marker(prefix: str, armor: str, proposal_id: str) -> str:
    marker = f"{prefix} {armor} {proposal_id}"
    if prefix.startswith("<!--"):
        marker += " -->"
    return marker


def _wrap_active_block(
    begin_marker: str, end_marker: str, body: str, *, base: str,
) -> str:
    body = body.rstrip("\n")
    if base.strip():
        prefix = "" if base.endswith("\n\n") else (
            "\n" if base.endswith("\n") else "\n\n"
        )
    else:
        prefix = ""
    return f"{prefix}{begin_marker}\n{body}\n{end_marker}\n"


def _merge_active_block(
    path: Path, begin_marker: str, end_marker: str, body: str,
) -> Path:
    existing = path.read_text() if path.is_file() else ""
    base = _strip_marked_block(existing, begin_marker, end_marker)
    merged = base
    if body.strip():
        merged = base + _wrap_active_block(
            begin_marker, end_marker, body, base=base,
        )
    if merged != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(merged)
    return path


def load_proposal(
    state_root: Path, armor: str, proposal_id: str,
) -> TemperProposal | None:
    path = proposal_path(state_root, armor, proposal_id)
    if not path.is_file():
        return None
    try:
        return TemperProposal.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def list_proposals(
    state_root: Path, armor: str | None = None,
) -> list[TemperProposal]:
    root = Path(state_root)
    paths: list[Path] = []
    if armor:
        paths = sorted(proposals_dir(root, armor).glob("*.json"))
    else:
        armors_root = root / "armors"
        if armors_root.is_dir():
            for armor_dir in sorted(armors_root.iterdir()):
                if armor_dir.is_dir():
                    paths.extend(
                        sorted((armor_dir / "temper" / "proposals").glob("*.json"))
                    )
    out: list[TemperProposal] = []
    for path in paths:
        try:
            out.append(TemperProposal.from_dict(json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda p: (p.created_at, p.proposal_id), reverse=True)


def create_proposal(
    state_root: Path,
    target_armor: str,
    *,
    source_trims: list[str],
    source_weapon: str | None = None,
    source_forge: str | None = None,
    mode: str = "full_temper",
    operator_notes: str = "",
) -> TemperProposal:
    state_root = Path(state_root)
    armor = armors_mod.load_armor(state_root, target_armor)
    if armor is None:
        raise FileNotFoundError(f"armor {target_armor!r} not found")
    if not source_trims:
        raise ValueError("at least one source trim is required")

    metas = []
    for trim_id in source_trims:
        meta = trims_mod.load_trim(state_root, target_armor, trim_id)
        if meta is None:
            raise FileNotFoundError(
                f"trim {trim_id!r} under armor {target_armor!r} not found"
            )
        metas.append(meta)

    created_at = time.time()
    proposal_id = _proposal_id(
        target_armor, source_trims, source_weapon, source_forge, operator_notes,
        created_at,
    )
    items: list[TemperItem] = []
    for meta in metas:
        items.extend(_items_from_trim(state_root, meta, mode))
    if not items:
        raise ValueError("selected trims produced no promotable material")

    proposal = TemperProposal(
        proposal_id=proposal_id,
        target_armor=target_armor,
        source_trims=list(source_trims),
        source_weapon=source_weapon,
        source_forge=source_forge,
        mode=mode,
        operator_notes=operator_notes,
        created_at=created_at,
        items=items,
    )
    _write_proposal(state_root, proposal)
    return proposal


def create_proposal_from_weapon(
    state_root: Path,
    target_armor: str,
    *,
    source_weapon: str,
    source_forge: str | None = None,
    mode: str = "full_temper",
    operator_notes: str = "",
) -> TemperProposal:
    state_root = Path(state_root)
    armor = armors_mod.load_armor(state_root, target_armor)
    if armor is None:
        raise FileNotFoundError(f"armor {target_armor!r} not found")
    weapon_path = weapons_mod.weapon_dir(state_root, source_weapon) / "weapon.json"
    if not weapon_path.is_file():
        raise FileNotFoundError(f"weapon {source_weapon!r} not found")
    try:
        weapon = json.loads(weapon_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"weapon {source_weapon!r} has invalid weapon.json") from exc

    created_at = time.time()
    source_forge = source_forge or weapon.get("forge_id")
    proposal_id = _proposal_id(
        target_armor, [], source_weapon, source_forge, operator_notes,
        created_at,
    )
    items = _items_from_weapon(source_weapon, weapon, mode)
    if not items:
        raise ValueError("weapon produced no promotable temper material")

    proposal = TemperProposal(
        proposal_id=proposal_id,
        target_armor=target_armor,
        source_trims=[],
        source_weapon=source_weapon,
        source_forge=source_forge,
        mode=mode,
        operator_notes=operator_notes,
        created_at=created_at,
        items=items,
    )
    _write_proposal(state_root, proposal)
    return proposal


def create_proposal_from_tailor_session(
    state_root: Path,
    target_armor: str,
    *,
    session_id: str | None = None,
    mode: str | None = None,
    operator_notes: str = "",
) -> TemperProposal:
    state_root = Path(state_root)
    armor = armors_mod.load_armor(state_root, target_armor)
    if armor is None:
        raise FileNotFoundError(f"armor {target_armor!r} not found")

    from ara import temper_lifecycle

    record = temper_lifecycle.load_tailor_session(state_root)
    if record is None:
        raise FileNotFoundError("tailor session not found")
    if session_id and record.get("session_id") != session_id:
        raise FileNotFoundError(f"tailor session {session_id!r} not found")
    if record.get("status") == "aborted":
        raise TemperStateError("aborted tailor sessions cannot create proposals")

    candidates = list(record.get("candidates") or [])
    if not candidates:
        raise ValueError("tailor session produced no temper candidates")

    source_request = dict(record.get("source_request") or {})
    source_trims = [
        str(trim).strip()
        for trim in list(source_request.get("source_trims") or [])
        if str(trim).strip()
    ]
    source_weapon = (
        source_request.get("source_weapon")
        or source_request.get("weapon")
        or None
    )
    source_forge = source_request.get("source_forge") or source_request.get("forge")
    proposal_mode = mode or source_request.get("mode") or "full_temper"
    created_at = time.time()
    notes = operator_notes or f"Tailor session {record.get('session_id')}"
    proposal_id = _proposal_id(
        target_armor,
        source_trims,
        source_weapon,
        source_forge,
        f"{notes}|{record.get('session_id')}",
        created_at,
    )

    items = [
        _item_from_tailor_candidate(candidate, idx)
        for idx, candidate in enumerate(candidates, start=1)
    ]
    temper_lifecycle.validate_tailor_coverage_for_proposal(record)
    items, drop_note = _dedup_and_cap_items(items)
    if drop_note:
        notes = (notes + " " + drop_note).strip()
    proposal = TemperProposal(
        proposal_id=proposal_id,
        target_armor=target_armor,
        source_trims=source_trims,
        source_weapon=source_weapon,
        source_forge=source_forge,
        mode=proposal_mode,
        operator_notes=notes,
        created_at=created_at,
        items=items,
    )
    _write_proposal(state_root, proposal)
    return proposal


# D1/BUG-4: the agent emits near-duplicate guardrails (the same web-vs-hardware
# absolute once per source, overlapping reply-rewrite rhema rules, etc.). Dedup
# by a destination-aware semantic signature and cap per (destination, source)
# so the proposal — and the armor absolute it composes into — stays lean.
PER_DESTINATION_SOURCE_CAP = 6


def _item_signature(item: "TemperItem") -> tuple:
    c = item.control or {}
    d = item.destination

    def _set(key: str) -> tuple:
        v = c.get(key)
        return tuple(sorted(str(x) for x in v)) if isinstance(v, list) else ()

    if d == "absolute":
        return (d, _set("allowed"), _set("restricted"))
    if d == "rhema":
        return (d, str(c.get("action")), str(c.get("layer")), str(c.get("pattern")))
    if d == "bulwark":
        return (d, str(c.get("action")), str(c.get("layer")), str(c.get("match")))
    if d == "canary":
        return (d, str(c.get("signal")), str(c.get("pattern")))
    return (d, json.dumps(c, sort_keys=True, default=str))


def _dedup_and_cap_items(
    items: list["TemperItem"],
) -> tuple[list["TemperItem"], str]:
    """Drop semantic duplicates and cap per (destination, source).

    Returns the kept items plus an operator-facing note describing what was
    dropped (never a silent truncation)."""
    seen: set[tuple] = set()
    per_ds: dict[tuple, int] = {}
    kept: list["TemperItem"] = []
    dup_dropped = 0
    cap_dropped = 0
    for item in items:
        sig = _item_signature(item)
        if sig in seen:
            dup_dropped += 1
            continue
        ds_key = (item.destination, item.source)
        if per_ds.get(ds_key, 0) >= PER_DESTINATION_SOURCE_CAP:
            cap_dropped += 1
            continue
        seen.add(sig)
        per_ds[ds_key] = per_ds.get(ds_key, 0) + 1
        kept.append(item)
    notes: list[str] = []
    if dup_dropped:
        notes.append(f"{dup_dropped} duplicate")
    if cap_dropped:
        notes.append(f"{cap_dropped} over-cap")
    note = (
        f"[deduped: dropped {', '.join(notes)} of {len(items)} candidates]"
        if notes else ""
    )
    return kept, note


def validate_seal(proposal: TemperProposal) -> list[str]:
    """D1: hard seal validators for a Temper proposal (mirrors the forge seal
    validator framework). Returns a list of failure messages; empty == ok.

    Hard failures block sealing: no items, an invalid destination, or a
    malformed/over-broad control that somehow reached seal. Missing citations
    are a soft concern (the deterministic trim/weapon paths are citationless by
    design) and do not block here.
    """
    failures: list[str] = []
    if not proposal.items:
        failures.append("proposal has no items to seal")
    for idx, item in enumerate(proposal.items):
        if item.destination not in temper_controls.DESTINATIONS:
            failures.append(f"item {idx}: invalid destination {item.destination!r}")
            continue
        if item.control:
            try:
                temper_controls.validate_control(item.destination, dict(item.control))
                if item.destination == "rhema":
                    temper_controls.validate_rhema_pattern(
                        (item.control or {}).get("pattern")
                    )
            except temper_controls.TemperControlError as exc:
                failures.append(f"item {idx} ({item.destination}): {exc}")
    return failures


def seal_proposal(
    state_root: Path, armor: str, proposal_id: str, *,
    operator: str = "operator",
) -> TemperProposal:
    proposal = _require_proposal(state_root, armor, proposal_id)
    if proposal.status == "rejected":
        raise TemperStateError("rejected proposals cannot be sealed")
    if proposal.status == "applied":
        raise TemperStateError("applied proposals cannot be resealed")
    failures = validate_seal(proposal)
    if failures:
        raise TemperStateError("seal blocked: " + "; ".join(failures))
    proposal.status = "sealed"
    proposal.sealed_at = time.time()
    proposal.sealed_by = operator
    _write_proposal(state_root, proposal)
    return proposal


def seal_selected_proposal(
    state_root: Path,
    armor: str,
    proposal_id: str,
    *,
    selected_indexes: list[int],
    operator: str = "operator",
) -> TemperProposal:
    state_root = Path(state_root)
    source = _require_proposal(state_root, armor, proposal_id)
    if source.status != "pending":
        raise TemperStateError("only pending proposals can be selectively sealed")
    normalized = sorted(set(int(index) for index in selected_indexes))
    if not normalized:
        raise TemperStateError("select at least one Temper item to seal")
    invalid = [
        index for index in normalized
        if index < 0 or index >= len(source.items)
    ]
    if invalid:
        raise TemperStateError(
            "selected Temper item index out of range: "
            + ", ".join(str(index) for index in invalid)
        )
    created_at = time.time()
    selected = [
        TemperItem.from_dict(source.items[index].to_dict())
        for index in normalized
    ]
    failures = validate_seal(
        TemperProposal(
            proposal_id="", target_armor=armor, source_trims=[],
            items=selected,
        )
    )
    if failures:
        raise TemperStateError("seal blocked: " + "; ".join(failures))
    notes = (
        f"Selected {len(selected)} of {len(source.items)} items from "
        f"{source.proposal_id}."
    )
    if source.operator_notes:
        notes += " " + source.operator_notes
    proposal = TemperProposal(
        proposal_id=_proposal_id(
            armor,
            source.source_trims,
            source.source_weapon,
            source.source_forge,
            notes,
            created_at,
        ),
        target_armor=source.target_armor,
        source_trims=list(source.source_trims),
        source_weapon=source.source_weapon,
        source_forge=source.source_forge,
        mode=source.mode,
        operator_notes=notes,
        created_at=created_at,
        status="sealed",
        sealed_at=created_at,
        sealed_by=operator,
        items=selected,
    )
    _write_proposal(state_root, proposal)
    return proposal


def activate_temper(
    state_root: Path, armor: str, proposal_id: str,
) -> dict:
    state_root = Path(state_root)
    proposal = _require_proposal(state_root, armor, proposal_id)
    if proposal.status not in {"sealed", "applied"}:
        raise TemperStateError("only sealed or applied Temper proposals can be active")
    current = active_temper_id(state_root, armor)
    if current:
        _release_active_temper(state_root, armor, current)
        _set_active_temper(state_root, armor, None)
    materialized = _materialize_active_temper(state_root, armor, proposal)
    _set_active_temper(state_root, armor, proposal_id)
    return {
        "armor": armor,
        "proposal_id": proposal_id,
        "active": True,
        "materialized": materialized,
    }


def release_temper(
    state_root: Path, armor: str, proposal_id: str | None = None,
) -> dict:
    state_root = Path(state_root)
    current = active_temper_id(state_root, armor)
    if proposal_id is not None and current != proposal_id:
        return {
            "armor": armor,
            "proposal_id": proposal_id,
            "released": False,
        }
    if current:
        _release_active_temper(state_root, armor, current)
    _set_active_temper(state_root, armor, None)
    return {
        "armor": armor,
        "proposal_id": current,
        "released": current is not None,
    }


def reject_proposal(
    state_root: Path, armor: str, proposal_id: str, *,
    operator: str = "operator",
) -> TemperProposal:
    proposal = _require_proposal(state_root, armor, proposal_id)
    if proposal.status == "applied":
        raise TemperStateError("applied proposals cannot be rejected")
    proposal.status = "rejected"
    proposal.rejected_at = time.time()
    proposal.rejected_by = operator
    _write_proposal(state_root, proposal)
    return proposal


def apply_proposal(
    state_root: Path,
    armor: str,
    proposal_id: str,
    *,
    operator: str = "operator",
    apply_mode: str = "stage",
) -> TemperProposal:
    state_root = Path(state_root)
    proposal = _require_proposal(state_root, armor, proposal_id)
    if proposal.status != "sealed":
        raise TemperStateError("proposal must be sealed before apply")
    armor_obj = armors_mod.load_armor(state_root, armor)
    if armor_obj is None:
        raise FileNotFoundError(f"armor {armor!r} not found")

    for idx, item in enumerate(proposal.items, start=1):
        decision = item.operator_decision or item.default_decision
        if decision in {"reject", "do_not_promote", "skip"}:
            continue
        if item.destination == "absolute":
            item.applied_path = str(_append_absolute(armor_obj.absolute_path, proposal, item))
        elif item.destination == "bulwark":
            item.applied_path = str(_append_bulwark(state_root / "bulwark.md", proposal, item))
        elif item.destination == "rhema":
            item.applied_path = str(_append_rhema(state_root / "rhema.md", proposal, item))
        elif item.destination in {"canary", "approval_gate", "alert_hook"}:
            item.applied_path = str(_write_control_artifact(
                state_root, armor, proposal, item, idx,
            ))
        elif item.destination in {"trim", "trim_template"}:
            item.applied_path = str(_write_trim_template(
                state_root, armor, proposal, item, idx,
            ))

    proposal.status = "applied"
    proposal.applied_at = time.time()
    proposal.applied_by = operator
    proposal.apply_mode = apply_mode
    _write_proposal(state_root, proposal)
    return proposal


def _require_proposal(
    state_root: Path, armor: str, proposal_id: str,
) -> TemperProposal:
    proposal = load_proposal(state_root, armor, proposal_id)
    if proposal is None:
        raise FileNotFoundError(
            f"temper proposal {proposal_id!r} under armor {armor!r} not found"
        )
    return proposal


def _materialize_active_temper(
    state_root: Path, armor: str, proposal: TemperProposal,
) -> dict[str, list[str]]:
    active_dir = active_temper_materialized_dir(
        state_root, armor, proposal.proposal_id,
    )
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "manifest.json").write_text(json.dumps({
        "armor": armor,
        "proposal_id": proposal.proposal_id,
        "status": proposal.status,
        "item_count": len(proposal.items),
    }, indent=2, sort_keys=True))
    materialized: dict[str, list[str]] = {
        "overlaid": [],
        "artifacts": [str(active_dir / "manifest.json")],
        "skipped": [],
    }
    items = [
        item for item in proposal.items
        if (item.operator_decision or item.default_decision)
        not in {"reject", "do_not_promote", "skip"}
    ]
    # Route every accepted item to its enforced home via the ingest plan.
    # Enforceable entries (bulwark/rhema/canary/approval_gate/alert_hook)
    # become Bulwark rules in <state>/bulwark.md; absolute (and advisory
    # fallbacks) land in the armor ABSOLUTE. No more inert artifacts.
    plans = [(item, temper_ingest.plan_item(item)) for item in items]
    bulwark_lines = [
        p.rule_text for _, p in plans if p.home == temper_ingest.BULWARK_HOME
    ]
    absolute_bodies = [
        p.rule_text for _, p in plans
        if p.home == temper_ingest.ABSOLUTE_HOME and p.rule_text.strip()
    ]

    if absolute_bodies:
        armor_obj = armors_mod.load_armor(state_root, armor)
        if armor_obj is None:
            raise FileNotFoundError(f"armor {armor!r} not found")
        begin = _active_begin_marker(
            _ACTIVE_ABS_BEGIN_PREFIX, armor, proposal.proposal_id,
        )
        body = "\n\n".join(b.rstrip() for b in absolute_bodies).rstrip() + "\n"
        _merge_active_block(
            armor_obj.absolute_path, begin, _ACTIVE_ABS_END, body,
        )
        materialized["overlaid"].append(str(armor_obj.absolute_path))

    if bulwark_lines:
        begin = _active_begin_marker(
            _ACTIVE_BW_BEGIN_PREFIX, armor, proposal.proposal_id,
        )
        # Self-contained ``## global`` heading so rules register regardless of
        # the surrounding file; the block is appended at end of bulwark.md.
        body = "## global\n" + "\n".join(
            line.rstrip() for line in bulwark_lines if line.strip()
        ).rstrip() + "\n"
        path = _merge_active_block(
            state_root / "bulwark.md", begin, _ACTIVE_BW_END, body,
        )
        materialized["overlaid"].append(str(path))

    materialized["enforced"] = sorted(
        {p.destination for _, p in plans if p.enforced}
    )
    materialized["advisory"] = sorted(
        {p.destination for _, p in plans if not p.enforced}
    )
    return materialized


def project_active_temper_for_task(
    state_root: Path,
    armor: str,
    task_id: str,
    *,
    trim_id: str | None = None,
    weapon_id: str | None = None,
) -> dict:
    state_root = Path(state_root)
    task_id = str(task_id or "").strip()
    if not task_id:
        raise TemperStateError("task_id is required for Temper task projection")
    proposal_id = active_temper_id(state_root, armor)
    if not proposal_id:
        # New model: no legacy armor-scoped temper. Equipment-slot tempers
        # (tempers.py) inject their doctrine live via active_absolute_rule, so
        # there is nothing to project per-task — return a no-op projection
        # rather than blocking the task run.
        return {
            "armor": armor,
            "task_id": task_id,
            "proposal_id": None,
            "trim_id": trim_id,
            "weapon_id": weapon_id,
            "task_scope": [],
            "selected": {"absolute": 0},
            "excluded": {"absolute": 0},
            "selected_items": [],
            "excluded_items": [],
            "note": "no armor-scoped temper; equipment-slot tempers apply live",
        }
    proposal = _require_proposal(state_root, armor, proposal_id)
    if proposal.status not in {"sealed", "applied"}:
        raise TemperStateError("active temper must be sealed or applied")
    armor_obj = armors_mod.load_armor(state_root, armor)
    if armor_obj is None:
        raise FileNotFoundError(f"armor {armor!r} not found")

    task_families = _derive_task_scope_tags(
        state_root, armor, trim_id=trim_id, weapon_id=weapon_id,
    )
    accepted_items = [
        item for item in proposal.items
        if (item.operator_decision or item.default_decision)
        not in {"reject", "do_not_promote", "skip"}
    ]
    selected_absolute: list[TemperItem] = []
    excluded_absolute: list[TemperItem] = []
    for item in accepted_items:
        if item.destination != "absolute":
            continue
        if _temper_item_applies_to_task(item, task_families):
            selected_absolute.append(item)
        else:
            excluded_absolute.append(item)

    existing = armor_obj.absolute_path.read_text() if armor_obj.absolute_path.is_file() else ""
    base = _strip_marked_block(
        existing,
        _active_begin_marker(_ACTIVE_ABS_BEGIN_PREFIX, armor, proposal_id),
        _ACTIVE_ABS_END,
    )
    body = _render_active_items(selected_absolute)
    projected_text = base
    if body.strip():
        projected_text = base + _wrap_active_block(
            _active_begin_marker(_ACTIVE_ABS_BEGIN_PREFIX, armor, proposal_id),
            _ACTIVE_ABS_END,
            body,
            base=base,
        )

    absolute_path = task_absolute_projection_path(state_root, task_id)
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_text(projected_text.rstrip() + ("\n" if projected_text else ""))

    manifest = {
        "armor": armor,
        "task_id": task_id,
        "proposal_id": proposal_id,
        "trim_id": trim_id,
        "weapon_id": weapon_id,
        "task_scope": sorted(task_families),
        "absolute_path": str(absolute_path),
        "selected": {"absolute": len(selected_absolute)},
        "excluded": {"absolute": len(excluded_absolute)},
        "selected_items": [
            {
                "title": item.title,
                "scope": sorted(_temper_item_scope_tags(item)),
                "destination": item.destination,
            }
            for item in selected_absolute
        ],
        "excluded_items": [
            {
                "title": item.title,
                "scope": sorted(_temper_item_scope_tags(item)),
                "destination": item.destination,
            }
            for item in excluded_absolute
        ],
    }
    manifest_path = task_temper_projection_manifest_path(state_root, task_id)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _derive_task_scope_tags(
    state_root: Path,
    armor: str,
    *,
    trim_id: str | None,
    weapon_id: str | None,
) -> set[str]:
    tags: set[str] = set()
    trim_weapon = ""
    if trim_id:
        try:
            meta = trims_mod.load_trim(state_root, armor, trim_id)
        except Exception:
            meta = None
        if meta is not None:
            trim_weapon = str(getattr(meta, "weapon_ref", "") or "")
            blob = " ".join([
                str(getattr(meta, "target", "") or ""),
                " ".join(str(item) for item in getattr(meta, "channels", []) or []),
                str(getattr(meta, "posture", "") or ""),
                str(getattr(meta, "depth", "") or ""),
                trim_weapon,
            ])
            tags.update(_infer_scope_tags_from_text(blob))
    if weapon_id:
        tags.update(_infer_scope_tags_from_text(str(weapon_id)))
        tags.update(_infer_scope_tags_from_weapon(state_root, weapon_id))
    if trim_weapon:
        tags.update(_infer_scope_tags_from_weapon(state_root, trim_weapon))
    return {tag for tag in tags if tag not in {"global", "universal"}}


def _infer_scope_tags_from_weapon(state_root: Path, weapon_id: str) -> set[str]:
    path = weapons_mod.weapon_dir(state_root, weapon_id) / "weapon.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    values: list[str] = [weapon_id]
    for key in (
        "weapon_target_id", "alphabet_pack_name", "task_family",
        "methodology_family", "name", "title", "description",
    ):
        value = data.get(key)
        if isinstance(value, str):
            values.append(value)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        values.extend(str(value) for value in metadata.values())
    return _infer_scope_tags_from_text(" ".join(values))


def _temper_item_applies_to_task(item: TemperItem, task_families: set[str]) -> bool:
    item_tags = _temper_item_scope_tags(item)
    if item_tags & {"global", "universal"}:
        return True
    if not item_tags:
        return False
    return bool(item_tags & task_families)


def _temper_item_scope_tags(item: TemperItem) -> set[str]:
    explicit: set[str] = set()
    explicit.update(_normalize_scope_tag(value) for value in item.scope)
    for context in (item.methodology_context, item.control.get("methodology_context")):
        if isinstance(context, dict):
            explicit.add(_normalize_scope_tag(context.get("task_family")))
            for key in ("task_families", "families", "scope"):
                value = context.get(key)
                if isinstance(value, list):
                    explicit.update(_normalize_scope_tag(part) for part in value)
    control_scope = item.control.get("scope") if isinstance(item.control, dict) else None
    if isinstance(control_scope, list):
        explicit.update(_normalize_scope_tag(part) for part in control_scope)
    elif isinstance(control_scope, str):
        explicit.add(_normalize_scope_tag(control_scope))
    explicit = {tag for tag in explicit if tag}
    if explicit:
        return explicit
    return {
        tag for tag in _infer_scope_tags_from_text(_temper_item_scope_blob(item))
        if tag
    }


def _temper_item_scope_blob(item: TemperItem) -> str:
    values = [
        item.title,
        item.rationale,
        item.risk,
        item.proposed_text,
    ]
    if isinstance(item.control, dict):
        for value in item.control.values():
            if isinstance(value, list):
                values.extend(str(part) for part in value)
            elif isinstance(value, dict):
                values.extend(str(part) for part in value.values())
            else:
                values.append(str(value))
    return " ".join(values)


def _infer_scope_tags_from_text(text: str) -> set[str]:
    blob = str(text or "").lower()
    tags: set[str] = set()
    if re.search(r"\b(web[-_ ]?app|wstg|http|https|browser|xss|sql injection|api testing)\b", blob):
        tags.add("web_app")
    if re.search(r"\b(ios|android|mobile[-_ ]?app|mobile)\b", blob):
        tags.add("mobile")
    if re.search(r"\b(hardware|firmware|binwalk|flashrom|esptool|jtag|uart|debug interface)\b", blob):
        tags.add("hardware")
    if re.search(r"\b(rf|wireless|bluetooth|wi[- ]?fi|spectrum|radio)\b", blob):
        tags.add("rf_research")
    if re.search(r"\b(anti[-_ ]?crime|incident workflow|law enforcement|suspect data)\b", blob):
        tags.add("anti_crime")
    if re.search(r"\b(global|universal|all sessions|all tasks)\b", blob):
        tags.add("global")
    return tags


def _normalize_scope_tag(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return ""
    aliases = {
        "web": "web_app",
        "webapp": "web_app",
        "web_application": "web_app",
        "mobile_app": "mobile",
        "ios": "mobile",
        "android": "mobile",
        "firmware": "hardware",
        "rf": "rf_research",
        "wireless": "rf_research",
        "universal": "global",
    }
    return aliases.get(text, text)


def _release_active_temper(
    state_root: Path, armor: str, proposal_id: str,
) -> None:
    armor_obj = armors_mod.load_armor(state_root, armor)
    if armor_obj is not None:
        begin = _active_begin_marker(
            _ACTIVE_ABS_BEGIN_PREFIX, armor, proposal_id,
        )
        _strip_active_file(armor_obj.absolute_path, begin, _ACTIVE_ABS_END)
    _strip_active_file(
        state_root / "bulwark.md",
        _active_begin_marker(_ACTIVE_BW_BEGIN_PREFIX, armor, proposal_id),
        _ACTIVE_BW_END,
    )
    _strip_active_file(
        state_root / "rhema.md",
        _active_begin_marker(_ACTIVE_RHEMA_BEGIN_PREFIX, armor, proposal_id),
        _ACTIVE_RHEMA_END,
    )
    active_dir = active_temper_materialized_dir(state_root, armor, proposal_id)
    if active_dir.is_dir():
        shutil.rmtree(active_dir)


def _strip_active_file(path: Path, begin_marker: str, end_marker: str) -> None:
    if not path.is_file():
        return
    existing = path.read_text()
    stripped = _strip_marked_block(existing, begin_marker, end_marker)
    if stripped != existing:
        path.write_text(stripped)


def _render_active_items(items: list[TemperItem]) -> str:
    rendered: list[str] = []
    for item in items:
        control = _control_for_render(item)
        body = temper_controls.render_control(
            item.destination,
            control,
            citations=item.citations,
            fallback_text=item.proposed_text,
        ).strip()
        if body:
            rendered.append(body)
    return "\n\n".join(rendered).rstrip() + ("\n" if rendered else "")


def _write_active_temper_artifact(
    state_root: Path,
    armor: str,
    proposal: TemperProposal,
    item: TemperItem,
    index: int,
) -> Path:
    root = (
        active_temper_materialized_dir(state_root, armor, proposal.proposal_id)
        / item.destination
    )
    root.mkdir(parents=True, exist_ok=True)
    slug = _slug(item.title) or f"item-{index}"
    suffix = "md" if item.destination == "trim_template" else "json"
    path = root / f"{index:03d}-{slug}.{suffix}"
    rendered = temper_controls.render_control(
        item.destination,
        _control_for_render(item),
        citations=item.citations,
        fallback_text=item.proposed_text,
    )
    if suffix == "md":
        path.write_text(rendered.rstrip() + "\n")
        return path
    payload = {
        "proposal_id": proposal.proposal_id,
        "destination": item.destination,
        "thread_type": item.thread_type,
        "title": item.title,
        "source": item.source,
        "rationale": item.rationale,
        "risk": item.risk,
        "control": item.control,
        "scope": item.scope,
        "methodology_context": item.methodology_context,
        "safety_constraints": item.safety_constraints,
        "citations": item.citations,
        "rendered": rendered,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _write_proposal(state_root: Path, proposal: TemperProposal) -> None:
    path = proposal_path(state_root, proposal.target_armor, proposal.proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal.to_dict(), indent=2, sort_keys=True) + "\n")


def _proposal_id(
    armor: str,
    trims: list[str],
    weapon: str | None,
    forge: str | None,
    notes: str,
    created_at: float,
) -> str:
    seed = json.dumps({
        "armor": armor,
        "trims": trims,
        "weapon": weapon,
        "forge": forge,
        "notes": notes,
        "created_at": created_at,
    }, sort_keys=True)
    digest = hashlib.sha256(seed.encode()).hexdigest()[:10]
    return f"temper-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(created_at))}-{digest}"


def _items_from_trim(
    state_root: Path, meta: trims_mod.TrimMeta, mode: str,
) -> list[TemperItem]:
    trim_id = meta.trim_id
    abs_text = _read_text(
        trims_mod.trim_absolute_path(state_root, meta.armor, trim_id)
    )
    bw_text = _read_text(
        trims_mod.trim_bulwark_section_path(state_root, meta.armor, trim_id)
    )
    safe_abs = _sanitize_task_specific(abs_text, meta)
    safe_bw = _sanitize_task_specific(bw_text, meta)
    items: list[TemperItem] = []

    if mode in {"full_temper", "doctrine", "doctrine_only", "absolute"}:
        items.append(TemperItem(
            destination="absolute",
            title=f"Doctrine candidate from {trim_id}",
            source=f"trim:{trim_id}",
            rationale=(
                "Promotes repeated task posture, cadence, and operator "
                "preferences without carrying client-specific targets."
            ),
            risk="medium",
            default_decision="keep_for_review",
            proposed_text=_absolute_candidate(meta, safe_abs),
        ))

    if mode in {"full_temper", "boundaries", "boundaries_only", "bulwark"}:
        items.append(TemperItem(
            destination="bulwark",
            title=f"Scoped boundary candidate from {trim_id}",
            source=f"trim:{trim_id}",
            rationale=(
                "Keeps reusable boundary posture scoped to this armor instead "
                "of writing global policy."
            ),
            risk="high",
            default_decision="keep_for_review",
            proposed_text=_bulwark_candidate(meta, safe_bw),
        ))

    if mode in {"full_temper", "trim_fit", "trim", "template"}:
        items.append(TemperItem(
            destination="trim_template",
            title=f"Reusable trim fit from {trim_id}",
            source=f"trim:{trim_id}",
            rationale=(
                "Keeps task-specific tone, depth, and workflow choices as "
                "reusable trim material instead of permanent armor doctrine."
            ),
            risk="low",
            default_decision="keep_as_template",
            proposed_text=_trim_template_candidate(meta),
        ))

    return items


def _items_from_weapon(
    weapon_id: str, weapon: dict, mode: str,
) -> list[TemperItem]:
    crumbs = list(weapon.get("crumbs") or [])
    shapes = list(weapon.get("shapes") or [])
    fact_shapes = list(weapon.get("fact_shapes") or [])
    stores = list(weapon.get("stores") or [])
    lines = list(weapon.get("lines") or [])
    items: list[TemperItem] = []

    if mode in {"full_temper", "doctrine", "doctrine_only", "absolute"}:
        text = _weapon_doctrine_candidate(
            weapon_id, crumbs, shapes, fact_shapes, stores, lines,
        )
        if text.strip():
            items.append(TemperItem(
                destination="absolute",
                title=f"Forge methodology candidate from {weapon_id}",
                source=f"weapon:{weapon_id}",
                rationale=(
                    "Promotes durable methodology, output expectations, and "
                    "knowledge posture extracted during forge review."
                ),
                risk="medium",
                default_decision="keep_for_review",
                proposed_text=text,
            ))

    if mode in {"full_temper", "boundaries", "boundaries_only", "bulwark"}:
        text = _weapon_boundary_candidate(weapon_id, lines)
        if text.strip():
            items.append(TemperItem(
                destination="bulwark",
                title=f"Forge boundary candidate from {weapon_id}",
                source=f"weapon:{weapon_id}",
                rationale=(
                    "Keeps weapon-discovered scope boundaries reviewable "
                    "before any standing armor boundary is broadened."
                ),
                risk="high",
                default_decision="keep_for_review",
                proposed_text=text,
            ))

    if mode in {"full_temper", "trim_fit", "trim", "template"}:
        text = _weapon_trim_template_candidate(weapon_id, weapon)
        if text.strip():
            items.append(TemperItem(
                destination="trim_template",
                title=f"Forge task-fit candidate from {weapon_id}",
                source=f"weapon:{weapon_id}",
                rationale=(
                    "Keeps the weapon's tone, evidence style, and task shape "
                    "available as reusable trim material."
                ),
                risk="low",
                default_decision="keep_as_template",
                proposed_text=text,
            ))

    return items


def _item_from_tailor_candidate(candidate: dict, index: int) -> TemperItem:
    citations = candidate.get("citations")
    if not isinstance(citations, list) or not citations:
        title = candidate.get("title") or f"candidate {index}"
        raise ValueError(f"tailor candidate {title!r} is missing citation evidence")
    clean_citations = []
    for citation in citations:
        if not isinstance(citation, dict) or not citation:
            title = candidate.get("title") or f"candidate {index}"
            raise ValueError(
                f"tailor candidate {title!r} has invalid citation evidence"
            )
        clean_citations.append(dict(citation))
    required = [
        "thread_type",
        "destination",
        "title",
        "source",
        "rationale",
        "risk",
        "default_decision",
        "proposed_text",
        "control",
    ]
    missing = [
        field_name for field_name in required
        if not str(candidate.get(field_name) or "").strip()
    ]
    if missing:
        raise ValueError(
            f"tailor candidate {index} missing required fields: "
            + ", ".join(missing)
        )
    try:
        thread_type = temper_controls.validate_thread_type(
            candidate.get("thread_type")
        )
        control = temper_controls.validate_control(
            str(candidate.get("destination") or ""),
            candidate.get("control"),
        )
    except temper_controls.TemperControlError as exc:
        title = candidate.get("title") or f"candidate {index}"
        raise ValueError(f"tailor candidate {title!r} has invalid control: {exc}") from exc
    return TemperItem(
        destination=str(candidate["destination"]),
        title=str(candidate["title"]),
        source=str(candidate["source"]),
        rationale=str(candidate["rationale"]),
        risk=str(candidate["risk"]),
        default_decision=str(candidate["default_decision"]),
        proposed_text=str(candidate["proposed_text"]),
        thread_type=thread_type,
        control=control,
        scope=[
            str(item).strip()
            for item in list(candidate.get("scope") or [])
            if str(item).strip()
        ],
        methodology_context=dict(candidate.get("methodology_context") or {}),
        safety_constraints=dict(candidate.get("safety_constraints") or {}),
        citations=clean_citations,
    )


def _absolute_candidate(meta: trims_mod.TrimMeta, safe_abs: str) -> str:
    lines = [
        f"## Tempered doctrine from trim `{meta.trim_id}`",
        "",
        f"- Default posture: `{meta.posture}` at `{meta.depth}` depth.",
        f"- Active testing default: {'allowed with approval' if meta.active_testing else 'off by default'}.",
        f"- Gateway defaults: {', '.join(meta.channels) if meta.channels else 'inherit armor defaults'}.",
        f"- Task target references are represented as `[task-target]`.",
    ]
    if meta.tod_allowed:
        lines.append("- Keep TOD windows as reviewed task cadence, not as client-specific facts.")
    if meta.rate_limits:
        lines.append(f"- Rate-limit posture: `{json.dumps(meta.rate_limits, sort_keys=True)}`.")
    if meta.stop_conditions:
        lines.append(f"- Stop conditions: {', '.join(meta.stop_conditions)}.")
    excerpt = _excerpt(safe_abs)
    if excerpt:
        lines.extend(["", "### Source excerpt", "", excerpt])
    return "\n".join(lines).rstrip() + "\n"


def _bulwark_candidate(meta: trims_mod.TrimMeta, safe_bw: str) -> str:
    lines = [
        f"# scope: armor:{meta.armor}",
        f"# source-trim: {meta.trim_id}",
        "# Tempered boundary candidate. Review before broadening.",
    ]
    if meta.posture in {"graybox", "whitebox"}:
        lines.append(f"# posture: {meta.posture}")
    if meta.active_testing:
        lines.append("# active-testing: approval-required")
    excerpt = _excerpt(safe_bw, max_lines=16)
    if excerpt:
        lines.extend(["", excerpt])
    return "\n".join(lines).rstrip() + "\n"


def _trim_template_candidate(meta: trims_mod.TrimMeta) -> str:
    return (
        f"# Trim template candidate from `{meta.trim_id}`\n\n"
        f"- Armor: `{meta.armor}`\n"
        f"- Posture: `{meta.posture}`\n"
        f"- Depth: `{meta.depth}`\n"
        f"- Active testing: `{str(meta.active_testing).lower()}`\n"
        f"- Gateways: `{', '.join(meta.channels) if meta.channels else 'inherit'}`\n"
        f"- TOD windows: `{len(meta.tod_allowed)}`\n"
        f"- Rate limits: `{json.dumps(meta.rate_limits, sort_keys=True)}`\n"
        f"- Stop conditions: `{', '.join(meta.stop_conditions)}`\n"
        "\nKeep concrete targets, secrets, authorization text, and client "
        "identifiers in the task-specific trim that instantiates this template.\n"
    )


def _weapon_doctrine_candidate(
    weapon_id: str,
    crumbs: list[dict],
    shapes: list[dict],
    fact_shapes: list[dict],
    stores: list[dict],
    lines: list[dict],
) -> str:
    lines_out = [
        f"## Tempered forge methodology from weapon `{weapon_id}`",
        "",
        f"- Procedure entries: `{len(crumbs)}`.",
        f"- Concept/rubric entries: `{len(shapes)}`.",
        f"- Fact-shape entries: `{len(fact_shapes)}`.",
        f"- Store/resource entries: `{len(stores)}`.",
        f"- Scope line entries: `{len(lines)}`.",
        "- Promote as reviewable doctrine; do not treat this as applied policy.",
    ]
    samples = []
    samples.extend(_weapon_sample_lines("Procedures", crumbs))
    samples.extend(_weapon_sample_lines("Concepts", shapes))
    samples.extend(_weapon_sample_lines("Fact shapes", fact_shapes))
    samples.extend(_weapon_sample_lines("Stores", stores))
    if samples:
        lines_out.extend(["", "### Representative material", "", *samples])
    return "\n".join(lines_out).rstrip() + "\n"


def _weapon_boundary_candidate(weapon_id: str, line_items: list[dict]) -> str:
    if not line_items:
        return ""
    out = [
        f"# scope: weapon:{weapon_id}",
        "# Tempered boundary candidate from forged weapon scope lines.",
        "# Review before applying to standing armor.",
        "",
    ]
    for item in line_items[:12]:
        cite = _citation_id(item)
        if cite:
            out.append(f"# cite: {cite}")
        in_scope = _as_list(item.get("in_scope") or item.get("scope"))
        out_scope = _as_list(item.get("out_of_scope"))
        statement = str(item.get("statement") or item.get("description") or "").strip()
        if in_scope:
            out.append("in_scope: " + "; ".join(in_scope[:8]))
        if out_scope:
            out.append("out_of_scope: " + "; ".join(out_scope[:8]))
        if statement:
            out.append("note: " + statement)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _weapon_trim_template_candidate(weapon_id: str, weapon: dict) -> str:
    alphabets = weapon.get("alphabets") or {}
    alphabet_names = sorted(alphabets) if isinstance(alphabets, dict) else []
    style = weapon.get("style") or {}
    policy = weapon.get("policy") or {}
    return (
        f"# Trim template candidate from weapon `{weapon_id}`\n\n"
        f"- Weapon target: `{weapon.get('weapon_target_id') or weapon_id}`\n"
        f"- Forge id: `{weapon.get('forge_id') or 'unknown'}`\n"
        f"- Alphabet pack: `{weapon.get('alphabet_pack_name') or 'none'}`\n"
        f"- Alphabets: `{', '.join(alphabet_names) if alphabet_names else 'none'}`\n"
        f"- Policy layers: `{', '.join(sorted(policy)) if isinstance(policy, dict) and policy else 'none'}`\n"
        f"- Style profile: `{json.dumps(style, sort_keys=True) if style else 'none'}`\n"
        "\nUse this as reusable task-fit material. Keep concrete targets, "
        "authorization text, secrets, and client identifiers in the task trim.\n"
    )


def _weapon_sample_lines(label: str, items: list[dict], *, limit: int = 6) -> list[str]:
    if not items:
        return []
    out = [f"#### {label}"]
    for item in items[:limit]:
        name = str(
            item.get("name")
            or item.get("title")
            or item.get("description")
            or item.get("statement")
            or "unnamed"
        ).strip()
        cite = _citation_id(item)
        suffix = f" (`{cite}`)" if cite else ""
        out.append(f"- {name}{suffix}")
    return out + [""]


def _citation_id(item: dict) -> str:
    citation = item.get("citation") if isinstance(item, dict) else None
    if isinstance(citation, dict):
        return str(citation.get("manifest_element_id") or "").strip()
    return ""


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _append_absolute(
    path: Path, proposal: TemperProposal, item: TemperItem,
) -> Path:
    existing = path.read_text() if path.is_file() else ""
    body = temper_controls.render_control(
        item.destination,
        _control_for_render(item),
        citations=item.citations,
        fallback_text=item.proposed_text,
    )
    section = (
        f"\n{_ABS_BEGIN_PREFIX} {proposal.proposal_id} -->\n"
        f"{body.rstrip()}\n"
        f"{_ABS_END}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip() + "\n" + section)
    return path


def _append_bulwark(
    path: Path, proposal: TemperProposal, item: TemperItem,
) -> Path:
    existing = path.read_text() if path.is_file() else ""
    body = temper_controls.render_control(
        item.destination,
        _control_for_render(item),
        citations=item.citations,
        fallback_text=item.proposed_text,
    )
    section = (
        f"\n{_BW_BEGIN_PREFIX} {proposal.target_armor} {proposal.proposal_id}\n"
        f"{body.rstrip()}\n"
        f"{_BW_END}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip() + "\n" + section)
    return path


def _append_rhema(
    path: Path, proposal: TemperProposal, item: TemperItem,
) -> Path:
    existing = path.read_text() if path.is_file() else ""
    body = temper_controls.render_control(
        item.destination,
        _control_for_render(item),
        citations=item.citations,
        fallback_text=item.proposed_text,
    )
    section = (
        f"\n{_RHEMA_BEGIN_PREFIX} {proposal.proposal_id} -->\n"
        f"{body.rstrip()}\n"
        f"{_RHEMA_END}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip() + "\n" + section)
    return path


def _write_control_artifact(
    state_root: Path,
    armor: str,
    proposal: TemperProposal,
    item: TemperItem,
    index: int,
) -> Path:
    root = Path(state_root) / "armors" / armor / "temper" / item.destination
    root.mkdir(parents=True, exist_ok=True)
    slug = _slug(item.title) or f"item-{index}"
    path = root / f"{proposal.proposal_id}-{slug}.json"
    payload = {
        "proposal_id": proposal.proposal_id,
        "destination": item.destination,
        "thread_type": item.thread_type,
        "title": item.title,
        "source": item.source,
        "rationale": item.rationale,
        "risk": item.risk,
        "control": item.control,
        "scope": item.scope,
        "methodology_context": item.methodology_context,
        "safety_constraints": item.safety_constraints,
        "citations": item.citations,
        "rendered": temper_controls.render_control(
            item.destination,
            _control_for_render(item),
            citations=item.citations,
            fallback_text=item.proposed_text,
        ),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _control_for_render(item: TemperItem) -> dict:
    control = dict(item.control or {})
    if item.methodology_context and "methodology_context" not in control:
        control["methodology_context"] = dict(item.methodology_context)
    if item.safety_constraints and "safety_constraints" not in control:
        control["safety_constraints"] = dict(item.safety_constraints)
    return control


def _write_trim_template(
    state_root: Path,
    armor: str,
    proposal: TemperProposal,
    item: TemperItem,
    index: int,
) -> Path:
    root = Path(state_root) / "armors" / armor / "temper" / "trim-templates"
    root.mkdir(parents=True, exist_ok=True)
    slug = _slug(item.title) or f"item-{index}"
    path = root / f"{proposal.proposal_id}-{slug}.md"
    path.write_text(item.proposed_text.rstrip() + "\n")
    return path


def _sanitize_task_specific(text: str, meta: trims_mod.TrimMeta) -> str:
    out = text or ""
    targets = {meta.target}
    for asset in meta.assets:
        if isinstance(asset, dict):
            targets.add(str(asset.get("host") or asset.get("url") or ""))
    out = re.sub(
        r"https?://[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z][A-Za-z0-9-]+(?::\d+)?",
        "[task-target]",
        out,
    )
    out = re.sub(
        r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z0-9-]+(?:\:\d+)?\b",
        "[task-target]",
        out,
    )
    out = re.sub(
        r"\b[A-Za-z0-9-]+(?:\\\.[A-Za-z0-9-]+)*\\\.[A-Za-z][A-Za-z0-9-]+\b",
        "[task-target]",
        out,
    )
    for target in sorted(t for t in targets if t):
        parsed = urlparse(target)
        host = parsed.netloc or parsed.path
        for needle in {target, host, host.split(":")[0]}:
            if needle:
                out = out.replace(needle, "[task-target]")
                out = out.replace(re.escape(needle), "[task-target]")
    out = re.sub(r"https?://\[task-target\]", "[task-target]", out)
    out = re.sub(r"[A-Za-z0-9-]+\.\[task-target\]", "[task-target]", out)
    out = re.sub(
        r"[A-Za-z0-9\\._-]*\[task-target\][A-Za-z0-9\\._-]*",
        "[task-target]",
        out,
    )
    out = re.sub(r"(?i)(token|secret|password)\s*[:=]\s*\S+", r"\1=[redacted]", out)
    return out


def _read_text(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


def _excerpt(text: str, *, max_lines: int = 12) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:max_lines])


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    return slug.strip("-")[:64]


__all__ = [
    "TemperItem",
    "TemperProposal",
    "TemperStateError",
    "activate_temper",
    "active_temper_id",
    "active_temper_path",
    "apply_proposal",
    "create_proposal",
    "create_proposal_from_tailor_session",
    "create_proposal_from_weapon",
    "list_proposals",
    "load_proposal",
    "proposal_path",
    "proposals_dir",
    "reject_proposal",
    "release_temper",
    "seal_proposal",
    "seal_selected_proposal",
]
