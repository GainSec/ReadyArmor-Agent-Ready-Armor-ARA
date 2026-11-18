"""Lead record + supporting dataclasses.

ID format (10-hex, same scale-rationale as facts).
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


LeadStatus = Literal[
    "open", "investigating", "promoted", "confirmed",
    "dismissed_operator", "dismissed_auto", "superseded", "archived",
]
LeadOrigin = Literal[
    "agent_hypothesis", "anomaly_promoted", "operator_input",
    "operator_seeded_agent_enriched", "system_repair",
]
LeadPriority = Literal["critical", "high", "medium", "low"]


@dataclass(frozen=True)
class PrioritySignals:
    evidence_strength: float
    value_tier: float
    crumb_provenance_score: float
    scope_weight: float
    prior_finding_yield: float
    aggregate: float

    # Backward-compat alias — accepts the old offsec-flavored name.
    @property
    def attack_surface_tier(self) -> float:
        return self.value_tier


@dataclass(frozen=True)
class ReviewEvent:
    ts: float
    action: str
    actor: str
    note: str | None
    stability_before: float
    stability_after: float


@dataclass(frozen=True)
class NegationPredicate:
    predicate: str
    contradicts_when_value_in: list[Any]


@dataclass(frozen=True)
class FieldEnrichment:
    field: str
    value: Any
    actor: str               # "operator" | "agent"
    citation: dict | None    # {"manifest_element_id": ..., "evidence_ref": ..., ...}
    enriched_at: float


@dataclass(frozen=True)
class Lead:
    id: str
    title: str
    reasoning: str
    proposed_test: str | None
    not_mechanically_testable: bool
    evidence_refs: list[str]
    fact_refs: list[str]
    activation_id: str
    origin: LeadOrigin
    enrichments: list[FieldEnrichment]
    priority_computed: LeadPriority
    priority_signals: PrioritySignals
    priority_agent_proposed: str | None
    priority_operator_override: str | None
    stability_s: float
    last_reviewed_at: float
    review_history: list[ReviewEvent]
    aged_ceiling_at: float
    status: LeadStatus
    death_signal: str | None
    death_citation: str | None
    created_at: float
    closed_at: float | None
    closed_by: str | None
    derived_from_crumb: str | None = None
    affected_assets: list[str] = field(default_factory=list)
    negation_predicates: list[NegationPredicate] = field(default_factory=list)
    promoted_at: float | None = None
    notes: str | None = None

    def canonical_priority(self) -> LeadPriority:
        return self.priority_operator_override or self.priority_computed  # type: ignore[return-value]

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict) -> "Lead":
        return cls(
            id=d["id"], title=d["title"], reasoning=d["reasoning"],
            proposed_test=d.get("proposed_test"),
            not_mechanically_testable=d["not_mechanically_testable"],
            evidence_refs=list(d.get("evidence_refs") or []),
            fact_refs=list(d.get("fact_refs") or []),
            activation_id=d["activation_id"],
            origin=d["origin"],
            enrichments=[FieldEnrichment(**e) for e in d.get("enrichments") or []],
            priority_computed=d["priority_computed"],
            priority_signals=PrioritySignals(**d["priority_signals"]),
            priority_agent_proposed=d.get("priority_agent_proposed"),
            priority_operator_override=d.get("priority_operator_override"),
            stability_s=d["stability_s"],
            last_reviewed_at=d["last_reviewed_at"],
            review_history=[ReviewEvent(**r) for r in d.get("review_history") or []],
            aged_ceiling_at=d["aged_ceiling_at"],
            status=d["status"],
            death_signal=d.get("death_signal"),
            death_citation=d.get("death_citation"),
            created_at=d["created_at"],
            closed_at=d.get("closed_at"),
            closed_by=d.get("closed_by"),
            derived_from_crumb=d.get("derived_from_crumb"),
            affected_assets=list(d.get("affected_assets") or []),
            negation_predicates=[NegationPredicate(**n) for n in d.get("negation_predicates") or []],
            promoted_at=d.get("promoted_at"),
            notes=d.get("notes"),
        )


def mint_lead_id() -> str:
    h = hashlib.sha256(uuid.uuid4().bytes + str(time.time()).encode()).hexdigest()
    return "L:" + h[:10]
