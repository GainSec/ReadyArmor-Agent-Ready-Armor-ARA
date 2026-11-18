"""Closure gate: prevents engagement-close while critical/high leads stale.

A lead blocks closure if:
- canonical priority ∈ {critical, high}, AND
- ``not_mechanically_testable`` is False, AND
- retrievability(now) < 0.5
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ara.runtime.leads.decay import retrievability
from ara.runtime.leads.store import LeadStore


@dataclass(frozen=True)
class ClosureBlocker:
    lead_id: str
    title: str
    priority: str
    R_now: float
    reason: str


def check_closure(store: LeadStore, *, now: float | None = None) -> list[ClosureBlocker]:
    n = now or time.time()
    blockers: list[ClosureBlocker] = []
    for lead in store.open_leads():
        if lead.not_mechanically_testable:
            continue
        canon = lead.canonical_priority()
        if canon not in ("critical", "high"):
            continue
        R = retrievability(lead, n)
        if R >= 0.5:
            continue
        blockers.append(ClosureBlocker(
            lead_id=lead.id, title=lead.title, priority=canon,
            R_now=R,
            reason=f"open {canon} lead, retrievability={R:.2f}",
        ))
    return blockers
