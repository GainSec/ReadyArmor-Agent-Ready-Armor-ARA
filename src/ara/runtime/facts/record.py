"""Fact record + serialization + content-hash dedup.

ID format (per 1.1 confirmation Q1, raised by operator: WSTG-scale
weapons can produce >4,000 facts in a session): 10-hex chars, drawn
from sha256 of (predicate, value, asserted_by_crumb, activation_id,
asserted_at). 16⁻¹⁰ collision-safe to ~10⁵ facts per session.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


FactSource = Literal["agent_assertion", "crumb_emit", "operator_input", "import"]


@dataclass(frozen=True)
class Fact:
    id: str
    predicate: str
    value: Any
    asserted_by_crumb: str
    evidence_refs: list[str]
    chunk_at_time: str
    activation_id: str
    asserted_at: float
    prior_fact: str | None = None
    depends_on: list[str] = field(default_factory=list)
    confidence: float = 1.0
    source: FactSource = "agent_assertion"
    note: str | None = None

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict) -> "Fact":
        return cls(
            id=d["id"],
            predicate=d["predicate"],
            value=d["value"],
            asserted_by_crumb=d["asserted_by_crumb"],
            evidence_refs=list(d.get("evidence_refs") or []),
            chunk_at_time=d["chunk_at_time"],
            activation_id=d["activation_id"],
            asserted_at=d["asserted_at"],
            prior_fact=d.get("prior_fact"),
            depends_on=list(d.get("depends_on") or []),
            confidence=d.get("confidence", 1.0),
            source=d.get("source", "agent_assertion"),
            note=d.get("note"),
        )


def content_hash(
    *,
    predicate: str,
    value: Any,
    asserted_by_crumb: str,
    evidence_refs: list[str],
    activation_id: str,
) -> str:
    """Stable hash for dedup. Same content → same id within a session."""
    blob = json.dumps({
        "predicate": predicate,
        "value": value,
        "asserted_by_crumb": asserted_by_crumb,
        "evidence_refs": sorted(evidence_refs),
        "activation_id": activation_id,
    }, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def mint_id(
    *,
    predicate: str,
    value: Any,
    asserted_by_crumb: str,
    evidence_refs: list[str],
    activation_id: str,
    asserted_at: float,
) -> str:
    """Deterministic 10-hex id (collision-safe at WSTG scale ≥4,000 facts)."""
    salted = json.dumps({
        "ch": content_hash(
            predicate=predicate, value=value,
            asserted_by_crumb=asserted_by_crumb,
            evidence_refs=evidence_refs,
            activation_id=activation_id,
        ),
        "ts": asserted_at,
    }, sort_keys=True).encode("utf-8")
    return "f:" + hashlib.sha256(salted).hexdigest()[:10]
