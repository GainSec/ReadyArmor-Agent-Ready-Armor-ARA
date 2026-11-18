"""SM-18-style retrievability decay + stabilization on operator action.

R(t) = exp(-elapsed / stability_s)

Operator actions multiply stability:
- investigating ×2.0
- touched ×1.3
- surfaced ×1.0 (auto-resurface; no operator action yet)

Closing actions (promoted/confirmed/dismissed) remove the lead from
scheduler attention; stability becomes irrelevant.
"""
from __future__ import annotations

import math
from dataclasses import replace

from ara.runtime.leads.record import Lead, LeadPriority, ReviewEvent


INITIAL_STABILITY_S: dict[LeadPriority, float] = {
    "critical": 30 * 60,
    "high": 2 * 3600,
    "medium": 8 * 3600,
    "low": 24 * 3600,
}

AGED_CEILING_S: dict[LeadPriority, float] = {
    "critical": 72 * 3600,
    "high": 7 * 24 * 3600,
    "medium": 14 * 24 * 3600,
    "low": 30 * 24 * 3600,
}

STABILIZATION_MULTIPLIER: dict[str, float | None] = {
    "investigating": 2.0,
    "touched": 1.3,
    "surfaced": 1.0,
    "promoted": None,
    "confirmed": None,
    "dismissed": None,
    "archived": None,
}

_CLOSED_STATUS: dict[str, str] = {
    "promoted": "promoted",
    "confirmed": "confirmed",
    "dismissed": "dismissed_operator",
    "archived": "archived",
}


def retrievability(lead: Lead, now: float) -> float:
    if lead.stability_s <= 0:
        return 0.0
    elapsed = max(0.0, now - lead.last_reviewed_at)
    return math.exp(-elapsed / lead.stability_s)


def apply_action(
    lead: Lead, action: str, *,
    now: float, actor: str = "operator",
    note: str | None = None,
    death_signal: str | None = None,
    death_citation: str | None = None,
) -> Lead:
    """Returns updated Lead. ``action`` is from STABILIZATION_MULTIPLIER keys
    or ``auto_dismissed`` (from death detectors).
    """
    if action == "auto_dismissed":
        ev = ReviewEvent(
            ts=now, action="auto_dismissed", actor=actor, note=note,
            stability_before=lead.stability_s, stability_after=lead.stability_s,
        )
        return replace(
            lead,
            status="dismissed_auto",
            death_signal=death_signal,
            death_citation=death_citation,
            closed_at=now, closed_by=actor,
            review_history=lead.review_history + [ev],
        )
    mult = STABILIZATION_MULTIPLIER.get(action)
    if mult is None:
        new_status = _CLOSED_STATUS.get(action, lead.status)
        ev = ReviewEvent(
            ts=now, action=action, actor=actor, note=note,
            stability_before=lead.stability_s, stability_after=lead.stability_s,
        )
        promoted_at = now if action == "promoted" else lead.promoted_at
        return replace(
            lead, status=new_status, closed_at=(now if action != "promoted" else lead.closed_at),
            closed_by=(actor if action != "promoted" else lead.closed_by),
            promoted_at=promoted_at,
            review_history=lead.review_history + [ev],
        )
    new_stability = lead.stability_s * mult
    ev = ReviewEvent(
        ts=now, action=action, actor=actor, note=note,
        stability_before=lead.stability_s, stability_after=new_stability,
    )
    new_status: str = lead.status
    if action == "investigating" and lead.status == "open":
        new_status = "investigating"
    return replace(
        lead, stability_s=new_stability, last_reviewed_at=now,
        status=new_status,
        review_history=lead.review_history + [ev],
    )
