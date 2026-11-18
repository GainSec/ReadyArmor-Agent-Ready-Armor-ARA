"""Typed channels for the facts subsystem.

- ``fact.assert``    — write side; validates evidence + dependencies
- ``facts.query``    — read side; predicate prefix or exact
- ``facts.history``  — read side; time-ordered for one predicate
- ``facts.neighbors``— read side; semantic + temporal + causal + evidential

Channels register themselves on import.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection
from ara.runtime.facts.neighbors import NeighborIndex
from ara.runtime.facts.record import Fact, mint_id
from ara.runtime.facts.store import FactStore


# ---------- bodies ----------

@dataclass
class FactAssertBody:
    predicate: str
    value: Any
    asserted_by_crumb: str
    evidence_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    confidence: float = 1.0
    source: str = "agent_assertion"
    note: str | None = None
    prior_fact: str | None = None


@dataclass
class FactQueryBody:
    predicate: str
    exact: bool = False


@dataclass
class FactHistoryBody:
    predicate: str


@dataclass
class FactNeighborsBody:
    fact_id: str
    depth: int = 2
    temporal_window_s: float = 60.0


# ---------- helper: per-session resources ----------

def _store_for(ctx: ChannelContext, session_root: Path) -> FactStore:
    if not ctx.engagement_id:
        raise ChannelRejection("ChannelContext.engagement_id required for facts")
    return FactStore(session_root / ctx.engagement_id)


# ---------- channels ----------

class FactAssertChannel(Channel):
    name = "fact.assert"
    schema = FactAssertBody
    sink_subsystem = "facts.jsonl"

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def validate(self, body: dict) -> None:
        super().validate(body)
        if not body.get("predicate"):
            raise ChannelRejection("predicate required")
        evidence_refs = body.get("evidence_refs") or []
        depends_on = body.get("depends_on") or []
        if not evidence_refs and not depends_on:
            raise ChannelRejection(
                "fact must trace to either evidence_refs or depends_on (both empty)"
            )
        confidence = body.get("confidence", 1.0)
        if not (0.0 <= float(confidence) <= 1.0):
            raise ChannelRejection(f"confidence out of [0,1]: {confidence}")
        source = body.get("source", "agent_assertion")
        if source not in ("agent_assertion", "crumb_emit", "operator_input", "import"):
            raise ChannelRejection(f"unknown source: {source!r}")

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        if not ctx.activation_id:
            raise ChannelRejection("ChannelContext.activation_id required")
        store = _store_for(ctx, self.sessions_root)
        binder = EvidenceBinder(store.session_dir)
        try:
            from ara.runtime import provenance as _prov
            _prov.enrich_evidence(
                body, binder=binder, ctx=ctx,
                session_dir=store.session_dir,
                state_root=self.sessions_root.parent,
            )
        except Exception:  # noqa: BLE001
            pass
        from ara.runtime.binder import sources as _bsources
        evidence_refs = list(body.get("evidence_refs") or [])
        _rb_seen = False
        for ev in evidence_refs:
            rec = binder.get_record(ev)
            if rec is None:
                raise ChannelRejection(f"evidence_ref not found in binder: {ev}")
            if _bsources.is_response_bearing(rec.source or ""):
                _rb_seen = True
        _gate_reject = bool(evidence_refs) and not _rb_seen
        try:
            from ara import debug_log as _dbg
            _dbg.emit(
                "finding-gate", "decision",
                state_root=self.sessions_root.parent, session_id=ctx.engagement_id,
                kind="fact", verdict=("reject" if _gate_reject else "accept"),
                reason=("no response-bearing evidence" if _gate_reject else ""),
                activation_id=ctx.activation_id,
            )
        except Exception:  # noqa: BLE001
            pass
        if _gate_reject:
            # Primary-evidence gate: a fact citing evidence directly must cite ≥1
            # response-bearing record (the observed result), not command/connection
            # metadata. A fact tracing only via depends_on defers to those facts.
            raise ChannelRejection(
                "PRIMARY_EVIDENCE_RESPONSE_BEARING: fact cites only command/"
                "connection evidence; cite the tool output or HTTP response that "
                "establishes the observation (source tool:*, mitm:request, or shim:spawn_output)")
        depends_on = list(body.get("depends_on") or [])
        for did in depends_on:
            if store.by_id(did) is None:
                raise ChannelRejection(f"depends_on fact not found: {did}")

        ts = time.time()
        crumb = body["asserted_by_crumb"]
        chunk_at_time = body.get("chunk_at_time") or "_unknown"
        fact = Fact(
            id=mint_id(
                predicate=body["predicate"], value=body["value"],
                asserted_by_crumb=crumb, evidence_refs=evidence_refs,
                activation_id=ctx.activation_id, asserted_at=ts,
            ),
            predicate=body["predicate"],
            value=body["value"],
            asserted_by_crumb=crumb,
            evidence_refs=evidence_refs,
            chunk_at_time=chunk_at_time,
            activation_id=ctx.activation_id,
            asserted_at=ts,
            prior_fact=body.get("prior_fact"),
            depends_on=depends_on,
            confidence=float(body.get("confidence", 1.0)),
            source=body.get("source", "agent_assertion"),
            note=body.get("note"),
        )
        stored = store.append(fact)
        return {"fact": stored.to_jsonable(), "deduplicated": stored.id != fact.id}


class FactQueryChannel(Channel):
    name = "facts.query"
    schema = FactQueryBody
    sink_subsystem = "facts.jsonl"

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        store = _store_for(ctx, self.sessions_root)
        facts = store.by_predicate(body["predicate"], exact=bool(body.get("exact", False)))
        return {"facts": [f.to_jsonable() for f in facts]}


class FactHistoryChannel(Channel):
    name = "facts.history"
    schema = FactHistoryBody
    sink_subsystem = "facts.jsonl"

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        store = _store_for(ctx, self.sessions_root)
        facts = store.history(body["predicate"])
        return {"facts": [f.to_jsonable() for f in facts]}


class FactNeighborsChannel(Channel):
    name = "facts.neighbors"
    schema = FactNeighborsBody
    sink_subsystem = "facts.jsonl"

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        store = _store_for(ctx, self.sessions_root)
        idx = NeighborIndex(store)
        nbh = idx.for_fact(
            body["fact_id"],
            temporal_window_s=float(body.get("temporal_window_s", 60.0)),
            semantic_depth=int(body.get("depth", 2)),
        )
        return {
            "center": nbh.center,
            "causal": nbh.causal,
            "temporal_before": nbh.temporal_before,
            "temporal_after": nbh.temporal_after,
            "evidential": nbh.evidential,
            "semantic_upstream": nbh.semantic_upstream,
            "semantic_downstream": nbh.semantic_downstream,
        }


# Class-level registration — populates the catalog at import time, no
# session binding required.
for _cls in (FactAssertChannel, FactQueryChannel,
             FactHistoryChannel, FactNeighborsChannel):
    registry.register_class(_cls)


def install_fact_channels(*, sessions_root: Path) -> dict[str, Channel]:
    """Construct + register the four fact channels for a given sessions root.

    Idempotent: if a channel by the same name is already registered, the
    existing instance is reused (no duplicate-instance error).
    """
    builders = {
        "fact.assert": lambda: FactAssertChannel(sessions_root=sessions_root),
        "facts.query": lambda: FactQueryChannel(sessions_root=sessions_root),
        "facts.history": lambda: FactHistoryChannel(sessions_root=sessions_root),
        "facts.neighbors": lambda: FactNeighborsChannel(sessions_root=sessions_root),
    }
    channels: dict[str, Channel] = {}
    for name, build in builders.items():
        if registry.has(name):
            channels[name] = registry.get(name)
        else:
            ch = build()
            registry.register(ch)
            channels[name] = ch
    return channels
