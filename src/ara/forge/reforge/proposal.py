"""ReforgeProposal record + diff between two weapon drafts.

A reforge takes an existing sealed weapon, optionally adds new mats or
telemetry signals, and produces a *proposal* — a typed delta over the
predecessor weapon. The proposal is operator-reviewed (CLI or armory)
before being sealed as the next weapon version.

v1 supports:
- proposal record schema (write/read)
- weapon-draft diff (added / removed / modified components)
- per-change rationale slot (telemetry citation if recommender-driven)
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ComponentDiff:
    """One bucket-level diff (e.g. crumbs added/removed/modified)."""
    bucket: str            # "crumbs" | "shapes" | "stores" | "lines" | "fact_shapes" | "seed_facts" | "chunks"
    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    modified: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ReforgeProposal:
    id: str
    weapon_predecessor_id: str
    forge_id: str
    created_at: float
    triggered_by: str        # "operator" | "telemetry-recommendation"
    component_diffs: list[ComponentDiff] = field(default_factory=list)
    rubric_changes: dict | None = None
    synonym_changes: dict | None = None
    new_calibration_queries: list[dict] = field(default_factory=list)
    rationales: dict[str, str] = field(default_factory=dict)
    applied_at: float | None = None
    approved_by: str | None = None

    def to_jsonable(self) -> dict:
        return {
            "id": self.id,
            "weapon_predecessor_id": self.weapon_predecessor_id,
            "forge_id": self.forge_id,
            "created_at": self.created_at,
            "triggered_by": self.triggered_by,
            "component_diffs": [asdict(d) for d in self.component_diffs],
            "rubric_changes": self.rubric_changes,
            "synonym_changes": self.synonym_changes,
            "new_calibration_queries": list(self.new_calibration_queries),
            "rationales": dict(self.rationales),
            "applied_at": self.applied_at,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_jsonable(cls, d: dict) -> "ReforgeProposal":
        return cls(
            id=d["id"],
            weapon_predecessor_id=d["weapon_predecessor_id"],
            forge_id=d["forge_id"],
            created_at=d["created_at"],
            triggered_by=d["triggered_by"],
            component_diffs=[
                ComponentDiff(**cd) for cd in (d.get("component_diffs") or [])
            ],
            rubric_changes=d.get("rubric_changes"),
            synonym_changes=d.get("synonym_changes"),
            new_calibration_queries=list(d.get("new_calibration_queries") or []),
            rationales=dict(d.get("rationales") or {}),
            applied_at=d.get("applied_at"),
            approved_by=d.get("approved_by"),
        )


def mint_proposal_id() -> str:
    return "rp_" + uuid.uuid4().hex[:10]


_BUCKETS = ("crumbs", "shapes", "stores", "lines", "fact_shapes",
            "seed_facts", "chunks")


def _component_id(component: dict, bucket: str) -> str:
    """Best-effort id derivation: prefer explicit *_id keys; fall back
    to ``citation.manifest_element_id`` + ``extraction_id`` so identical
    bytes match.
    """
    for key in (f"{bucket[:-1]}_id", "id", "name"):
        if component.get(key):
            return f"{bucket}:{component[key]}"
    cite = (component.get("citation") or {}).get("manifest_element_id")
    if cite:
        return f"{bucket}:cite:{cite}:{component.get('extraction_id', '')}"
    # Hash fallback
    import hashlib
    h = hashlib.sha256(json.dumps(component, sort_keys=True, default=str)
                        .encode()).hexdigest()
    return f"{bucket}:{h[:10]}"


def diff_drafts(prior: dict, proposed: dict) -> list[ComponentDiff]:
    """Compute per-bucket added/removed/modified lists between two
    weapon-draft / sealed-weapon JSON dicts.
    """
    out: list[ComponentDiff] = []
    for bucket in _BUCKETS:
        prior_items = list(prior.get(bucket) or [])
        proposed_items = list(proposed.get(bucket) or [])
        prior_by_id = {_component_id(c, bucket): c for c in prior_items}
        proposed_by_id = {_component_id(c, bucket): c for c in proposed_items}
        added: list[dict] = []
        removed: list[dict] = []
        modified: list[dict] = []
        for cid, c in proposed_by_id.items():
            if cid not in prior_by_id:
                added.append(c)
            elif json.dumps(prior_by_id[cid], sort_keys=True, default=str) \
                    != json.dumps(c, sort_keys=True, default=str):
                modified.append({
                    "id": cid,
                    "before": prior_by_id[cid],
                    "after": c,
                })
        for cid, c in prior_by_id.items():
            if cid not in proposed_by_id:
                removed.append(c)
        if added or removed or modified:
            out.append(ComponentDiff(
                bucket=bucket, added=added, removed=removed, modified=modified,
            ))
    return out


def build_proposal(
    *,
    weapon_predecessor_id: str,
    forge_id: str,
    prior_weapon_json: dict,
    proposed_weapon_json: dict,
    triggered_by: str = "operator",
    rationales: dict[str, str] | None = None,
    synonym_changes: dict | None = None,
    rubric_changes: dict | None = None,
    new_calibration_queries: list[dict] | None = None,
) -> ReforgeProposal:
    diffs = diff_drafts(prior_weapon_json, proposed_weapon_json)
    return ReforgeProposal(
        id=mint_proposal_id(),
        weapon_predecessor_id=weapon_predecessor_id,
        forge_id=forge_id,
        created_at=time.time(),
        triggered_by=triggered_by,
        component_diffs=diffs,
        rubric_changes=rubric_changes,
        synonym_changes=synonym_changes,
        new_calibration_queries=list(new_calibration_queries or []),
        rationales=dict(rationales or {}),
    )


def write_proposal(path: Path, proposal: ReforgeProposal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proposal.to_jsonable(), sort_keys=True,
                              indent=2, default=str))
    os.replace(tmp, path)


def read_proposal(path: Path) -> ReforgeProposal:
    return ReforgeProposal.from_jsonable(json.loads(path.read_text()))


def summarize(proposal: ReforgeProposal) -> dict[str, Any]:
    """Counts only — for CLI ``ara forge reforge status``."""
    out: dict[str, Any] = {
        "id": proposal.id,
        "weapon_predecessor_id": proposal.weapon_predecessor_id,
        "forge_id": proposal.forge_id,
        "triggered_by": proposal.triggered_by,
        "applied_at": proposal.applied_at,
    }
    for d in proposal.component_diffs:
        out[d.bucket] = {
            "added": len(d.added), "removed": len(d.removed),
            "modified": len(d.modified),
        }
    if proposal.rubric_changes:
        out["rubric_changes"] = True
    if proposal.synonym_changes:
        out["synonym_changes"] = True
    if proposal.new_calibration_queries:
        out["new_calibration_queries"] = len(proposal.new_calibration_queries)
    return out
