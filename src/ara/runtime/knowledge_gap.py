"""Knowledge-gap recon precondition.

Per spec ``armor-trim-v0.1-amendment.md`` §8 (Q3 resolution). Replaces
strict depth-tier gating with: provided mats → parsed into facts →
recon-class crumb preconditions consult the fact store before firing.

Strategy:

1. Each recon-class crumb declares ``produces: [<fact_shape>, ...]`` in
   its forge metadata.
2. Pre-fire check: ``has_sufficient_coverage(produces, fact_store)``
   → if all required shapes already populated above the configured
   minimum count, the crumb is **skipped** with an ``evidence:
   from-mats`` record.
3. Operator override: one-time
   ``RUN-RECON-DESPITE-KNOWLEDGE:<crumb_id>`` token.

This module is the **pure decision logic**. The actual integration
into the forge crumb runner is a follow-up; this gives the substrate
+ tests so the runner can call into a stable surface.

Fact-store interface is duck-typed: anything that exposes
``count(shape: str) -> int`` works. We don't import the runtime
fact channel here so this stays a pure module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class FactStore(Protocol):
    """Minimum fact-store contract this module needs."""
    def count(self, shape: str) -> int: ...    # noqa: E704


# ---------------------------------------------------------------------------
# Coverage decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageRequirement:
    """One fact-shape the crumb needs facts about. ``min_count`` of
    facts of this shape must already be populated for the crumb to
    skip — default 1 (any prior knowledge counts)."""
    shape: str
    min_count: int = 1


@dataclass
class GapResult:
    skip: bool
    matched: list[str] = field(default_factory=list)         # shapes already covered
    missing: list[CoverageRequirement] = field(default_factory=list)
    evidence: str = ""                                        # for skip-record


def has_sufficient_coverage(
    requirements: list[CoverageRequirement],
    store: FactStore,
) -> GapResult:
    """Pure decision. ``skip=True`` only when **every** requirement is
    satisfied — partial coverage means the crumb still fires (we don't
    silently skip work that the engagement intake would have expected
    to be done)."""
    if not requirements:
        return GapResult(skip=False, evidence="no requirements declared")
    matched: list[str] = []
    missing: list[CoverageRequirement] = []
    for req in requirements:
        try:
            n = int(store.count(req.shape))
        except Exception:
            n = 0
        if n >= req.min_count:
            matched.append(req.shape)
        else:
            missing.append(req)
    if missing:
        return GapResult(
            skip=False, matched=matched, missing=missing,
            evidence=(
                f"{len(matched)} of {len(requirements)} shapes already covered; "
                f"{len(missing)} missing — running"
            ),
        )
    return GapResult(
        skip=True, matched=matched, missing=[],
        evidence=(
            f"all {len(requirements)} shapes already covered from mats "
            "or prior crumbs"
        ),
    )


# ---------------------------------------------------------------------------
# Crumb metadata helper
# ---------------------------------------------------------------------------

def parse_produces(produces_raw: list) -> list[CoverageRequirement]:
    """Accept the ``produces`` field as either:

    - ``["subdomain.host", "endpoint.url"]``  (strings → min_count=1)
    - ``[{"shape": "subdomain.host", "min_count": 5}]``

    Returns a list of ``CoverageRequirement``.
    """
    out: list[CoverageRequirement] = []
    for entry in produces_raw or []:
        if isinstance(entry, str):
            if entry.strip():
                out.append(CoverageRequirement(shape=entry.strip()))
        elif isinstance(entry, dict):
            shape = str(entry.get("shape") or "").strip()
            if not shape:
                continue
            try:
                n = int(entry.get("min_count") or 1)
            except (TypeError, ValueError):
                n = 1
            out.append(CoverageRequirement(shape=shape, min_count=max(1, n)))
    return out


# ---------------------------------------------------------------------------
# Override-token name helper
# ---------------------------------------------------------------------------

def override_token_for(crumb_id: str) -> str:
    """Per-crumb override token (operator can re-recon despite mats)."""
    return f"RUN-RECON-DESPITE-KNOWLEDGE:{crumb_id}"


__all__ = [
    "CoverageRequirement", "FactStore", "GapResult",
    "has_sufficient_coverage", "override_token_for", "parse_produces",
]
