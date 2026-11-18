"""Computed lead priority — pure function of structured signals (no LLM).

In v1 there is no full WeaponConfig yet. The operator/agent supplies
the per-signal weights either:
  a) directly in the lead body (``value_tier``,
     ``scope_weight``, ``derived_from_crumb``), OR
  b) defaulted by this module (medium tier, full scope).

When 1.4/forge plumbing lands, those defaults are replaced by lookups
into the sealed weapon's CONC + LINES layers.

The signal name ``value_tier`` is generic ("how high-value is the
area this lead covers, on a 0..1 scale"). The pre-rename name
``attack_surface_tier`` is accepted as a body alias for back-compat.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.leads.record import LeadPriority, PrioritySignals


@dataclass(frozen=True)
class PriorityWeights:
    evidence_strength: float = 0.30
    value_tier: float = 0.30
    crumb_provenance_score: float = 0.20
    scope_weight: float = 0.15
    prior_finding_yield: float = 0.05

    @property
    def attack_surface_tier(self) -> float:
        """Back-compat alias for the pre-rename signal name."""
        return self.value_tier


_DEFAULT_TIER = 0.4
_DEFAULT_SCOPE = 0.7


def _evidence_diversity(refs: list[str], binder: EvidenceBinder | None) -> float:
    if not refs or binder is None:
        return 0.0
    sources: set[str] = set()
    for ev in refs:
        rec = binder.get_record(ev)
        if rec is not None:
            sources.add(rec.source)
    if not sources:
        return 0.0
    # 1 source → 0.5; 2 → 0.75; 3+ → 1.0
    return min(1.0, 0.5 + 0.25 * (len(sources) - 1))


def compute_priority(
    body: dict,
    *,
    binder: EvidenceBinder | None = None,
    weights: PriorityWeights | None = None,
    cross_engagement_yield: dict[str, float] | None = None,
) -> tuple[LeadPriority, PrioritySignals]:
    weights = weights or PriorityWeights()
    n = len(body.get("evidence_refs") or [])
    diversity = _evidence_diversity(body.get("evidence_refs") or [], binder)
    evidence_strength = min(1.0, 0.4 * math.log(n + 1) + 0.6 * diversity)

    # value_tier is the canonical name; attack_surface_tier accepted
    # as a back-compat alias so existing callers keep working.
    tier_raw = body.get("value_tier", body.get("attack_surface_tier", _DEFAULT_TIER))
    tier = float(tier_raw)
    if not (0.0 <= tier <= 1.0):
        tier = _DEFAULT_TIER

    if body.get("derived_from_crumb"):
        crumb_score = 1.0
    elif body.get("origin") == "anomaly_promoted":
        crumb_score = 0.7
    elif body.get("origin") == "system_repair":
        crumb_score = 0.2
    else:
        crumb_score = 0.4

    scope = float(body.get("scope_weight", _DEFAULT_SCOPE))
    if not (0.0 <= scope <= 1.0):
        scope = _DEFAULT_SCOPE

    if cross_engagement_yield is not None:
        prior_yield = float(cross_engagement_yield.get(
            body.get("derived_from_crumb") or "_none", 0.0))
    else:
        prior_yield = 0.0

    aggregate = (
        weights.evidence_strength * evidence_strength
        + weights.value_tier * tier
        + weights.crumb_provenance_score * crumb_score
        + weights.scope_weight * scope
        + weights.prior_finding_yield * prior_yield
    )

    if aggregate >= 0.80:
        priority: LeadPriority = "critical"
    elif aggregate >= 0.60:
        priority = "high"
    elif aggregate >= 0.40:
        priority = "medium"
    else:
        priority = "low"

    signals = PrioritySignals(
        evidence_strength=evidence_strength,
        value_tier=tier,
        crumb_provenance_score=crumb_score,
        scope_weight=scope,
        prior_finding_yield=prior_yield,
        aggregate=aggregate,
    )
    return priority, signals
