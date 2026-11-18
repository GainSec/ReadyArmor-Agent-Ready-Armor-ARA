"""Routing plan for ingesting Temper entries into their enforced homes.

Single source of truth shared by ``temper._materialize_active_temper`` (which
writes the files) and the armory Ingest review page (which shows the operator
where each entry lands *before* they commit). Every enforceable Temper
destination resolves to a Bulwark rule; ``absolute`` resolves to the armor's
ABSOLUTE (injected live as the prompt envelope).

Enforcement ground truth (mitm_proxy):
  * request (``ask``/exec/fs/net/url): ``deny``/``approval`` act; ``ask`` also
    supports ``rewrite`` (prompt mutation via ``_try_apply_request_rewrite``).
  * response (``reply``): only ``deny``/``approval`` act — reply ``rewrite``/
    ``shadow_rewrite`` are NOT applied to the response body. So a content
    tripwire that must actually fire has to be ``deny reply``.

Each plan carries an honest ``enforced`` flag: True only when the resolved
(layer, action) is one the proxy actually acts on. Advisory fallbacks land in
the ABSOLUTE so the intent still reaches the model, flagged as non-mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import temper_controls

# (layer, action) pairs the proxy actually enforces. See module docstring.
_ENFORCED_PAIRS = frozenset(
    {(layer, "deny") for layer in ("exec", "fs", "net", "url", "env", "ask", "reply")}
    | {(layer, "approval") for layer in ("exec", "fs", "net", "url", "env", "ask", "reply")}
    | {("ask", "rewrite"), ("url", "rewrite")}
)

BULWARK_HOME = "bulwark.md"
ABSOLUTE_HOME = "absolute.md"


@dataclass
class IngestPlan:
    """Where a single Temper entry lands and whether it mechanically enforces."""
    destination: str
    home: str                       # BULWARK_HOME | ABSOLUTE_HOME
    enforced: bool                  # True => hard mechanical rule; False => advisory prompt
    rule_text: str                  # exact bulwark rule line(s), or the absolute body
    summary: str                    # plain-English "what it does"
    layer: Optional[str] = None     # bulwark layer, when home == BULWARK_HOME
    action: Optional[str] = None    # bulwark action, when home == BULWARK_HOME
    note: str = ""                  # caveat surfaced to the operator


def _cite_comment(citations) -> str:
    ids = [
        str(c.get("manifest_element_id")).strip()
        for c in (citations or [])
        if isinstance(c, dict) and c.get("manifest_element_id")
    ]
    return f"  # cite: {', '.join(ids)}" if ids else ""


def _bulwark_line(action: str, layer: str, match: str, *, highlight: bool = False) -> str:
    # Modifiers (e.g. ``highlight``) sit BETWEEN action and layer; a trailing
    # modifier would be swallowed by the greedy content match.
    match = temper_controls.normalize_bulwark_match(match)
    mod = "highlight " if highlight else ""
    return f"{action} {mod}{layer} {match}"


def _plan_bulwark(control: dict) -> IngestPlan:
    action = str(control.get("action") or "deny").strip()
    layer = str(control.get("layer") or "exec").strip()
    match = str(control.get("match") or "").strip()
    line = _bulwark_line(action, layer, match)
    return IngestPlan(
        destination="bulwark", home=BULWARK_HOME,
        enforced=(layer, action) in _ENFORCED_PAIRS,
        rule_text=line, layer=layer, action=action,
        summary=f"{action} {layer} events matching {match!r}",
    )


def _plan_rhema(control: dict) -> IngestPlan:
    # Rhema entries are content rules. Faithful translation to a bulwark
    # content-layer rule; enforced only for pairs the proxy acts on.
    action = str(control.get("action") or "deny").strip()
    layer = str(control.get("layer") or "reply").strip()
    pattern = str(control.get("pattern") or "").strip()
    enforced = (layer, action) in _ENFORCED_PAIRS
    note = ""
    if layer == "reply" and action in ("rewrite", "shadow_rewrite"):
        note = (
            "reply-content rewrite is not applied to responses; use "
            "'deny reply' to block+flag matching output"
        )
    return IngestPlan(
        destination="rhema", home=BULWARK_HOME, enforced=enforced,
        rule_text=_bulwark_line(action, layer, pattern), layer=layer, action=action,
        summary=f"{action} {layer} content matching {pattern!r}", note=note,
    )


def _plan_canary(control: dict) -> IngestPlan:
    # A canary is an output tripwire: block+flag any response carrying the
    # pattern. `deny reply` is the only response action the proxy enforces.
    pattern = str(control.get("pattern") or "").strip()
    line = _bulwark_line("deny", "reply", pattern, highlight=True)
    return IngestPlan(
        destination="canary", home=BULWARK_HOME, enforced=True,
        rule_text=line, layer="reply", action="deny",
        summary=f"blocks + flags any model response containing {pattern!r}",
    )


# Common model-emitted "surface" words mapped onto bulwark's layer vocabulary,
# so an approval_gate that names its surface as e.g. "api"/"command" still
# becomes a real gate instead of advisory-only prose.
_SURFACE_TO_LAYER = {
    "exec": "exec", "command": "exec", "commands": "exec", "shell": "exec",
    "binary": "exec", "process": "exec", "tool": "exec",
    "fs": "fs", "file": "fs", "files": "fs", "filesystem": "fs", "path": "fs",
    "net": "net", "network": "net", "socket": "net", "connection": "net",
    "url": "url", "http": "url", "https": "url", "api": "url",
    "endpoint": "url", "web": "url", "request_url": "url",
    "reply": "reply", "response": "reply", "output": "reply",
    "ask": "ask", "request": "ask", "prompt": "ask", "input": "ask",
    "env": "env",
}


def _resolve_layer(control: dict) -> str:
    """Resolve a bulwark layer from the control's ``layer`` or ``surface``
    (with common-alias mapping). Empty string if none maps."""
    for field in ("layer", "surface"):
        v = str(control.get(field) or "").strip().lower()
        if v in temper_controls._LAYERS:
            return v
        if v in _SURFACE_TO_LAYER:
            return _SURFACE_TO_LAYER[v]
    return ""


def _plan_layered_or_advisory(destination: str, control: dict, title: str, body: str,
                              *, highlight: bool) -> IngestPlan:
    """approval_gate / alert_hook: enforce as a bulwark rule when the control
    carries a resolvable layer (layer/surface) + match/trigger; otherwise land
    in the ABSOLUTE as flagged advisory guidance (intent still reaches the model)."""
    layer = _resolve_layer(control)
    match = str(control.get("match") or control.get("trigger") or "").strip()
    action = "approval" if destination == "approval_gate" else "allow"
    if layer in temper_controls._LAYERS and match:
        return IngestPlan(
            destination=destination, home=BULWARK_HOME,
            enforced=(layer, action) in _ENFORCED_PAIRS,
            rule_text=_bulwark_line(action, layer, match, highlight=highlight),
            layer=layer, action=action,
            summary=f"{action} {layer} events matching {match!r}",
        )
    return IngestPlan(
        destination=destination, home=ABSOLUTE_HOME, enforced=False,
        rule_text=body or title, summary=f"advisory guidance: {title}",
        note="no concrete layer+match — captured as prompt guidance, not a mechanical rule",
    )


def plan_item(item) -> IngestPlan:
    """Resolve one Temper item to its ingest destination.

    ``item`` is a ``TemperItem`` (has ``.destination``, ``.control``,
    ``.title``, ``.proposed_text``, ``.citations``).
    """
    destination = str(getattr(item, "destination", "") or "").strip()
    control = dict(getattr(item, "control", None) or {})
    citations = getattr(item, "citations", None) or []
    title = str(getattr(item, "title", "") or "")
    proposed = str(getattr(item, "proposed_text", "") or "")

    if destination == "absolute":
        if control:
            body = temper_controls.render_control(
                "absolute", control, citations=citations, fallback_text=proposed,
            ).strip()
        else:
            body = proposed
        return IngestPlan(
            destination="absolute", home=ABSOLUTE_HOME, enforced=False,
            rule_text=body, summary="injected into the system prompt (advisory)",
        )
    if destination == "bulwark":
        plan = _plan_bulwark(control)
    elif destination == "rhema":
        plan = _plan_rhema(control)
    elif destination == "canary":
        plan = _plan_canary(control)
    elif destination == "approval_gate":
        plan = _plan_layered_or_advisory(
            destination, control, title,
            str(control.get("approval_prompt") or proposed), highlight=False)
    elif destination == "alert_hook":
        plan = _plan_layered_or_advisory(
            destination, control, title,
            str(control.get("message") or proposed), highlight=True)
    else:
        # trim_template or unknown — advisory capture, never silently enforced.
        return IngestPlan(
            destination=destination, home=ABSOLUTE_HOME, enforced=False,
            rule_text=proposed or title,
            summary=f"{destination or 'entry'} captured as guidance",
            note="unrecognized destination — captured as prompt guidance",
        )

    # Attach citation trail to bulwark rules for provenance.
    if plan.home == BULWARK_HOME:
        cite = _cite_comment(citations)
        if cite:
            plan.rule_text = plan.rule_text + cite
    return plan
