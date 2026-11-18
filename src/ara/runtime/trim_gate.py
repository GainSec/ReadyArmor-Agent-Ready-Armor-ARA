"""Composite trim gate — single entry point for runtime enforcement.

The supervisor (or any caller intercepting an outbound tool call)
consults this gate *before* dispatching. It composes the four v0.1
runtime substrates + the tool classification registry into one verdict:

1. **TOD gate** — current time inside any allowed window?
2. **Stop-watcher latch** — has any stop-condition tripped on this engagement?
3. **Tool classification + policy** — `(tool, flag-pattern)` known? what
   classification, what policy verdict?
4. **Rate-limit bucket** — per-(eid, host) capacity available?

The order matters — TOD and stop-latch are kill-switches, classification
gating is "may need approval token", rate-limit is "queue if overflow".
The first failing gate wins; later gates aren't even consulted.

This module owns no state of its own — it delegates entirely to the
four substrate modules. That keeps it cheap to test (each gate is
already covered) and makes the integration into the supervisor a
single call site.

Wiring into supervisor (when an integration testbed is available):

    from ara.runtime.trim_gate import evaluate_call, GateVerdict

    def on_tool_use(self, msg):
        eid = self._engagement_id_for_session()
        if eid:
            v = evaluate_call(
                self.state_root, eid,
                op_kind="active", host=msg.target_host,
                tool=msg.tool, args=msg.args,
                trim=trims.load_active_trim(self.state_root),
            )
            if not v.allow:
                return self._inject_deny(v.reason)
            if v.requires_classification:
                return self._inject_classification_prompt(v.classification_prompt)
            if v.requires_token:
                if not tokens.has_token(v.requires_token, ...):
                    return self._inject_token_request(v.requires_token)
                tokens.require_token(v.requires_token, ...)   # consume
        # … existing supervisor pipeline …

The composite verdict is intentionally rich: callers can render the
operator UI exactly as the spec describes (deny / approval-token /
classification-pending / rate-limit-queue) without re-running checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import tool_registry as tr
from . import rate_bucket as rb
from . import stop_watcher as sw
from . import tod as tod_mod


# ---------------------------------------------------------------------------
# Inputs (caller passes a thin trim view)
# ---------------------------------------------------------------------------

@dataclass
class TrimView:
    """The runtime-relevant slice of a TrimMeta. Pass this in rather than
    the full meta so the gate doesn't have to know about the trims module
    layout, and tests can synthesize one without touching disk."""
    engagement_id: str
    env: str = "nonprod"
    posture: str = "blackbox"
    active_testing: bool = False
    tod_allowed: list[dict] = field(default_factory=list)
    rate_limits: dict = field(default_factory=dict)
    stop_conditions: list[str] = field(default_factory=list)
    paranoid_tool_gating: bool = False           # armor.meta pulls forward


@dataclass
class GateVerdict:
    """Composite result.

    - ``allow=True`` and no ``requires_*`` → proceed.
    - ``allow=True`` with ``requires_token`` → operator must have a
      one-time token; caller calls ``require_token`` to consume.
    - ``allow=True`` with ``requires_classification=True`` → caller injects
      the pause-and-ask prompt and re-runs the gate after agent reply.
    - ``allow=False`` → deny; ``reason`` carries the human-readable cause.
    - ``wait_seconds > 0`` → rate-limit overflow; caller may sleep
      (queue) or hard-deny based on trim.rate_limits.overflow.
    """
    allow: bool
    reason: str
    gate: str                                    # which gate decided
    requires_token: str | None = None
    requires_classification: bool = False
    classification_prompt: str | None = None
    classification_key: str | None = None
    wait_seconds: float = 0.0
    next_window_open_epoch: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Composite evaluator
# ---------------------------------------------------------------------------

def evaluate_call(
    state_root: Path, *,
    trim: TrimView,
    op_kind: str = "active",                     # "active" | "read" | "any"
    host: str = "",
    tool: str = "",
    args: list[str] | None = None,
    now_epoch: float,
    findings_summary: sw.FindingsSummary | None = None,
    window_start_epoch: float | None = None,
) -> GateVerdict:
    """Pure-function composition. Caller provides current time + a
    findings summary built from their own store; this module never
    reaches into runtime state outside the four substrate state files.
    """
    args = args or []

    # ── 1. TOD gate ────────────────────────────────────────────────
    tod_res = tod_mod.evaluate_gate(
        state_root, trim.engagement_id,
        tod_allowed=trim.tod_allowed,
        now_epoch=now_epoch, op_kind=op_kind,
    )
    if not tod_res.allow:
        return GateVerdict(
            allow=False,
            reason=f"TOD: {tod_res.reason}",
            gate="tod",
            next_window_open_epoch=tod_res.next_window_open_epoch,
            extra={"tod_state": tod_res.state,
                   "tod_active_decision": (
                       tod_res.active_decision.to_dict()
                       if tod_res.active_decision is not None else None
                   )},
        )

    # ── 2. Stop-watcher latch ──────────────────────────────────────
    if trim.stop_conditions:
        verdict = sw.evaluate_and_latch(
            state_root, trim.engagement_id,
            conditions=trim.stop_conditions,
            summary=findings_summary or sw.FindingsSummary(),
            now_epoch=now_epoch,
            window_start_epoch=window_start_epoch,
        )
        if verdict.tripped:
            return GateVerdict(
                allow=False,
                reason=f"stop-condition: {verdict.reason}",
                gate="stop_watcher",
                extra={"stop_condition": verdict.condition},
            )

    # ── 3. Tool classification + policy ─────────────────────────────
    classification_key: str | None = None
    if tool:
        try:
            classification_key = tr.key_for(tool, args)
        except tr.InvalidToolName:
            return GateVerdict(
                allow=False,
                reason=f"invalid tool name: {tool!r}",
                gate="tool_registry",
            )
        entry = tr.lookup(state_root, classification_key)
        # Reclassify-on-next-call honors operator's UI flag.
        needs_class = (
            entry is None or entry.reclassify_on_next_call
        )
        if needs_class:
            prompt = tr.build_classification_prompt(
                tool=tool, args=args,
                engagement_env=trim.env, posture=trim.posture,
            )
            return GateVerdict(
                allow=True,
                reason="first-use classification required",
                gate="tool_registry",
                requires_classification=True,
                classification_prompt=prompt,
                classification_key=classification_key,
            )
        # 1/3 confidence is gated until operator explicitly approves
        # (and paranoid mode forces this regardless of confidence).
        if entry.status == tr.STATUS_PENDING_FIRST_USE:
            return GateVerdict(
                allow=False,
                reason=(
                    "operator approval required before first use "
                    f"(confidence={entry.confidence}/3, "
                    f"paranoid={trim.paranoid_tool_gating})"
                ),
                gate="tool_registry",
                extra={"classification_key": classification_key},
            )
        # Apply trim policy verdict (active+passive denial,
        # destructive+prod token requirement).
        ctx = tr.PolicyContext(
            active_testing=trim.active_testing,
            env=trim.env, posture=trim.posture,
        )
        policy = tr.evaluate_policy(entry, ctx)
        if not policy.allowed:
            return GateVerdict(
                allow=False,
                reason=f"trim policy: {policy.reason}",
                gate="tool_registry",
                extra={"classification_key": classification_key},
            )
        # destructive on prod → caller must provide PROD-DESTRUCTIVE-APPROVAL
        if policy.requires_token:
            return GateVerdict(
                allow=True,
                reason=policy.reason,
                gate="tool_registry",
                requires_token=policy.requires_token,
                classification_key=classification_key,
            )

    # ── 4. Rate-limit bucket ───────────────────────────────────────
    if host and trim.rate_limits:
        res = rb.acquire(
            state_root, trim.engagement_id, host,
            now_epoch=now_epoch,
        )
        if not res.granted:
            overflow = (trim.rate_limits.get("overflow") or "queue").lower()
            if overflow == "deny":
                return GateVerdict(
                    allow=False,
                    reason=f"rate-limit: {res.reason}",
                    gate="rate_bucket",
                    wait_seconds=res.wait_seconds,
                )
            # queue: caller sleeps wait_seconds, then retries.
            return GateVerdict(
                allow=True,
                reason=f"rate-limit overflow — queue {res.wait_seconds:.3f}s",
                gate="rate_bucket",
                wait_seconds=res.wait_seconds,
                classification_key=classification_key,
            )

    return GateVerdict(
        allow=True,
        reason="all gates pass",
        gate="composite",
        classification_key=classification_key,
    )


# ---------------------------------------------------------------------------
# Trim → TrimView adapter
# ---------------------------------------------------------------------------

def view_for_active_trim(
    state_root: Path, armor_name: str | None,
) -> TrimView | None:
    """Convenience for supervisors: load the trim currently applied to
    ``armor_name`` and project it to a TrimView. Returns ``None`` when
    no trim is active (caller treats this as "no engagement contract,
    pass through with default policy").

    Local imports keep the runtime layer free of trim-module coupling
    in unit tests that don't want to load the full armor stack.
    """
    if not armor_name:
        return None
    try:
        from .. import armors as _armors
        from .. import trims as _trims
    except Exception:
        return None
    tid = _trims.active_trim_id(state_root, armor_name)
    if tid is None:
        return None
    meta = _trims.load_trim(state_root, armor_name, tid)
    if meta is None:
        return None
    armor = _armors.load_armor(state_root, armor_name)
    paranoid = bool(
        getattr(getattr(armor, "meta", None), "paranoid_tool_gating", False)
    ) if armor is not None else False
    return view_from_trim_meta(meta, paranoid=paranoid)


def view_from_trim_meta(meta: object, *, paranoid: bool = False) -> TrimView:
    """Project a ``trims.TrimMeta`` (or anything with the same fields)
    into a ``TrimView``. Kept here so callers don't need to import the
    trims module."""
    eid = getattr(meta, "engagement_id", None) or ""
    return TrimView(
        engagement_id=eid,
        env=getattr(meta, "env", "nonprod"),
        posture=getattr(meta, "posture", "blackbox"),
        active_testing=bool(getattr(meta, "active_testing", False)),
        tod_allowed=list(getattr(meta, "tod_allowed", []) or []),
        rate_limits=dict(getattr(meta, "rate_limits", {}) or {}),
        stop_conditions=list(getattr(meta, "stop_conditions", []) or []),
        paranoid_tool_gating=paranoid,
    )


__all__ = [
    "GateVerdict", "TrimView",
    "evaluate_call", "view_for_active_trim", "view_from_trim_meta",
]
