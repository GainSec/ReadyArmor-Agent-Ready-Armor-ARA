"""anomaly.emit + anomaly.promote + anomaly.dismiss channels.

Per 3.2 confirm Q5: hard cap of 50 anomalies per crumb per activation
(operator-tunable; emits beyond the cap are dropped with audit log).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ara.runtime.anomaly.divergence import DivergenceVector
from ara.runtime.anomaly.record import (
    AnomalyRecord,
    AnomalyStore,
    divergence_to_dict,
    mint_anomaly_id,
)
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection


_DEFAULT_CAP_PER_CRUMB_PER_ACTIVATION = 50


@dataclass
class AnomalyEmitBody:
    evidence_id: str
    crumb_id: str
    expected_shape_hash: str
    divergence: dict
    threshold_at_emission: float


@dataclass
class AnomalyPromoteBody:
    anomaly_id: str
    title: str
    reasoning: str = ""


@dataclass
class AnomalyDismissBody:
    anomaly_id: str
    reason: str


def _session_dir(ctx: ChannelContext, sessions_root: Path) -> Path:
    if not ctx.engagement_id:
        raise ChannelRejection("ChannelContext.engagement_id required")
    return sessions_root / ctx.engagement_id


class AnomalyEmitChannel(Channel):
    name = "anomaly.emit"
    schema = AnomalyEmitBody
    sink_subsystem = "anomalies.jsonl"

    def __init__(
        self, *, sessions_root: Path,
        cap_per_crumb_per_activation: int = _DEFAULT_CAP_PER_CRUMB_PER_ACTIVATION,
    ) -> None:
        self.sessions_root = Path(sessions_root)
        self.cap = cap_per_crumb_per_activation

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        if not ctx.activation_id:
            raise ChannelRejection("ChannelContext.activation_id required")
        sd = _session_dir(ctx, self.sessions_root)
        binder = EvidenceBinder(sd)
        if binder.get_record(body["evidence_id"]) is None:
            raise ChannelRejection(f"evidence_id not found: {body['evidence_id']!r}")

        store = AnomalyStore(sd)
        existing = store.count_for_crumb_in_activation(
            body["crumb_id"], ctx.activation_id,
        )
        if existing >= self.cap:
            return {
                "dropped": True,
                "reason": f"cap reached: {existing}/{self.cap} for crumb in activation",
                "cap": self.cap,
            }
        rec = AnomalyRecord(
            id=mint_anomaly_id(),
            evidence_id=body["evidence_id"],
            crumb_id=body["crumb_id"],
            expected_shape_hash=body["expected_shape_hash"],
            divergence=dict(body.get("divergence") or {}),
            threshold_at_emission=float(body["threshold_at_emission"]),
            activation_id=ctx.activation_id,
            detected_at=time.time(),
        )
        store.append(rec)
        return {"anomaly": rec.to_jsonable(), "dropped": False}


class AnomalyPromoteChannel(Channel):
    name = "anomaly.promote"
    schema = AnomalyPromoteBody
    sink_subsystem = "anomalies.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        sd = _session_dir(ctx, self.sessions_root)
        store = AnomalyStore(sd)
        anom = store.by_id(body["anomaly_id"])
        if anom is None:
            raise ChannelRejection(f"unknown anomaly: {body['anomaly_id']!r}")
        if anom.status != "pending":
            raise ChannelRejection(f"anomaly status={anom.status}, can't promote")
        # File a lead via lead.file
        try:
            lead_file = registry.get("lead.file")
        except KeyError as e:
            raise ChannelRejection("lead.file channel not installed") from e
        lead_ctx = ChannelContext(
            engagement_id=ctx.engagement_id,
            activation_id=ctx.activation_id,
            forge_id=ctx.forge_id,
            caller=ctx.caller,
            audit_sink=ctx.audit_sink,
        )
        lead_res = lead_file.call({
            "title": body["title"][:120],
            "reasoning": body.get("reasoning") or
                f"Anomaly {anom.id}: divergence raw_score="
                f"{anom.divergence.get('raw_score', 0):.2f}",
            "evidence_refs": [anom.evidence_id],
            "origin": "anomaly_promoted",
            "derived_from_crumb": anom.crumb_id,
        }, lead_ctx)
        from dataclasses import replace
        promoted = replace(
            anom, status="promoted_to_lead",
            promoted_lead_id=lead_res["lead"]["id"],
        )
        store.append(promoted)
        return {"anomaly": promoted.to_jsonable(), "lead": lead_res["lead"]}


class AnomalyDismissChannel(Channel):
    name = "anomaly.dismiss"
    schema = AnomalyDismissBody
    sink_subsystem = "anomalies.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        sd = _session_dir(ctx, self.sessions_root)
        store = AnomalyStore(sd)
        anom = store.by_id(body["anomaly_id"])
        if anom is None:
            raise ChannelRejection(f"unknown anomaly: {body['anomaly_id']!r}")
        if anom.status != "pending":
            raise ChannelRejection(f"anomaly status={anom.status}, can't dismiss")
        from dataclasses import replace
        dismissed = replace(
            anom, status="dismissed_operator",
            dismissed_at=time.time(), dismissed_by=ctx.caller,
            dismiss_reason=body.get("reason"),
        )
        store.append(dismissed)
        return {"anomaly": dismissed.to_jsonable()}


for _cls in (AnomalyEmitChannel, AnomalyPromoteChannel, AnomalyDismissChannel):
    registry.register_class(_cls)


def install_anomaly_channels(
    *, sessions_root: Path,
    cap_per_crumb_per_activation: int = _DEFAULT_CAP_PER_CRUMB_PER_ACTIVATION,
) -> dict[str, Channel]:
    builders = {
        "anomaly.emit": lambda: AnomalyEmitChannel(
            sessions_root=sessions_root,
            cap_per_crumb_per_activation=cap_per_crumb_per_activation,
        ),
        "anomaly.promote": lambda: AnomalyPromoteChannel(sessions_root=sessions_root),
        "anomaly.dismiss": lambda: AnomalyDismissChannel(sessions_root=sessions_root),
    }
    chans: dict[str, Channel] = {}
    for name, build in builders.items():
        if registry.has(name):
            chans[name] = registry.get(name)
        else:
            ch = build()
            registry.register(ch)
            chans[name] = ch
    return chans


# Silence unused-import warnings reserved for future
_ = (Any, DivergenceVector, field)
