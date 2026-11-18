"""Typed channels for the leads subsystem.

Channels:
- ``lead.file``         (agent or operator) — file new lead
- ``lead.investigate``  (operator)          — commit; stability ×2
- ``lead.touch``        (operator)          — glance; stability ×1.3
- ``lead.dismiss``      (operator)          — close (with reason)
- ``lead.promote``      (operator)          — to ephemeral play
- ``lead.confirm``      (system)            — auto by FindingSignOff
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection
from ara.runtime.facts.store import FactStore
from ara.runtime.leads.decay import (
    AGED_CEILING_S,
    INITIAL_STABILITY_S,
    apply_action,
)
from ara.runtime.leads.priority import compute_priority, PriorityWeights
from ara.runtime.leads.record import (
    FieldEnrichment,
    Lead,
    NegationPredicate,
    PrioritySignals,
    mint_lead_id,
)
from ara.runtime.leads.store import LeadStore


# ---------- bodies ----------

@dataclass
class LeadFileBody:
    title: str
    reasoning: str
    evidence_refs: list[str] = field(default_factory=list)
    fact_refs: list[str] = field(default_factory=list)
    proposed_test: str | None = None
    not_mechanically_testable: bool = False
    origin: str = "agent_hypothesis"
    derived_from_crumb: str | None = None
    affected_assets: list[str] = field(default_factory=list)
    value_tier: float | None = None
    # Back-compat alias accepted at execute() time:
    attack_surface_tier: float | None = None
    scope_weight: float | None = None
    priority_agent_proposed: str | None = None
    negation_predicates: list[dict] = field(default_factory=list)
    enrichments: list[dict] = field(default_factory=list)
    notes: str | None = None


@dataclass
class _LeadIdBody:
    lead_id: str
    note: str | None = None


@dataclass
class LeadDismissBody:
    lead_id: str
    reason: str
    supersedes: str | None = None
    note: str | None = None


@dataclass
class LeadConfirmBody:
    lead_id: str
    finding_id: str


@dataclass
class LeadOverrideBody:
    lead_id: str
    priority: str
    note: str | None = None


# ---------- helpers ----------

def _store_for(ctx: ChannelContext, sessions_root: Path) -> LeadStore:
    if not ctx.engagement_id:
        raise ChannelRejection("ChannelContext.engagement_id required for leads")
    return LeadStore(sessions_root / ctx.engagement_id)


def _binder_for(ctx: ChannelContext, sessions_root: Path) -> EvidenceBinder:
    return EvidenceBinder(sessions_root / (ctx.engagement_id or "_default"))


def _facts_for(ctx: ChannelContext, sessions_root: Path) -> FactStore:
    return FactStore(sessions_root / (ctx.engagement_id or "_default"))


# ---------- channels ----------

class LeadFileChannel(Channel):
    name = "lead.file"
    schema = LeadFileBody
    sink_subsystem = "leads.jsonl"

    def __init__(self, *, sessions_root: Path,
                 priority_weights: PriorityWeights | None = None) -> None:
        self.sessions_root = Path(sessions_root)
        self.priority_weights = priority_weights or PriorityWeights()

    def validate(self, body: dict) -> None:
        super().validate(body)
        title = body.get("title") or ""
        reasoning = body.get("reasoning") or ""
        if not title.strip() or not reasoning.strip():
            raise ChannelRejection("title and reasoning are required")
        if len(title) > 120:
            raise ChannelRejection("title exceeds 120 chars")
        if not body.get("not_mechanically_testable", False) and not (
            body.get("evidence_refs") or body.get("fact_refs")
        ):
            raise ChannelRejection(
                "lead must cite either evidence_refs or fact_refs "
                "(or be marked not_mechanically_testable)"
            )
        origin = body.get("origin", "agent_hypothesis")
        if origin not in ("agent_hypothesis", "anomaly_promoted",
                          "operator_input", "operator_seeded_agent_enriched",
                          "system_repair"):
            raise ChannelRejection(f"unknown origin: {origin!r}")

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        if not ctx.activation_id:
            raise ChannelRejection("ChannelContext.activation_id required")
        binder = _binder_for(ctx, self.sessions_root)
        try:
            from ara.runtime import provenance as _prov
            _prov.enrich_evidence(
                body, binder=binder, ctx=ctx,
                session_dir=self.sessions_root / (ctx.engagement_id or "_default"),
                state_root=self.sessions_root.parent,
            )
        except Exception:  # noqa: BLE001
            pass
        from ara.runtime.binder import sources as _bsources
        _refs = body.get("evidence_refs") or []
        _rb_seen = False
        for ev in _refs:
            _rec = binder.get_record(ev)
            if _rec is None:
                raise ChannelRejection(f"evidence_ref not found: {ev}")
            if _bsources.is_response_bearing(_rec.source or ""):
                _rb_seen = True
        _gate_reject = bool(_refs) and not _rb_seen
        try:
            from ara import debug_log as _dbg
            _dbg.emit(
                "finding-gate", "decision",
                state_root=self.sessions_root.parent, session_id=ctx.engagement_id,
                kind="lead", verdict=("reject" if _gate_reject else "accept"),
                reason=("no response-bearing evidence" if _gate_reject else ""),
                activation_id=ctx.activation_id,
            )
        except Exception:  # noqa: BLE001
            pass
        if _gate_reject:
            # Primary-evidence gate: if a lead cites evidence directly, at least
            # one ref must be response-bearing (the output/response that led to the
            # theory), not a command/connection-only record. A lead citing only
            # fact_refs defers to those facts' own primaries.
            raise ChannelRejection(
                "PRIMARY_EVIDENCE_RESPONSE_BEARING: lead cites only command/"
                "connection evidence; cite the tool output or HTTP response that "
                "led to the theory (source tool:*, mitm:request, or shim:spawn_output)")
        fact_store = _facts_for(ctx, self.sessions_root)
        for fid in body.get("fact_refs") or []:
            if fact_store.by_id(fid) is None:
                raise ChannelRejection(f"fact_ref not found: {fid}")

        priority, signals = compute_priority(
            body, binder=binder, weights=self.priority_weights,
        )
        now = time.time()
        lead = Lead(
            id=mint_lead_id(),
            title=body["title"],
            reasoning=body["reasoning"],
            proposed_test=body.get("proposed_test"),
            not_mechanically_testable=bool(body.get("not_mechanically_testable", False)),
            evidence_refs=list(body.get("evidence_refs") or []),
            fact_refs=list(body.get("fact_refs") or []),
            activation_id=ctx.activation_id,
            origin=body.get("origin", "agent_hypothesis"),
            enrichments=[FieldEnrichment(**e) for e in (body.get("enrichments") or [])],
            priority_computed=priority,
            priority_signals=signals,
            priority_agent_proposed=body.get("priority_agent_proposed"),
            priority_operator_override=None,
            stability_s=INITIAL_STABILITY_S[priority],
            last_reviewed_at=now,
            review_history=[],
            aged_ceiling_at=now + AGED_CEILING_S[priority],
            status="open",
            death_signal=None, death_citation=None,
            created_at=now, closed_at=None, closed_by=None,
            derived_from_crumb=body.get("derived_from_crumb"),
            affected_assets=list(body.get("affected_assets") or []),
            negation_predicates=[
                NegationPredicate(**n) for n in (body.get("negation_predicates") or [])
            ],
            notes=body.get("notes"),
        )
        store = _store_for(ctx, self.sessions_root)
        store.append(lead)
        return {
            "lead": lead.to_jsonable(),
            "priority": lead.priority_computed,
            "signals": _signal_dict(signals),
        }


def _signal_dict(s: PrioritySignals) -> dict[str, float]:
    return {
        "evidence_strength": s.evidence_strength,
        "value_tier": s.value_tier,
        # Back-compat: also expose under the old name.
        "attack_surface_tier": s.value_tier,
        "crumb_provenance_score": s.crumb_provenance_score,
        "scope_weight": s.scope_weight,
        "prior_finding_yield": s.prior_finding_yield,
        "aggregate": s.aggregate,
    }


def _mutate_action(
    sessions_root: Path, ctx: ChannelContext, body: dict,
    *, action: str, actor: str | None = None,
    note: str | None = None,
) -> Lead:
    store = _store_for(ctx, sessions_root)
    lead = store.by_id(body["lead_id"])
    if lead is None:
        raise ChannelRejection(f"unknown lead: {body['lead_id']!r}")
    new_lead = apply_action(
        lead, action,
        now=time.time(), actor=actor or ctx.caller, note=note,
    )
    store.append(new_lead)
    return new_lead


class LeadInvestigateChannel(Channel):
    name = "lead.investigate"
    schema = _LeadIdBody
    sink_subsystem = "leads.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        ld = _mutate_action(
            self.sessions_root, ctx, body,
            action="investigating", note=body.get("note"),
        )
        return {"lead": ld.to_jsonable()}


class LeadTouchChannel(Channel):
    name = "lead.touch"
    schema = _LeadIdBody
    sink_subsystem = "leads.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        ld = _mutate_action(
            self.sessions_root, ctx, body,
            action="touched", note=body.get("note"),
        )
        return {"lead": ld.to_jsonable()}


class LeadDismissChannel(Channel):
    name = "lead.dismiss"
    schema = LeadDismissBody
    sink_subsystem = "leads.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        store = _store_for(ctx, self.sessions_root)
        lead = store.by_id(body["lead_id"])
        if lead is None:
            raise ChannelRejection(f"unknown lead: {body['lead_id']!r}")
        if body.get("supersedes"):
            from dataclasses import replace
            new = replace(
                lead, status="superseded",
                closed_at=time.time(), closed_by=ctx.caller,
                death_signal="superseded_by",
                death_citation=body["supersedes"],
            )
        else:
            new = apply_action(
                lead, "dismissed",
                now=time.time(), actor=ctx.caller,
                note=f"reason: {body.get('reason') or 'unspecified'}",
            )
        store.append(new)
        return {"lead": new.to_jsonable()}


class LeadPromoteChannel(Channel):
    name = "lead.promote"
    schema = _LeadIdBody
    sink_subsystem = "leads.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        ld = _mutate_action(
            self.sessions_root, ctx, body,
            action="promoted", note=body.get("note"),
        )
        return {"lead": ld.to_jsonable()}


class LeadConfirmChannel(Channel):
    name = "lead.confirm"
    schema = LeadConfirmBody
    sink_subsystem = "leads.jsonl"
    allowed_callers = ("system",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        ld = _mutate_action(
            self.sessions_root, ctx, body,
            action="confirmed",
            note=f"confirmed via finding {body['finding_id']}",
        )
        return {"lead": ld.to_jsonable()}


class LeadPriorityOverrideChannel(Channel):
    name = "lead.priority_override"
    schema = LeadOverrideBody
    sink_subsystem = "leads.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        store = _store_for(ctx, self.sessions_root)
        lead = store.by_id(body["lead_id"])
        if lead is None:
            raise ChannelRejection(f"unknown lead: {body['lead_id']!r}")
        if body["priority"] not in ("critical", "high", "medium", "low"):
            raise ChannelRejection(f"unknown priority: {body['priority']!r}")
        from dataclasses import replace
        new = replace(lead, priority_operator_override=body["priority"])
        store.append(new)
        return {"lead": new.to_jsonable()}


for _cls in (LeadFileChannel, LeadInvestigateChannel, LeadTouchChannel,
             LeadDismissChannel, LeadPromoteChannel, LeadConfirmChannel,
             LeadPriorityOverrideChannel):
    registry.register_class(_cls)


def install_lead_channels(*, sessions_root: Path) -> dict[str, Channel]:
    """Idempotent: reuse already-registered channels under the same name."""
    builders = {
        "lead.file": lambda: LeadFileChannel(sessions_root=sessions_root),
        "lead.investigate": lambda: LeadInvestigateChannel(sessions_root=sessions_root),
        "lead.touch": lambda: LeadTouchChannel(sessions_root=sessions_root),
        "lead.dismiss": lambda: LeadDismissChannel(sessions_root=sessions_root),
        "lead.promote": lambda: LeadPromoteChannel(sessions_root=sessions_root),
        "lead.confirm": lambda: LeadConfirmChannel(sessions_root=sessions_root),
        "lead.priority_override": lambda: LeadPriorityOverrideChannel(sessions_root=sessions_root),
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


# Silence unused-import noise reserved for future audit
_ = (Any,)
