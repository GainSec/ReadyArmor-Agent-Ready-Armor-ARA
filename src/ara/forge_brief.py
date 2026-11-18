"""Auto-generated forge-v1 system-prompt brief.

When an engagement is mounted, the supervisor injects a brief into
every outbound LLM request explaining ARA's Hermes MCP tools and
listing the channels callable from the agent's currently active
chunk. This makes ARA-supervised agents (OpenClaw, Claude Code,
generic Node/HTTP agents) auto-aware of forge-v1 without any
operator-side prompt engineering.

The brief is regenerated per-request so chunk swaps are reflected
immediately — when the active chunk changes from probe → writeup,
the next outbound request's brief lists ``finding.file`` instead of
``lead.file`` etc.

Idempotent injection: a sentinel marker (``ARA_FORGE_BRIEF_V1``) is
embedded in the brief; if it's already present in the payload's
system text, no re-injection happens. This keeps re-mediated requests
stable and prevents repeated brief stacking.
"""
from __future__ import annotations

from typing import Any


_BRIEF_SENTINEL = "ARA_FORGE_BRIEF_V1"


_ALWAYS_AVAILABLE_HINT = """\
You are running under ARA forge-v1 supervision. Hermes exposes these as MCP
tools named ``mcp_ara_forge_*``. Use those tools directly.

Use them for *structured knowledge production* — facts, leads,
findings, anomalies, chunk-swap tags. The bridge handles
audit-logging, evidence binding, and freshness validation
automatically; you do not need to write JSON files yourself.

Canonical ARA channel names still appear in audit logs, but Hermes tool
calls use the exposed MCP names listed for the active chunk below.
"""


_TASK_AVAILABLE_HINT = """\
You are running under ARA task supervision. Hermes exposes ARA runtime channels
as MCP tools named ``mcp_ara_forge_*``. Use those tools directly.

Use them for *structured engagement records* — facts, leads, findings,
evidence lookups, and chunk-swap tags. The bridge handles audit-logging,
evidence binding, and freshness validation automatically; you do not need to
write JSON files yourself.

Canonical ARA channel names still appear in audit logs, but Hermes tool calls
use the exposed MCP names listed for the active chunk below.
"""


_CHANNEL_USAGE = {
    "fact.assert": (
        "When you observe a structured fact (a predicate-value "
        "pair you can cite to evidence), call Hermes tool "
        "`mcp_ara_forge_fact_assert` with arguments:\n"
        "  { predicate: 'target.tech.cms', value: 'wordpress',\n"
        "    asserted_by_crumb: '<crumb_id>',\n"
        "    evidence_refs: ['e:abcdef0123'] }"
    ),
    "lead.file": (
        "When you have a hypothesis worth tracking, call Hermes tool "
        "`mcp_ara_forge_lead_file` with arguments:\n"
        "  { title: 'Possible IDOR on /api/orders/{id}',\n"
        "    reasoning: 'Sequential ids; no auth check seen.',\n"
        "    evidence_refs: ['e:...'],\n"
        "    derived_from_crumb: '<crumb_id>' }"
    ),
    "finding.file": (
        "When you've confirmed a finding (only valid in writeup-class "
        "chunks), call Hermes tool `mcp_ara_forge_finding_file` with "
        "arguments:\n"
        "  { title: '...', category: '<from category alphabet>',\n"
        "    severity_cell: ['<data_class>', '<access>'],\n"
        "    impact: { confidentiality: 0..3, integrity: 0..3, availability: 0..3 },\n"
        "    evidence_refs: ['e:...'], lead_refs: ['L:...'],\n"
        "    provenance_chain: ['<crumb_id>', '<fact_id>', '<lead_id>'],\n"
        "    fix_template_id: '<from fix_library>' OR fix_custom: {...},\n"
        "    origin: 'agent_derived',\n"
        "    affected_assets: ['target.host'] }"
    ),
    "facts.query": (
        "Read structured facts asserted earlier with Hermes tool "
        "`mcp_ara_forge_facts_query` when exposed, or use `facts.query` "
        "only if the runtime exposes that exact tool."
    ),
    "lead.investigate": (
        "Operator-only: marks a lead as actively under investigation."
    ),
    "finding.sign_off": (
        "Operator-only: locks a draft finding for inclusion in reports."
    ),
    "chunks.emit_tag": (
        "Swap to a different chunk by calling Hermes tool "
        "`mcp_ara_forge_chunks_emit_tag` with arguments "
        "{ tag: '<readied_for_writeup>' }.\n"
        "Or you may emit ``<exit_tag>`` inline in your prose; "
        "ARA scans assistant turns and fires the swap automatically."
    ),
    "anomaly.emit": (
        "Anomalies fire automatically when tool output diverges from "
        "the active crumb's expected shape; you do not call this "
        "channel directly."
    ),
    "evidence.search": (
        "Search captured evidence by calling Hermes tool "
        "`mcp_ara_forge_evidence_search` with arguments { query: 'sqli' }. "
        "This is a lookup channel; it does not clear task action debt. "
        "Use fact.assert, lead.file, finding.file, or chunks.emit_tag after "
        "the lookup to record progress."
    ),
    "evidence.get": (
        "Fetch captured bytes by calling Hermes tool "
        "`mcp_ara_forge_evidence_get` with arguments { evidence_id: 'e:...' }. "
        "This is a lookup channel; it does not clear task action debt."
    ),
}


def build_brief(
    *,
    chunk_id: str | None,
    chunk_title: str | None,
    chunk_prompt_fragment: str | None,
    tool_surface: list[str],
    exits: dict[str, str],
    alphabet_pack_name: str | None,
    weapon_id: str | None,
    resources: list[dict] | None = None,
    available_channels: list[str] | tuple[str, ...] | None = None,
    categories: list[dict] | None = None,
    runtime_mode: str = "forge",
) -> str:
    """Compose the full brief for the current engagement state."""
    is_task_mode = str(runtime_mode).strip().lower() in {"task", "engagement"}
    available = set(str(ch) for ch in available_channels) if available_channels else None
    lines: list[str] = [
        f"<!-- {_BRIEF_SENTINEL} -->",
        "",
        (_TASK_AVAILABLE_HINT if is_task_mode else _ALWAYS_AVAILABLE_HINT).strip(),
        "",
    ]
    if weapon_id:
        lines.append(f"Weapon equipped: `{weapon_id}`"
                     + (f" (alphabet pack: `{alphabet_pack_name}`)"
                        if alphabet_pack_name else ""))
        lines.append("")
    if categories:
        catlines: list[str] = []
        for c in categories:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "").strip()
            if not cid:
                continue
            label = str(c.get("label") or "").strip()
            catlines.append(f"- `{cid}`" + (f" — {label}" if label else ""))
        if catlines:
            lines.append("## Vulnerability categories in scope")
            lines.append("")
            lines.append(
                "The vulnerability classes this weapon hunts. While "
                "enumerating and probing, work through these against the "
                "target's surface — map which are plausible given what you "
                "observe, then investigate the strongest. Do not stop at "
                "generic recon: each category is a concrete hypothesis to "
                "test. Use one of these ids as the `category` when you file a "
                "finding."
            )
            lines.append("")
            lines.extend(catlines)
            lines.append("")
    lines.append("## Phase gate and target traffic")
    lines.append("")
    lines.append(
        "Phase completion requires structured ARA records. Assert verified "
        "observations with fact.assert, file follow-up hypotheses with "
        "lead.file, file confirmed reportable issues with finding.file, "
        "and bind captured proof through evidence channels when available; "
        "a prose report is not a substitute for structured channel output."
    )
    lines.append("")
    lines.append(
        "Do not send target URLs to web_extract, web_search, or any external "
        "third-party extraction/search tool. Target traffic must use contained "
        "browser/terminal paths by default. External search or extraction is "
        "allowed only for forge-sealed resources or explicit operator approval."
    )
    lines.append("")
    if resources:
        lines.append("## Weapon resources")
        lines.append("")
        lines.append(
            "These forge-sealed resources may be referenced or fetched "
            "when needed for this task. Execution, scope expansion, or "
            "aggressive tooling still requires approval."
        )
        lines.append("")
        for res in resources[:20]:
            if not isinstance(res, dict):
                continue
            rid = str(res.get("id") or "resource")
            cls = str(res.get("class") or res.get("resource_class") or "reference")
            name = str(res.get("name") or rid)
            locator = str(res.get("locator") or res.get("url") or "")
            status = str(res.get("status") or ("resolved" if locator else "needs_resolution"))
            allowed = res.get("allowed_uses") or []
            approvals = res.get("requires_approval_for") or []
            hints = res.get("hints") or []
            if not isinstance(allowed, list):
                allowed = [allowed]
            if not isinstance(approvals, list):
                approvals = [approvals]
            if not isinstance(hints, list):
                hints = [hints]
            parts = [f"- `{rid}` [{cls}] {name}"]
            parts.append(f"  status: {status}")
            if locator:
                parts.append(f"  locator: {locator}")
            elif status != "resolved":
                parts.append(
                    "  unresolved: no network access is granted yet; "
                    "operator resolution is required before fetch/use."
                )
            if hints:
                parts.append("  hints: " + ", ".join(str(x) for x in hints))
            if allowed:
                parts.append("  allowed: " + ", ".join(str(x) for x in allowed))
            if approvals:
                parts.append("  approval: " + ", ".join(str(x) for x in approvals))
            lines.extend(parts)
        if len(resources) > 20:
            lines.append(f"- ... {len(resources) - 20} more sealed resources")
        lines.append("")
    if chunk_id:
        lines.append(f"## Active chunk: `{chunk_id}`"
                     + (f" — {chunk_title}" if chunk_title else ""))
        lines.append("")
        if chunk_prompt_fragment:
            lines.append(chunk_prompt_fragment.strip())
            lines.append("")
        visible_tool_surface = [
            str(ch) for ch in tool_surface
            if available is None or str(ch) in available
        ]
        if visible_tool_surface:
            lines.append("### Channels callable now")
            lines.append("")
            for ch in visible_tool_surface:
                hint = _CHANNEL_USAGE.get(ch)
                if hint:
                    lines.append(f"- `{ch}` — {hint}")
                else:
                    lines.append(f"- `{ch}`")
            lines.append("")
        # v0.1 §8 — crumb-intent gate. Tell the agent to call
        # crumb.intent BEFORE firing any procedure's tools. Knowledge
        # already covered in mats / prior crumbs returns skip; the
        # agent should move on rather than re-running redundant recon.
        lines.append("### Pre-fire knowledge-gap gate")
        lines.append("")
        lines.append(
            "If Hermes exposes `mcp_ara_forge_crumb_intent`, call it before "
            "running tools for a procedure (crumb). If that tool is not "
            "present, proceed with the active chunk tools listed above."
        )
        lines.append("")
        lines.append(
            "Example arguments for `mcp_ara_forge_crumb_intent` when exposed: "
            "{ crumb_id: '<the-procedure-name>', produces: "
            "['<fact_shape_1>', '<fact_shape_2>'] }."
        )
        lines.append("")
        lines.append(
            "Skip means the runtime has logged from-mats evidence "
            "for this crumb. Don't re-run; pick the next procedure."
        )
        lines.append("")
        if exits:
            lines.append("### Chunk exits (emit any of these to swap)")
            lines.append("")
            for tag, target in exits.items():
                lines.append(f"- `{tag}` → `{target}`")
            lines.append("")
            lines.append("Emit a tag inline in your prose (ARA scans "
                         "assistant turns) or call `chunks.emit_tag`.")
            lines.append("")
    lines.append(
        "Channel calls are validated, audit-logged, and rejected if "
        "the body is malformed or the channel isn't in the active "
        "chunk's tool_surface."
    )
    return "\n".join(lines)


def already_briefed(text: str) -> bool:
    return _BRIEF_SENTINEL in (text or "")


# ---------- payload shape adaptors ----------

def inject_into_payload(payload: Any, brief: str) -> Any:
    """Return a new payload with the brief injected as a system
    message. Idempotent: if the payload already contains the brief
    sentinel, returns the payload unchanged.

    Supports three common shapes:
    - Anthropic Messages API: ``{"system": "...", "messages": [...]}``
      The brief is prepended to ``system``.
    - OpenAI-style: ``{"messages": [{"role": "system", ...}, ...]}``
      The brief is prepended to the first system message, or a new
      system message is inserted at index 0.
    - Plain dict with ``"system"`` field: same as Anthropic.

    Other shapes pass through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    new = dict(payload)
    # Anthropic / generic system field
    sys = new.get("system")
    if sys is not None:
        if isinstance(sys, str):
            if already_briefed(sys):
                return payload
            new["system"] = brief + "\n\n" + sys
            return new
        if isinstance(sys, list):
            joined = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in sys
            )
            if already_briefed(joined):
                return payload
            new["system"] = [{"type": "text", "text": brief}] + list(sys)
            return new
    # OpenAI-style messages
    msgs = new.get("messages")
    if isinstance(msgs, list):
        joined = "\n".join(
            (m.get("content") if isinstance(m.get("content"), str)
             else "")
            for m in msgs if isinstance(m, dict) and m.get("role") == "system"
        )
        if already_briefed(joined):
            return payload
        # Find first system message and prepend; else insert one
        new_msgs = []
        injected = False
        for m in msgs:
            if not injected and isinstance(m, dict) and m.get("role") == "system":
                content = m.get("content") or ""
                if isinstance(content, str):
                    new_msgs.append({**m, "content": brief + "\n\n" + content})
                else:
                    new_msgs.append(m)  # unusual shape, pass through
                injected = True
            else:
                new_msgs.append(m)
        if not injected:
            new_msgs = [{"role": "system", "content": brief}] + list(msgs)
        new["messages"] = new_msgs
        return new
    # Plain string content under content key — wrap as a system note
    if isinstance(new.get("content"), str) and not already_briefed(new["content"]):
        new["content"] = brief + "\n\n" + new["content"]
        return new
    return new
