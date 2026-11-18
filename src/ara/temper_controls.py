"""Typed Tailor/Needle control contracts for Temper proposals."""
from __future__ import annotations

import json
from typing import Any


THREAD_TYPES = frozenset({
    "secret_handling",
    "methodology_fit",
    "safety_boundary",
    "prompt_mediation",
    "operator_approval",
    "alert_signal",
    "runtime_boundary",
    "trim_fit",
})

DESTINATIONS = frozenset({
    "absolute",
    "rhema",
    "bulwark",
    "canary",
    "approval_gate",
    "alert_hook",
    "trim_template",
})

_RHEMA_ACTIONS = frozenset({"deny", "approval", "rewrite", "shadow_rewrite"})
_BULWARK_ACTIONS = frozenset({"deny", "approval", "allow"})
_LAYERS = frozenset({"fs", "exec", "net", "url", "env", "reply", "ask", "acp"})

# Canonical task-family / routing scope vocabulary. Scope must be one of
# these so a candidate routes deterministically at task projection time.
SCOPE_TAGS = frozenset({
    "web_app", "mobile", "hardware", "rf_research", "anti_crime", "global",
})

_REGEX_META = set(r".^$*+?()[]{}|\\")
# Clearly-benign strings a reply-redaction pattern must NOT match. A good
# secret-redaction pattern targets a secret *format* (long tokens, key
# headers, PEM blocks) and matches none of these. An over-broad pattern
# (e.g. ``https?://[\w.-]+`` or one keying on the word ``hostname``) does.
_RHEMA_BENIGN_MUST_NOT_MATCH = (
    "https://owasp.org/www-project-web-security-testing-guide",
    "see the project documentation for testing methodology",
    "the hostname of the staging server is configured",
    "GET /api/v1/users HTTP/1.1 returned a 200 response",
    "report the finding to the operator for review",
    "the application uses standard authentication",
)


def validate_rhema_pattern(pattern: object) -> None:
    """Reject rhema reply-rewrite patterns that are dead labels or over-broad.

    Admission-time gate (not render-time): a pattern must compile as a regex,
    must not be a bare snake_case label masquerading as a matcher, must not
    match the empty string, and must not redact clearly-benign content.
    """
    import re

    raw = str(pattern or "").strip()
    prefixed = raw.lower().startswith("regex:")
    body = raw[len("regex:"):].strip() if prefixed else raw
    if not body:
        raise TemperControlError("rhema control pattern is empty")
    if not prefixed:
        has_meta = any(c in _REGEX_META for c in body)
        if not has_meta and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", body):
            raise TemperControlError(
                f"rhema control pattern {body!r} is a bare label, not a matcher; "
                "provide a regex (prefix with 'regex:') that matches the secret "
                "format it should redact"
            )
    try:
        rx = re.compile(body)
    except re.error as exc:
        raise TemperControlError(
            f"rhema control pattern is not a valid regex: {exc}"
        )
    if rx.match(""):
        raise TemperControlError(
            "rhema control pattern matches the empty string — far too broad"
        )
    for sample in _RHEMA_BENIGN_MUST_NOT_MATCH:
        if rx.search(sample):
            raise TemperControlError(
                "rhema control pattern is over-broad — it would redact benign "
                f"content such as {sample!r}; target the secret format instead"
            )

_CONTROL_CONTRACTS = {
    "absolute": {
        "required": ["principle", "allowed", "restricted", "exception_path"],
        "notes": "allowed and restricted must be non-empty lists of strings.",
    },
    "rhema": {
        "required": ["action", "layer", "pattern", "rationale"],
        "optional": ["replacement"],
        "actions": sorted(_RHEMA_ACTIONS),
        "notes": "replacement is required when action is rewrite or shadow_rewrite.",
    },
    "bulwark": {
        "required": ["action", "layer", "match", "exception_path"],
        "optional": ["condition"],
        "actions": sorted(_BULWARK_ACTIONS),
        "layers": sorted(_LAYERS),
    },
    "canary": {
        "required": ["signal", "pattern", "severity", "trip_when", "response"],
    },
    "approval_gate": {
        "required": ["surface", "trigger", "required_context", "approval_prompt"],
    },
    "alert_hook": {
        "required": ["event", "severity", "message"],
        "optional": ["route"],
    },
    "trim_template": {
        "required": ["task_family", "placeholders", "defaults", "operator_inputs"],
        "notes": (
            "placeholders and operator_inputs must be non-empty lists of strings; "
            "defaults must be an object."
        ),
    },
}
_PRINCIPLE_LABELS = {
    "secret_handling": "Secret-handling boundary",
    "methodology_fit": "Methodology-fit boundary",
    "safety_boundary": "Safety boundary",
    "prompt_mediation": "Prompt-mediation boundary",
    "operator_approval": "Operator-approval boundary",
    "alert_signal": "Alert-signal boundary",
    "runtime_boundary": "Runtime boundary",
    "trim_fit": "Trim-fit boundary",
}


class TemperControlError(ValueError):
    pass


def validate_thread_type(thread_type: object) -> str:
    value = str(thread_type or "").strip()
    if value not in THREAD_TYPES:
        raise TemperControlError(
            "thread_type must be one of: " + ", ".join(sorted(THREAD_TYPES))
        )
    return value


def validate_control(destination: str, control: object) -> dict:
    dest = str(destination or "").strip()
    if dest not in DESTINATIONS:
        raise TemperControlError(f"unknown temper destination: {destination!r}")
    if not isinstance(control, dict):
        raise TemperControlError(f"{dest} control must be an object")
    normalized = dict(control)
    validator = {
        "absolute": _validate_absolute,
        "rhema": _validate_rhema,
        "bulwark": _validate_bulwark,
        "canary": _validate_canary,
        "approval_gate": _validate_approval_gate,
        "alert_hook": _validate_alert_hook,
        "trim_template": _validate_trim_template,
    }[dest]
    validator(normalized)
    return normalized


def describe_control_contract(destination: str) -> dict:
    dest = str(destination or "").strip()
    if dest not in DESTINATIONS:
        return {
            "destination": dest,
            "required": [],
            "notes": "Unknown destination. Choose one of: "
            + ", ".join(sorted(DESTINATIONS)),
        }
    contract = dict(_CONTROL_CONTRACTS[dest])
    contract["destination"] = dest
    return contract


def render_control(
    destination: str,
    control: dict,
    *,
    citations: list[dict] | None = None,
    fallback_text: str = "",
) -> str:
    if not control:
        return fallback_text.rstrip() + ("\n" if fallback_text.strip() else "")
    normalized = validate_control(destination, control)
    renderer = {
        "absolute": _render_absolute,
        "rhema": _render_rhema,
        "bulwark": _render_bulwark,
        "canary": _render_canary,
        "approval_gate": _render_approval_gate,
        "alert_hook": _render_alert_hook,
        "trim_template": _render_trim_template,
    }[destination]
    text = renderer(normalized, citations or [])
    return text.rstrip() + "\n"


def _validate_required(control: dict, fields: tuple[str, ...], destination: str) -> None:
    missing = [
        field for field in fields
        if not _present(control.get(field))
    ]
    if missing:
        raise TemperControlError(
            f"{destination} control missing required fields: "
            + ", ".join(missing)
        )


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _require_list(control: dict, field: str, destination: str) -> None:
    value = control.get(field)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip()
        for item in value
    ):
        raise TemperControlError(
            f"{destination} control field {field} must be a non-empty list of strings"
        )


def _validate_absolute(control: dict) -> None:
    _validate_required(
        control, ("principle", "allowed", "restricted", "exception_path"),
        "absolute",
    )
    _require_list(control, "allowed", "absolute")
    _require_list(control, "restricted", "absolute")
    principle = str(control.get("principle") or "").strip()
    if principle in _PRINCIPLE_LABELS:
        control["principle"] = _PRINCIPLE_LABELS[principle]
    elif principle.lower().startswith("tempered armor principle"):
        control["principle"] = (
            principle.split(":", 1)[1].strip()
            if ":" in principle else "Armor temper boundary"
        )


def _validate_rhema(control: dict) -> None:
    _validate_required(control, ("action", "layer", "pattern", "rationale"), "rhema")
    action = str(control.get("action") or "").strip()
    if action not in _RHEMA_ACTIONS:
        raise TemperControlError(
            "rhema control action must be one of: "
            + ", ".join(sorted(_RHEMA_ACTIONS))
        )
    if action in {"rewrite", "shadow_rewrite"} and not _present(
        control.get("replacement")
    ):
        raise TemperControlError(
            "rhema control replacement is required for rewrite actions"
        )


def normalize_bulwark_match(match: object) -> str:
    """Make a bulwark match fire as the author intended.

    Bulwark treats a match as a regex ONLY when it is prefixed with
    ``regex:``; otherwise the string is ``re.escape``d and anchored, so a
    model-emitted alternation like ``jtag|uart|esptool`` would silently never
    match (it materializes as a dead rule). Repair rather than reject: if the
    match carries regex metacharacters and compiles as a valid regex, add the
    ``regex:`` prefix so bulwark enforces it. Plain literal tokens are left
    untouched — bulwark matches those correctly as-is.
    """
    import re

    raw = str(match or "").strip()
    if not raw or raw.lower().startswith("regex:"):
        return raw
    # If it carries regex metacharacters (other than comma) and compiles, it's
    # already a regex — prefix it. Checked BEFORE the comma-token-list path so a
    # quantifier like ``\S{6,}`` is never mis-split on its internal comma.
    non_comma_meta = _REGEX_META - {","}
    if any(ch in non_comma_meta for ch in raw):
        try:
            re.compile(raw)
            return "regex:" + raw
        except re.error:
            return raw  # not a valid regex; leave literal rather than guess
    # Plain comma-separated token list ("adb, fastboot, esptool") -> regex
    # alternation so it fires as any-of instead of one anchored literal.
    if "," in raw:
        toks = [t.strip() for t in raw.split(",") if t.strip()]
        if toks:
            alt = "|".join(toks)
            try:
                re.compile(alt)
                return "regex:" + alt
            except re.error:
                return raw
    return raw  # single literal token — enforced as-is


def _validate_bulwark(control: dict) -> None:
    _validate_required(
        control, ("action", "layer", "match", "exception_path"), "bulwark",
    )
    action = str(control.get("action") or "").strip()
    if action not in _BULWARK_ACTIONS:
        raise TemperControlError(
            "bulwark control action must be one of: "
            + ", ".join(sorted(_BULWARK_ACTIONS))
        )
    layer = str(control.get("layer") or "").strip()
    if layer not in _LAYERS:
        raise TemperControlError(
            "bulwark control layer must be one of: " + ", ".join(sorted(_LAYERS))
        )
    # Repair the match so it actually fires (regex alternations emitted without
    # the ``regex:`` prefix would otherwise materialize as no-op rules).
    match = normalize_bulwark_match(control.get("match"))
    control["match"] = match
    # A working bulwark match is a single command/tool token or a ``regex:``
    # pattern. A multi-word literal (prose phrase) is re.escaped + anchored and
    # never fires — reject so the author supplies a regex/token-list instead of
    # a dead rule that only looks enforced.
    if match and not match.lower().startswith("regex:") and len(match.split()) > 1:
        raise TemperControlError(
            "bulwark control match must be a 'regex:' pattern, a comma-separated "
            "token list, or a single command/tool token — not a prose phrase; "
            f"got {match!r}"
        )


def _validate_canary(control: dict) -> None:
    _validate_required(
        control, ("signal", "pattern", "severity", "trip_when", "response"),
        "canary",
    )


def _validate_approval_gate(control: dict) -> None:
    _validate_required(
        control,
        ("surface", "trigger", "required_context", "approval_prompt"),
        "approval_gate",
    )


def _validate_alert_hook(control: dict) -> None:
    _validate_required(control, ("event", "severity", "message"), "alert_hook")


def _validate_trim_template(control: dict) -> None:
    _validate_required(
        control,
        ("task_family", "placeholders", "defaults", "operator_inputs"),
        "trim_template",
    )
    _require_list(control, "placeholders", "trim_template")
    _require_list(control, "operator_inputs", "trim_template")
    if not isinstance(control.get("defaults"), dict):
        raise TemperControlError("trim_template control defaults must be an object")


def _comments(control: dict, citations: list[dict]) -> list[str]:
    lines: list[str] = []
    rationale = str(control.get("rationale") or "").strip()
    if rationale:
        lines.append(f"# {rationale}")
    condition = str(control.get("condition") or "").strip()
    if condition:
        lines.append(f"# condition: {condition}")
    exception = str(control.get("exception_path") or "").strip()
    if exception:
        lines.append(f"# exception: {exception}")
    for citation in citations:
        cite = ""
        if isinstance(citation, dict):
            cite = str(citation.get("manifest_element_id") or "").strip()
        if cite:
            lines.append(f"# cite: {cite}")
    return lines


def _render_absolute(control: dict, citations: list[dict]) -> str:
    heading = str(control["principle"]).strip() or "Armor temper boundary"
    context = control.get("methodology_context")
    applies_when = ""
    if isinstance(context, dict):
        task_family = str(context.get("task_family") or "").strip()
        if task_family:
            applies_when = f"- Applies when: `{task_family}` methodology context."
    lines = [
        f"## Tempered Armor: {heading}",
        "",
        f"- Principle: {control['principle']}",
        *( [applies_when] if applies_when else [] ),
        "- Allowed:",
        *[f"  - {item}" for item in control["allowed"]],
        "- Restricted for this context:",
        *[f"  - {item}" for item in control["restricted"]],
        f"- Exception path: {control['exception_path']}",
    ]
    cite_lines = _comments({}, citations)
    if cite_lines:
        lines.extend(["", *cite_lines])
    return "\n".join(lines)


def _render_rhema(control: dict, citations: list[dict]) -> str:
    lines = _comments(control, citations)
    replacement = str(control.get("replacement") or "").strip()
    if replacement:
        lines.append(f"# replacement: {replacement}")
    lines.append(f"{control['action']} {control['layer']} {control['pattern']}")
    return "\n".join(lines)


def _render_bulwark(control: dict, citations: list[dict]) -> str:
    lines = _comments(control, citations)
    lines.append(f"{control['action']} {control['layer']} {control['match']}")
    return "\n".join(lines)


def _render_canary(control: dict, citations: list[dict]) -> str:
    return _render_json_control("canary", control, citations)


def _render_approval_gate(control: dict, citations: list[dict]) -> str:
    return _render_json_control("approval_gate", control, citations)


def _render_alert_hook(control: dict, citations: list[dict]) -> str:
    return _render_json_control("alert_hook", control, citations)


def _render_trim_template(control: dict, citations: list[dict]) -> str:
    return _render_json_control("trim_template", control, citations)


def _render_json_control(destination: str, control: dict, citations: list[dict]) -> str:
    payload: dict[str, Any] = {"destination": destination, "control": control}
    if citations:
        payload["citations"] = citations
    return json.dumps(payload, indent=2, sort_keys=True)


def control_property_schemas() -> dict:
    """Per-destination JSON Schema for the ``control`` object.

    Used by the temper MCP tool to show the model exactly which fields and
    enum values each destination's control needs — far better guidance than a
    single opaque object, and the source of the ~30% control-shape rejects.
    Advisory only: ``validate_control`` remains the authoritative gate.
    Each sub-schema keeps ``additionalProperties: True`` so extra fields never
    block a valid candidate."""
    layers = sorted(_LAYERS)
    _str = {"type": "string"}
    _str_list = {"type": "array", "items": {"type": "string"}}
    return {
        "absolute": {
            "type": "object", "additionalProperties": True,
            "properties": {
                "principle": _str, "allowed": _str_list,
                "restricted": _str_list, "exception_path": _str,
            },
            "required": ["principle", "allowed", "restricted", "exception_path"],
        },
        "rhema": {
            "type": "object", "additionalProperties": True,
            "properties": {
                "action": {"enum": sorted(_RHEMA_ACTIONS)},
                "layer": {"enum": layers},
                "pattern": {
                    "type": "string",
                    "description": (
                        "regex that matches the secret format (e.g. "
                        "regex:Bearer [A-Za-z0-9._-]{20,}); not a bare label "
                        "and not over-broad"
                    ),
                },
                "rationale": _str,
                "replacement": {
                    "type": "string",
                    "description": "required when action is rewrite or shadow_rewrite",
                },
            },
            "required": ["action", "layer", "pattern", "rationale"],
        },
        "bulwark": {
            "type": "object", "additionalProperties": True,
            "properties": {
                "action": {"enum": sorted(_BULWARK_ACTIONS)},
                "layer": {"enum": layers},
                "match": _str, "exception_path": _str, "condition": _str,
            },
            "required": ["action", "layer", "match", "exception_path"],
        },
        "canary": {
            "type": "object", "additionalProperties": True,
            "properties": {
                "signal": _str, "pattern": _str, "severity": _str,
                "trip_when": _str, "response": _str,
            },
            "required": ["signal", "pattern", "severity", "trip_when", "response"],
        },
        "approval_gate": {
            "type": "object", "additionalProperties": True,
            "properties": {
                "surface": _str, "trigger": _str,
                "required_context": _str, "approval_prompt": _str,
            },
            "required": ["surface", "trigger", "required_context", "approval_prompt"],
        },
        "alert_hook": {
            "type": "object", "additionalProperties": True,
            "properties": {
                "event": _str, "severity": _str, "message": _str, "route": _str,
            },
            "required": ["event", "severity", "message"],
        },
        "trim_template": {
            "type": "object", "additionalProperties": True,
            "properties": {
                "task_family": _str, "placeholders": _str_list,
                "defaults": {"type": "object"}, "operator_inputs": _str_list,
            },
            "required": ["task_family", "placeholders", "defaults", "operator_inputs"],
        },
    }


__all__ = [
    "DESTINATIONS",
    "THREAD_TYPES",
    "SCOPE_TAGS",
    "TemperControlError",
    "control_property_schemas",
    "describe_control_contract",
    "render_control",
    "validate_control",
    "validate_rhema_pattern",
    "validate_thread_type",
]
