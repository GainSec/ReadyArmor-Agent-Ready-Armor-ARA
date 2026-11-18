"""Provenance enrichment for fact/lead/finding filing.

At filing time this ties an entry to the causal chain that is *already captured*:
the **primary** (a response-bearing tool/HTTP record — auto-correlated from the
current activation so the primary-evidence gate is self-satisfying), the
**connection** it rode over (``net:connection`` materialized from the seccomp
ledger), and the **authorization** in effect (the active absolute/rhema rule).
The LLM turn that caused it is anchored by the entry's own ``activation_id``.

All of this is **additive and best-effort**: it only ever appends to
``evidence_refs`` (never removes what the agent cited) and never raises — a
failure here must not block a legitimate filing. Everything reuses existing
subsystems (the binder, ``net_capture``, ``_rhema_shim.active_absolute_rule``,
``ActivationManager``); no new store.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ara import debug_log
from ara.runtime.binder import sources as _sources


def _is_rb(rec: Any) -> bool:
    return rec is not None and _sources.is_response_bearing(getattr(rec, "source", "") or "")


def _latest_response_bearing(binder: Any, activation_id: str | None) -> str | None:
    """Most-recently captured response-bearing evidence in this activation."""
    if not activation_id:
        return None
    try:
        recs = [r for r in binder.by_activation(activation_id) if _is_rb(r)]
    except Exception:  # noqa: BLE001
        return None
    if not recs:
        return None
    recs.sort(key=lambda r: getattr(r, "captured_at", 0.0), reverse=True)
    return recs[0].id


def _bind_authorization(binder: Any, state_root: Any, engagement_id: str | None,
                        activation_id: str | None) -> str | None:
    """Bind the active absolute/rhema rule in effect as ``authorization:trim``
    evidence and return its id. Content-addressed → deduped across filings."""
    try:
        from ara import _rhema_shim as _rh
    except Exception:  # noqa: BLE001
        return None
    try:
        rule = _rh.active_absolute_rule(Path(state_root))
    except Exception:  # noqa: BLE001
        rule = None
    if rule is None:
        return None
    try:
        payload = {
            "rule_id": getattr(rule, "id", None),
            "reason": getattr(rule, "reason", None),
            "redirect": getattr(rule, "redirect", None),
            "engagement_id": engagement_id,
        }
        meta: dict[str, Any] = {}
        rid = getattr(rule, "id", None)
        if rid:
            meta["rule_id"] = str(rid)
        if engagement_id:
            meta["engagement_id"] = str(engagement_id)
        rec = binder.put(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
            source="authorization:trim",
            source_metadata=meta,
            summary=f"authorization {rid or ''}".strip(),
            content_type="application/json",
            activation_id=activation_id,
        )
        return rec.id
    except Exception:  # noqa: BLE001
        return None


def enrich_evidence(
    body: dict,
    *,
    binder: Any,
    ctx: Any,
    session_dir: Any,
    state_root: Any = None,
    max_connections: int = 5,
) -> dict:
    """Additively bind the provenance chain onto ``body['evidence_refs']``. Returns
    a summary of the edges added (for debug/observability). Never raises."""
    added: dict[str, list[str]] = {
        "primary": [], "connected_via": [], "authorized_by": [],
    }
    try:
        refs = list(body.get("evidence_refs") or [])
        act = getattr(ctx, "activation_id", None)
        eng = getattr(ctx, "engagement_id", None)
        if state_root is None:
            try:
                state_root = Path(session_dir).parent.parent  # <state>/sessions/<id>
            except Exception:  # noqa: BLE001
                state_root = None

        # 1. Auto-correlate the response-bearing PRIMARY (makes the gate
        #    self-satisfying) if the agent didn't already cite one.
        if not any(_is_rb(binder.get_record(r)) for r in refs):
            prim = _latest_response_bearing(binder, act)
            if prim and prim not in refs:
                refs.append(prim)
                added["primary"].append(prim)

        # 2. connected_via — materialize the activation's net:connection records.
        if state_root is not None and eng:
            try:
                from ara.runtime.binder.net_capture import materialize_net_connections
                for cid in materialize_net_connections(
                    session_dir, state_root, eng, activation_id=act,
                )[:max_connections]:
                    if cid not in refs:
                        refs.append(cid)
                        added["connected_via"].append(cid)
            except Exception:  # noqa: BLE001
                pass

        # 3. authorized_by — the active absolute/rhema rule in effect.
        if state_root is not None:
            aid = _bind_authorization(binder, state_root, eng, act)
            if aid and aid not in refs:
                refs.append(aid)
                added["authorized_by"].append(aid)

        body["evidence_refs"] = refs
        if any(added.values()):
            try:
                debug_log.emit(
                    "provenance", "enriched",
                    state_root=state_root,
                    session_id=eng,
                    activation_id=act,
                    primary=added["primary"],
                    connected_via=len(added["connected_via"]),
                    authorized_by=added["authorized_by"],
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return added
