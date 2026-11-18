"""Crumb-intent channel — knowledge-gap precondition + active-crumb gate.

Per spec armor-trim-v0.1-amendment §8 (Q3 resolution). The agent calls
this channel BEFORE firing tools for a procedure. The channel does
two things in one round-trip:

  1. **Knowledge-gap check** — consults the engagement fact store. If
     facts of the crumb's ``produces`` shapes are already populated
     (typically from mats parsed at upload, or from prior crumbs),
     returns ``skip`` with ``evidence: from-mats``. Otherwise ``fire``.

  2. **Active-crumb activation** — when the decision is ``fire``, also
     records the crumb_id on the engagement's CrumbTracker so the
     supervisor's anomaly autofirer + divergence detection knows what
     procedure the agent is in. (Previously the agent had no channel
     to declare active crumb; the tracker only ever cleared on chunk
     swap. With this gate, the agent's "I'm running this procedure
     now" intent is mechanically recorded.)

Operator override: one-time ``RUN-RECON-DESPITE-KNOWLEDGE:<crumb_id>``
token; consumed to force fire even when coverage is satisfied (e.g.
operator wants a verification re-recon).

Wiring:
  1. Bootstrap installs the channel with ``fact_store`` (per-engagement
     FactStore adapter) + ``crumb_tracker`` + ``state_root``.
  2. Auto-brief (forge_brief) instructs the agent: "before firing
     tools for any procedure, call ``channel.crumb.intent({crumb_id,
     produces})``. If decision=skip, move on; the runtime has logged
     the skip with from-mats evidence."
  3. Pack-side: weapon procedure templates render an inline
     ``crumb.intent`` call as the first step of every PROC crumb.
     Without this, the agent has to remember to call it; with it,
     the call is part of the procedure's own protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection
from ara.runtime.knowledge_gap import (
    GapResult,
    has_sufficient_coverage,
    override_token_for,
    parse_produces,
)


@dataclass
class CrumbIntentBody:
    """Channel body schema. Forge-side runtime fills these in."""
    crumb_id: str
    produces: list                                         # str or {shape, min_count}
    operator_id: str = ""


class CrumbIntentChannel(Channel):
    """Pre-fire knowledge-gap consult + active-crumb activation.

    Returns one of:
      ``{"decision": "fire", "reason": ...}`` — proceed; CrumbTracker
        now reports this crumb_id as active.
      ``{"decision": "skip", "reason": ..., "evidence": "from-mats", ...}`` —
        skip; CrumbTracker untouched.
      ``{"decision": "fire", "reason": "operator override", "token_consumed": "..."}`` —
        operator forced fire via RUN-RECON-DESPITE-KNOWLEDGE token;
        CrumbTracker activated.
    """
    name = "crumb.intent"
    schema = CrumbIntentBody
    sink_subsystem = "chunks.jsonl"

    def __init__(
        self, *, fact_store, state_root: Path,
        crumb_tracker=None,
    ) -> None:
        self.fact_store = fact_store
        self.state_root = Path(state_root)
        self.crumb_tracker = crumb_tracker

    def _activate(self, crumb_id: str) -> None:
        """Record this crumb as the active one. Best-effort — a missing
        tracker is fine (the channel still functions as a knowledge-gap
        check; some tests don't bootstrap one)."""
        if self.crumb_tracker is None:
            return
        try:
            self.crumb_tracker.set_active(crumb_id)
        except Exception:
            pass

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        crumb_id = str(body.get("crumb_id") or "").strip()
        produces_raw = body.get("produces") or []
        if not crumb_id:
            raise ChannelRejection("crumb_id required")

        # Operator override path — consume the per-crumb token if set.
        # Lazy import keeps tokens out of the hot path.
        try:
            from ara import tokens as _tokens
            tname = override_token_for(crumb_id)
            if _tokens.has_token(tname, state_root=self.state_root):
                _tokens.require_token(tname, state_root=self.state_root)
                self._activate(crumb_id)
                return {
                    "decision": "fire",
                    "reason": "operator forced fire via override token",
                    "token_consumed": tname,
                    "crumb_id": crumb_id,
                }
        except Exception:
            pass

        reqs = parse_produces(produces_raw)
        gap: GapResult = has_sufficient_coverage(reqs, self.fact_store)
        if gap.skip:
            # Knowledge gap closed → no activation. The agent should
            # NOT fire this crumb's tools.
            return {
                "decision": "skip",
                "reason": "knowledge gap closed by mats / prior crumbs",
                "evidence": "from-mats",
                "matched_shapes": list(gap.matched),
                "crumb_id": crumb_id,
            }
        # Fire path: activate + return.
        self._activate(crumb_id)
        return {
            "decision": "fire",
            "reason": gap.evidence,
            "matched_shapes": list(gap.matched),
            "missing_shapes": [r.shape for r in gap.missing],
            "crumb_id": crumb_id,
        }


class FactStorePredicateAdapter:
    """Wrap a runtime FactStore so it satisfies the
    ``knowledge_gap.FactStore`` protocol — i.e. exposes
    ``count(shape: str) -> int``.

    In ARA's fact model, a "shape" maps to a predicate (e.g.
    ``subdomain.host``). ``FactStore.by_predicate(shape, exact=True)``
    returns every fact with that exact predicate; the count of those
    facts is the coverage signal knowledge_gap consumes.
    """
    def __init__(self, fact_store) -> None:
        self.fact_store = fact_store

    def count(self, shape: str) -> int:
        try:
            return len(self.fact_store.by_predicate(shape, exact=True))
        except Exception:
            return 0


__all__ = [
    "CrumbIntentBody", "CrumbIntentChannel", "FactStorePredicateAdapter",
]
