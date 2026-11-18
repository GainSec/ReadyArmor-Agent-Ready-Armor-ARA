"""Finding record + supporting dataclasses."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


FindingOrigin = Literal[
    "mat_derived", "agent_derived", "operator_input",
    "operator_seeded_agent_enriched",
]
FindingStatus = Literal[
    "draft", "operator_review", "signed_off", "rescinded", "auto_dismissed",
]
# Verification verdict — set by the finding-check phase (agent-judge or the
# mechanical checkpoint). ``confirmed`` findings sit in the main list; the rest
# are surfaced to the operator in a separate "Not Confirmed" section, where the
# human can promote them back to a real finding. None = not yet judged.
FindingDisposition = Literal[
    "confirmed", "false_positive", "non_exploitable", "non_reproducible", "unsure",
]
NOT_CONFIRMED_DISPOSITIONS = frozenset(
    {"false_positive", "non_exploitable", "non_reproducible", "unsure"}
)
SeverityName = Literal["info", "low", "medium", "high", "critical"]
EnrichmentStatus = Literal["pending", "approved", "denied", "follow_up"]


@dataclass(frozen=True)
class FieldEnrichment:
    field: str
    proposed_value: Any
    rationale: str
    proposed_at: float
    status: EnrichmentStatus = "pending"
    reviewed_at: float | None = None
    reviewed_by: str | None = None
    review_note: str | None = None


@dataclass(frozen=True)
class FixCustom:
    what_changed: str
    why_it_fixes: str
    verification_method: str
    anti_patterns: str | None = None


@dataclass(frozen=True)
class Finding:
    id: str
    title: str
    category: str
    severity_cell: tuple[str, str]
    severity_score: float
    severity_name: SeverityName
    fix_template_id: str | None
    fix_custom: dict | None
    evidence_refs: list[str]
    fact_refs: list[str]
    lead_refs: list[str]
    provenance_chain: list[str]
    impact: dict
    affected_assets: list[str]
    origin: FindingOrigin
    status: FindingStatus
    activation_id: str
    created_at: float
    enrichments: list[FieldEnrichment] = field(default_factory=list)
    signed_off_at: float | None = None
    signed_off_by: str | None = None
    rescinded_at: float | None = None
    rescinded_by: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    content_hash: str | None = None
    category_proposed: str | None = None
    notes: str | None = None
    # CLAIM_VERIFICATION provenance: the markers the agent asserted its cited
    # evidence demonstrates (or the honest not-testable exemption). Persisted so
    # the periodic verification checkpoint can re-run the exact marker check
    # against the still-retrievable evidence, not just confirm retrievability.
    verification: dict | None = None
    not_mechanically_testable: bool = False
    # Verification verdict from the finding-check phase. None until judged;
    # then confirmed | false_positive | non_exploitable | non_reproducible |
    # unsure. non-confirmed dispositions are shown to the operator (not hidden)
    # in a dedicated section for manual review/promotion.
    disposition: str | None = None
    disposition_reason: str | None = None
    disposition_by: str | None = None      # agent_judge | mechanical | operator
    disposition_at: float | None = None

    def to_jsonable(self) -> dict:
        d = asdict(self)
        d["severity_cell"] = list(self.severity_cell)
        return d

    @classmethod
    def from_jsonable(cls, d: dict) -> "Finding":
        cell = d.get("severity_cell")
        if isinstance(cell, list):
            cell = tuple(cell)
        return cls(
            id=d["id"], title=d["title"], category=d["category"],
            severity_cell=cell,  # type: ignore[arg-type]
            severity_score=d["severity_score"],
            severity_name=d["severity_name"],
            fix_template_id=d.get("fix_template_id"),
            fix_custom=d.get("fix_custom"),
            evidence_refs=list(d.get("evidence_refs") or []),
            fact_refs=list(d.get("fact_refs") or []),
            lead_refs=list(d.get("lead_refs") or []),
            provenance_chain=list(d.get("provenance_chain") or []),
            impact=dict(d.get("impact") or {}),
            affected_assets=list(d.get("affected_assets") or []),
            origin=d["origin"],
            enrichments=[FieldEnrichment(**e) for e in d.get("enrichments") or []],
            status=d["status"],
            activation_id=d["activation_id"],
            created_at=d["created_at"],
            signed_off_at=d.get("signed_off_at"),
            signed_off_by=d.get("signed_off_by"),
            rescinded_at=d.get("rescinded_at"),
            rescinded_by=d.get("rescinded_by"),
            supersedes=d.get("supersedes"),
            superseded_by=d.get("superseded_by"),
            content_hash=d.get("content_hash"),
            category_proposed=d.get("category_proposed"),
            notes=d.get("notes"),
            verification=d.get("verification"),
            not_mechanically_testable=bool(d.get("not_mechanically_testable", False)),
            disposition=d.get("disposition"),
            disposition_reason=d.get("disposition_reason"),
            disposition_by=d.get("disposition_by"),
            disposition_at=d.get("disposition_at"),
        )


def mint_finding_id() -> str:
    h = hashlib.sha256(uuid.uuid4().bytes + str(time.time()).encode()).hexdigest()
    return "F:" + h[:10]


def severity_name_from_score(score: float) -> SeverityName:
    if score < 1.0:
        return "info"
    if score < 4.0:
        return "low"
    if score < 7.0:
        return "medium"
    if score < 9.0:
        return "high"
    return "critical"


def compute_content_hash(f: Finding) -> str:
    canon = json.dumps(f.to_jsonable(), sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(canon).hexdigest()
