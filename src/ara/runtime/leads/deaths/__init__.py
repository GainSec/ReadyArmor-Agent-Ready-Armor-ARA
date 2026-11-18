"""Death detectors. Each is ``(lead, ctx) → DeathVerdict | None``."""
from __future__ import annotations

import time
from dataclasses import dataclass

from ara.runtime.facts.store import FactStore
from ara.runtime.leads.record import Lead


@dataclass(frozen=True)
class DeathVerdict:
    death_signal: str
    death_citation: str | None
    note: str | None = None


def aged_past_ceiling(lead: Lead, *, now: float | None = None) -> DeathVerdict | None:
    """7.4: archive on TTL expiry. ``death_signal`` is special — caller
    should set status to ``archived`` rather than ``dismissed_auto``.
    """
    if lead.status not in ("open", "investigating"):
        return None
    n = now or time.time()
    if n > lead.aged_ceiling_at:
        return DeathVerdict(
            death_signal="aged_past_ceiling",
            death_citation=None,
            note=f"exceeded TTL by {int(n - lead.aged_ceiling_at)}s",
        )
    return None


def contradicted_by_fact(
    lead: Lead, *, fact_store: FactStore,
) -> DeathVerdict | None:
    """7.2: any negation_predicate matched in the fact store."""
    if lead.status not in ("open", "investigating"):
        return None
    if not lead.negation_predicates:
        return None
    for neg in lead.negation_predicates:
        contradict_set = set(neg.contradicts_when_value_in)
        for fact in fact_store.by_predicate(neg.predicate, exact=True):
            if fact.value in contradict_set:
                return DeathVerdict(
                    death_signal="contradicted_by_fact",
                    death_citation=fact.id,
                    note=f"{neg.predicate}={fact.value!r} contradicts lead",
                )
    return None


def engagement_closed(lead: Lead) -> DeathVerdict | None:
    """7.5: at engagement close, all open leads → archived."""
    if lead.status not in ("open", "investigating"):
        return None
    return DeathVerdict(
        death_signal="engagement_closed",
        death_citation=None,
        note="engagement closed",
    )


def superseded_by(lead: Lead, *, supersedes_by_id: str | None) -> DeathVerdict | None:
    """7.3: operator-driven; ``supersedes_by_id`` from a dismiss reason."""
    if not supersedes_by_id:
        return None
    return DeathVerdict(
        death_signal="superseded_by",
        death_citation=supersedes_by_id,
        note=f"superseded by {supersedes_by_id}",
    )
