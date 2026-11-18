"""ARA Temper MCP server.

This bridge gives Tailor/Needle runs a narrow MCP surface for proposing
Temper changes. Tools append candidates to the active Tailor session for
operator review; they do not modify live armor, Bulwark, Rhema, canary,
alert, approval, or trim files.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from ara import temper_controls, temper_lifecycle


PROTOCOL_VERSION = "2024-11-05"

_DESTINATIONS = [
    "absolute",
    "bulwark",
    "canary",
    "rhema",
    "approval_gate",
    "alert_hook",
    "trim_template",
]

_REQUIRED = [
    "destination",
    "thread_type",
    "title",
    "source",
    "rationale",
    "risk",
    "default_decision",
    "proposed_text",
    "control",
    "citations",
]

_CANDIDATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "enum": _DESTINATIONS,
            "description": (
                "Which armor surface this candidate targets. The control "
                "object's required fields depend on this: absolute needs "
                "principle/allowed/restricted/exception_path; bulwark needs "
                "action/layer/match/exception_path; rhema needs action/layer/"
                "pattern/rationale (+replacement for rewrites); canary needs "
                "signal/pattern/severity/trip_when/response; approval_gate needs "
                "surface/trigger/required_context/approval_prompt; alert_hook "
                "needs event/severity/message; trim_template needs task_family/"
                "placeholders/defaults/operator_inputs."
            ),
        },
        "thread_type": {
            "type": "string",
            "enum": [
                "secret_handling",
                "methodology_fit",
                "safety_boundary",
                "prompt_mediation",
                "operator_approval",
                "alert_signal",
                "runtime_boundary",
                "trim_fit",
            ],
            "description": (
                "Tailor thread this candidate belongs to. This names the "
                "durable armor concern before destination rendering."
            ),
        },
        "title": {
            "type": "string",
            "description": "Short operator-facing name for the candidate.",
        },
        "source": {
            "type": "string",
            "description": (
                "Material, manifest element, or forge artifact this candidate "
                "was derived from."
            ),
        },
        "rationale": {
            "type": "string",
            "description": (
                "Why the candidate keeps the agent on task without expanding "
                "static scope, gateway, or trim responsibilities."
            ),
        },
        "risk": {
            "type": "string",
            "description": "What failure mode this candidate is meant to prevent.",
        },
        "default_decision": {
            "type": "string",
            "description": (
                "Suggested review default. Use suggest unless the material "
                "clearly demands deny, approval, or canary handling."
            ),
        },
        "proposed_text": {
            "type": "string",
            "description": (
                "Human-readable preview for review. The machine-readable "
                "control object is authoritative for rendering."
            ),
        },
        "control": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Destination-specific control object. Rhema requires action, "
                "layer, pattern, rationale, and replacement for rewrites. "
                "Bulwark requires action, layer, match, and exception_path. "
                "Canary requires signal, pattern, severity, trip_when, and "
                "response. Approval gates require surface, trigger, "
                "required_context, and approval_prompt. Alerts require event, "
                "severity, and message. Absolutes require principle, allowed, "
                "restricted, and exception_path. Trim templates require "
                "task_family, placeholders, defaults, and operator_inputs."
            ),
        },
        "scope": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional runtime routing scope. Use normalized task-family "
                "tags such as web_app, mobile, hardware, rf_research, "
                "anti_crime, or global. Non-global controls are only projected "
                "into matching Run Task contexts."
            ),
        },
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
            },
            "minItems": 1,
            "description": (
                "Non-empty evidence objects, ideally with manifest_element_id, "
                "source span, and short quote/snippet."
            ),
        },
        "methodology_context": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_family": {
                    "type": "string",
                    "description": (
                        "Methodology family the source implies, such as "
                        "web_app, mobile, firmware, hardware, cloud, or mixed."
                    ),
                },
                "expected": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Actions, tools, or evidence handling normally expected "
                        "for this methodology family."
                    ),
                },
                "unexpected": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Actions, tools, paths, commands, or techniques that "
                        "are unexpected for this methodology family."
                    ),
                },
                "requires_context": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required active trim, weapon, operator approval, or "
                        "task mode that makes an otherwise unexpected action valid."
                    ),
                },
                "never_without_approval": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Destructive or cross-methodology actions that need "
                        "explicit operator approval even when context is close."
                    ),
                },
                "exception_path": {
                    "type": "string",
                    "description": (
                        "How an operator can intentionally allow the action, "
                        "for example by selecting a firmware trim or approving "
                        "a single command."
                    ),
                },
            },
            "required": ["task_family", "exception_path"],
            "description": (
                "Optional structured methodology-fit context. Required when "
                "a candidate would otherwise read as a broad instruction, "
                "playbook, workflow, or reproduction-step ban. For task_family "
                "web_app, broad web testing methods such as browser automation, "
                "web scanning, web port scanning, XSS testing, SQL injection "
                "testing, proxy-based testing, HTTP request/response review, "
                "and API testing are expected methodology; restrict only "
                "narrow out-of-scope, unapproved, destructive, or cross-context "
                "use."
            ),
        },
        "safety_constraints": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "surfaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Armor surfaces affected by this safety constraint, "
                        "such as fs, exec, net, url, env, reply, ask, or acp."
                    ),
                },
                "allowed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Evidence locations, tools, or actions normally allowed "
                        "for this armor/task family."
                    ),
                },
                "restricted": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Paths, commands, domains, environment names, processes, "
                        "or actions that should be denied or approval-gated."
                    ),
                },
                "requires_context": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Trim facts, weapon identity, operator approval, or task "
                        "mode required before restricted access is allowed."
                    ),
                },
                "never_without_approval": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Actions that always need explicit approval even when "
                        "some context is present."
                    ),
                },
                "exception_path": {
                    "type": "string",
                    "description": (
                        "How an operator can intentionally allow the restricted "
                        "surface, for example by choosing a host-review trim or "
                        "approving a single access."
                    ),
                },
            },
            "required": ["surfaces", "exception_path"],
            "description": (
                "Optional reusable armor safety hardening. Use for constraints "
                "such as /etc/**, /root/**, ~/.ssh/**, /dev/**, /sbin/**, "
                "secret stores, destructive actions, or host/system access that "
                "is normally unnecessary for the task family."
            ),
        },
    },
    "required": _REQUIRED,
    "additionalProperties": False,
}

# Per-destination control sub-schemas (D-followup): show the model exactly
# which fields + enum values each destination's control needs, instead of one
# opaque object. JSON-Schema if/then keyed on destination. Advisory guidance —
# validate_control() remains authoritative; sub-schemas keep
# additionalProperties:True so a valid candidate is never blocked.
_CANDIDATE_INPUT_SCHEMA["allOf"] = [
    {
        "if": {
            "properties": {"destination": {"const": dest}},
            "required": ["destination"],
        },
        "then": {"properties": {"control": control_schema}},
    }
    for dest, control_schema in temper_controls.control_property_schemas().items()
]

_COVERAGE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": True,
            },
            "description": (
                "Source-by-source completion matrix. Each source entry should "
                "include source_id and a destinations object covering canary, "
                "rhema, absolute, approval_gate, alert_hook, bulwark, and "
                "trim_template. Emitted cells name candidate_ids; skipped cells "
                "include a reason."
            ),
        },
        "notes": {
            "type": "string",
            "description": "Short operator-facing summary of coverage decisions.",
        },
    },
    "required": ["sources"],
    "additionalProperties": False,
}

_TOOL_DESTINATIONS = {
    "temper.absolute_candidate": "absolute",
    "temper.bulwark_candidate": "bulwark",
    "temper.canary_candidate": "canary",
    "temper.rhema_candidate": "rhema",
    "temper.approval_gate_candidate": "approval_gate",
    "temper.alert_hook_candidate": "alert_hook",
    "temper.trim_template_candidate": "trim_template",
}

_TOOL_DESCRIPTIONS = {
    "temper.absolute_candidate": (
        "Propose an armor absolute that should always constrain the agent for "
        "this task family. Prefer expected-methodology posture over broad "
        "instruction bans."
    ),
    "temper.bulwark_candidate": (
        "Propose an enforceable Bulwark runtime boundary candidate. Use only "
        "for derived methodology-fit controls such as file, command, URL, "
        "permission, or tool boundaries that are expected, unexpected, or "
        "approval-only for the task family; not static network, gateway, shim, "
        "or time-of-day scope."
    ),
    "temper.canary_candidate": (
        "Propose a canary candidate that should trip when an agent drifts "
        "toward disallowed disclosure or task expansion."
    ),
    "temper.rhema_candidate": (
        "Propose an ask/reply prompt-mediation candidate: a rewrite, deny, "
        "shadow rewrite, or reply redaction rule that keeps agent language on "
        "task before output reaches the operator."
    ),
    "temper.approval_gate_candidate": (
        "Propose an operator approval gate candidate for risky but sometimes "
        "valid task actions, especially actions that are unexpected for the "
        "current methodology but valid when a matching trim or weapon is active."
    ),
    "temper.alert_hook_candidate": (
        "Propose an alert candidate for events operators should see when a "
        "task-specific boundary is approached."
    ),
    "temper.trim_template_candidate": (
        "Propose a reusable trim template candidate only when the material "
        "implies optional operator-selected task framing."
    ),
}

_SUMMARY_TOOL = "temper.coverage_summary"
_SUMMARY_TOOL_DESCRIPTION = (
    "Write the final structured coverage matrix after candidate emission. "
    "Every source/destination cell must be emitted with candidate_ids or "
    "skipped with a reason; proposals cannot be promoted without this coverage."
)

# A single candidate tool with a required ``destination`` enum replaces the
# seven near-identical per-destination tools. The shared schema is sent once
# (not 7x), and the model picks the destination in-payload — it can no longer
# hallucinate a tool name or be fuzzy-repaired onto the wrong destination.
_CANDIDATE_TOOL = "temper.candidate"
_CANDIDATE_TOOL_DESCRIPTION = (
    "Propose one reviewable Temper candidate. Set ``destination`` to the armor "
    "surface it targets:\n"
    "- absolute: armor doctrine that should always constrain the agent for a "
    "task family (prefer expected-methodology posture over broad bans).\n"
    "- bulwark: an enforceable runtime boundary (file/command/url/permission/"
    "tool) that is expected, unexpected, or approval-only for the task family.\n"
    "- canary: a signal that should trip when the agent drifts toward "
    "disallowed disclosure or task expansion.\n"
    "- rhema: an ask/reply prompt-mediation rule (rewrite/deny/shadow rewrite) "
    "that keeps agent language on task before output reaches the operator.\n"
    "- approval_gate: an operator approval gate for risky-but-sometimes-valid "
    "actions that are unexpected for the current methodology.\n"
    "- alert_hook: an alert for events operators should see when a "
    "task-specific boundary is approached.\n"
    "- trim_template: reusable operator-selected task framing.\n"
    "One candidate per call. methodology_fit threads belong on absolute or "
    "bulwark, not alert_hook."
)

# D2/D3 grounding: Needle reads the source forge's manifest elements through
# these tools, and every candidate must cite a real element id (verified at
# record time). Without this Needle had no way to read the mats at all.
_MATS_SEARCH_TOOL = "temper.mats_search"
_MATS_GET_TOOL = "temper.mats_get"

_MATS_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Substring to find in the source manifest elements. Empty "
                "returns the first elements. Use to locate the material before "
                "proposing a candidate."
            ),
        },
        "limit": {"type": "integer", "description": "Max results (default 12)."},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_MATS_GET_SCHEMA = {
    "type": "object",
    "properties": {
        "manifest_element_id": {
            "type": "string",
            "description": (
                "Fetch the full text of one source manifest element. Cite this "
                "exact id in any candidate derived from it."
            ),
        },
    },
    "required": ["manifest_element_id"],
    "additionalProperties": False,
}

_TOOLS: list[dict] = [
    {
        "name": _MATS_SEARCH_TOOL,
        "description": (
            "Search the source forge's manifest elements (the deposited mats) "
            "by substring. Read the material here before proposing candidates."
        ),
        "inputSchema": _MATS_SEARCH_SCHEMA,
    },
    {
        "name": _MATS_GET_TOOL,
        "description": (
            "Fetch the full text of one source manifest element. Every "
            "candidate must cite a real manifest_element_id from these mats."
        ),
        "inputSchema": _MATS_GET_SCHEMA,
    },
    {
        "name": _CANDIDATE_TOOL,
        "description": _CANDIDATE_TOOL_DESCRIPTION,
        "inputSchema": _CANDIDATE_INPUT_SCHEMA,
    },
    {
        "name": _SUMMARY_TOOL,
        "description": _SUMMARY_TOOL_DESCRIPTION,
        "inputSchema": _COVERAGE_INPUT_SCHEMA,
    },
]


def tool_destination(name: str) -> str:
    """Back-compat: map a legacy per-destination tool name to its destination.

    The live surface is the single ``temper.candidate`` tool (destination is an
    argument). This shim keeps the old ``temper.<dest>_candidate`` names
    resolvable for any caller that still uses them.
    """
    try:
        return _TOOL_DESTINATIONS[name]
    except KeyError as exc:
        raise temper_lifecycle.TailorLifecycleError(
            f"unknown temper MCP tool: {name!r}"
        ) from exc


def _validate_arguments(args: dict) -> None:
    missing = [name for name in _REQUIRED if name not in args]
    if missing:
        raise temper_lifecycle.TailorLifecycleError(
            "temper MCP call missing required fields: " + ", ".join(missing)
        )
    unexpected = sorted(set(args) - set(_CANDIDATE_INPUT_SCHEMA["properties"]))
    if unexpected:
        raise temper_lifecycle.TailorLifecycleError(
            "temper MCP call has unsupported fields: " + ", ".join(unexpected)
        )


def _validate_coverage_arguments(args: dict) -> None:
    if "sources" not in args:
        raise temper_lifecycle.TailorLifecycleError(
            "temper coverage summary missing required field: sources"
        )
    unexpected = sorted(set(args) - set(_COVERAGE_INPUT_SCHEMA["properties"]))
    if unexpected:
        raise temper_lifecycle.TailorLifecycleError(
            "temper coverage summary has unsupported fields: " + ", ".join(unexpected)
        )


def call_tool(state_root: Path, name: str, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise temper_lifecycle.TailorLifecycleError(
            "temper MCP arguments must be an object"
        )
    if name == _SUMMARY_TOOL:
        _validate_coverage_arguments(arguments)
        return temper_lifecycle.add_tailor_coverage_summary(
            Path(state_root), dict(arguments),
        )
    if name == _MATS_SEARCH_TOOL:
        return temper_lifecycle.search_source_mats(
            Path(state_root),
            str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 12),
        )
    if name == _MATS_GET_TOOL:
        return temper_lifecycle.get_source_mat(
            Path(state_root),
            str(arguments.get("manifest_element_id") or ""),
        )
    candidate = dict(arguments)
    if name != _CANDIDATE_TOOL:
        # Back-compat: a legacy per-destination tool name supplies the
        # destination; the live single tool carries it in the arguments.
        candidate["destination"] = tool_destination(name)
    try:
        destination = candidate.get("destination")
        if destination not in _DESTINATIONS:
            raise temper_lifecycle.TailorLifecycleError(
                "temper candidate destination must be one of: "
                + ", ".join(_DESTINATIONS)
            )
        _validate_arguments(candidate)
    except temper_lifecycle.TailorLifecycleError as exc:
        rejection = temper_lifecycle.record_tailor_candidate_rejection(
            Path(state_root), candidate, str(exc), tool_name=name,
        )
        raise temper_lifecycle.TailorCandidateRejected(
            str(exc), rejection,
        ) from exc
    return temper_lifecycle.add_tailor_candidate(Path(state_root), candidate)


def _rejection_result(rejection: dict) -> dict:
    text = (
        "VALIDATION REJECTED (ara-temper server is healthy — this is a schema "
        "validation error, NOT a connectivity/server outage). Correct the "
        "candidate and resend the same tool call.\n\n"
        f"{rejection.get('error')}\n\n"
        f"{rejection.get('correction', {}).get('instruction')}\n\n"
        "Expected control contract:\n"
        + json.dumps(
            rejection.get("correction", {}).get("expected_control") or {},
            indent=2,
            sort_keys=True,
        )
    )
    payload = {
        "ok": False,
        "error_kind": "temper_candidate_rejected",
        "rejected": rejection,
        "correction": rejection.get("correction") or {},
    }
    return {
        "content": [{
            "type": "text",
            "text": text,
        }],
        "isError": True,
        "structuredContent": payload,
    }


def _state_root_from_env() -> Path:
    raw = os.environ.get("ARA_STATE_ROOT") or os.environ.get("ARA_STATE_DIR")
    return Path(raw) if raw else Path(".ara")


class _Server:
    def __init__(self, state_root: Path | None = None) -> None:
        self.initialized = False
        self.state_root = Path(state_root) if state_root else _state_root_from_env()

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin,
        )
        while True:
            line = await reader.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            self._respond(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ara-temper", "version": "0.1.0"},
            })
            return
        if method == "notifications/initialized":
            self.initialized = True
            return
        if method == "tools/list":
            self._respond(msg_id, {"tools": _TOOLS})
            return
        if method == "tools/call":
            self._tool_call(msg_id, msg.get("params") or {})
            return
        if method is None and msg_id is not None:
            return
        if msg_id is not None:
            self._error(msg_id, -32601, f"method not found: {method}")

    def _tool_call(self, msg_id: Any, params: dict) -> None:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            self._error(msg_id, -32602, "tools/call: missing/invalid name")
            return
        try:
            result = call_tool(self.state_root, name, args)
        except temper_lifecycle.TailorCandidateRejected as e:
            self._respond(msg_id, _rejection_result(e.rejection))
            return
        except temper_lifecycle.TailorLifecycleError as e:
            self._error(msg_id, -32000, str(e))
            return
        except Exception as e:  # noqa: BLE001
            self._error(msg_id, -32603, f"internal: {type(e).__name__}: {e}")
            return
        self._respond(msg_id, {
            "content": [{
                "type": "text",
                "text": json.dumps(result, indent=2),
            }],
            "isError": False,
            "structuredContent": result,
        })

    def _respond(self, msg_id: Any, result: Any) -> None:
        if msg_id is None:
            return
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(
        self,
        msg_id: Any,
        code: int,
        message: str,
        *,
        data: Any = None,
    ) -> None:
        if msg_id is None:
            return
        err: dict = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"jsonrpc": "2.0", "id": msg_id, "error": err})

    def _write(self, msg: dict) -> None:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def run() -> int:
    try:
        asyncio.run(_Server().serve())
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[temper-mcp] fatal: {type(e).__name__}: {e}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
