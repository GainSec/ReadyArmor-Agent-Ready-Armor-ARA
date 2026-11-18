"""NeighborIndex — built lazily on read, not persisted.

Five categories per fact (per skeleton §3.2):
- causal:        same asserted_by_crumb
- temporal_*:    asserted_at within window
- evidential:    sharing any evidence_ref
- semantic_up:   center.depends_on (transitive, capped depth)
- semantic_down: facts that depend on center (transitive, capped depth)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ara.runtime.facts.record import Fact
from ara.runtime.facts.store import FactStore


@dataclass(frozen=True)
class FactNeighborhood:
    center: str
    causal: list[str] = field(default_factory=list)
    temporal_before: list[str] = field(default_factory=list)
    temporal_after: list[str] = field(default_factory=list)
    evidential: list[str] = field(default_factory=list)
    semantic_upstream: list[str] = field(default_factory=list)
    semantic_downstream: list[str] = field(default_factory=list)


class NeighborIndex:
    def __init__(self, store: FactStore) -> None:
        self.store = store

    def for_fact(
        self,
        fact_id: str,
        *,
        temporal_window_s: float = 60.0,
        semantic_depth: int = 2,
    ) -> FactNeighborhood:
        all_facts = self.store.all()
        center = next((f for f in all_facts if f.id == fact_id), None)
        if center is None:
            raise KeyError(f"unknown fact id: {fact_id}")

        causal = [
            f.id for f in all_facts
            if f.id != center.id and f.asserted_by_crumb == center.asserted_by_crumb
        ]
        before: list[str] = []
        after: list[str] = []
        for f in all_facts:
            if f.id == center.id:
                continue
            dt = f.asserted_at - center.asserted_at
            if -temporal_window_s <= dt < 0:
                before.append(f.id)
            elif 0 < dt <= temporal_window_s:
                after.append(f.id)
        before.sort(key=lambda fid: -(_at(all_facts, fid) - center.asserted_at))
        after.sort(key=lambda fid: _at(all_facts, fid) - center.asserted_at)

        evidential: list[str] = []
        center_ev = set(center.evidence_refs)
        if center_ev:
            for f in all_facts:
                if f.id == center.id:
                    continue
                if center_ev & set(f.evidence_refs):
                    evidential.append(f.id)

        upstream = self._walk_dependencies(all_facts, center, semantic_depth, "up")
        downstream = self._walk_dependencies(all_facts, center, semantic_depth, "down")

        return FactNeighborhood(
            center=center.id,
            causal=causal,
            temporal_before=before,
            temporal_after=after,
            evidential=evidential,
            semantic_upstream=upstream,
            semantic_downstream=downstream,
        )

    @staticmethod
    def _walk_dependencies(
        all_facts: list[Fact], center: Fact, depth: int, direction: str,
    ) -> list[str]:
        by_id = {f.id: f for f in all_facts}
        # Reverse adjacency for downstream walk
        depended_on_by: dict[str, list[str]] = {}
        for f in all_facts:
            for dep in f.depends_on:
                depended_on_by.setdefault(dep, []).append(f.id)

        visited: set[str] = set()
        order: list[str] = []
        frontier: list[str] = [center.id]
        for _ in range(max(0, depth)):
            next_frontier: list[str] = []
            for fid in frontier:
                f = by_id.get(fid)
                if f is None:
                    continue
                neighbors = (
                    f.depends_on if direction == "up"
                    else depended_on_by.get(fid, [])
                )
                for n in neighbors:
                    if n in visited or n == center.id:
                        continue
                    visited.add(n)
                    order.append(n)
                    next_frontier.append(n)
            frontier = next_frontier
        return order


def _at(all_facts: list[Fact], fact_id: str) -> float:
    for f in all_facts:
        if f.id == fact_id:
            return f.asserted_at
    return 0.0
