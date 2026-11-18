"""Capture agent tool call + output pairs from the MITM-observed LLM stream.

Each provider chat request carries the conversation so far: assistant messages
with ``tool_calls`` (the command the agent ran) and ``role:"tool"`` messages with
the result (openclaw sends ``{"output": "..."}``). Pairing them by
``tool_call_id`` yields the tool call + its raw output — the *universal* capture
of what the agent actually did and observed, including in-process work (e.g. a
``python -c`` urllib request) that the syscall/subprocess capture path misses.

Bound into the existing evidence binder as ``tool:<name>`` sources (reusing
``sources.register_tool_source``); content-addressed, so re-seeing the same
exchange in later turns is idempotent. Best-effort: every failure is swallowed —
capture must never affect the request path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolExchange:
    tool_call_id: str | None
    tool_name: str | None
    command: str | None
    arguments: Any
    output: str


def _coerce_text(content: Any) -> str:
    """Pull the tool result's raw output text out of ``content``. Handles
    openclaw's ``{"output": "..."}`` JSON-string wrapper, Anthropic-style content
    part lists, and plain strings."""
    if isinstance(content, list):
        parts: list[str] = []
        for it in content:
            if isinstance(it, dict) and "text" in it:
                parts.append(str(it["text"]))
            elif isinstance(it, str):
                parts.append(it)
        content = "\n".join(parts)
    if not isinstance(content, str):
        return ""
    s = content.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "output" in obj:
            return str(obj["output"])
    return content


def extract_tool_exchanges(req_json: Any) -> list[ToolExchange]:
    """Pair assistant ``tool_calls`` with their ``role:"tool"`` results from an
    OpenAI-style chat request. One ``ToolExchange`` per tool result seen."""
    if not isinstance(req_json, dict):
        return []
    msgs = req_json.get("messages")
    if not isinstance(msgs, list):
        return []

    calls: dict[str, dict] = {}
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            cid = tc.get("id")
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            args_raw = fn.get("arguments")
            args_obj: Any = None
            if isinstance(args_raw, str):
                try:
                    args_obj = json.loads(args_raw)
                except ValueError:
                    args_obj = None
            elif isinstance(args_raw, dict):
                args_obj = args_raw
            command = args_obj.get("command") if isinstance(args_obj, dict) else None
            if cid:
                calls[cid] = {
                    "name": fn.get("name"),
                    "command": command if isinstance(command, str) else None,
                    "arguments": args_obj if args_obj is not None else args_raw,
                }

    out: list[ToolExchange] = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        cid = m.get("tool_call_id")
        call = (calls.get(cid) if cid else None) or {}
        out.append(ToolExchange(
            tool_call_id=cid,
            tool_name=call.get("name") or m.get("name"),
            command=call.get("command"),
            arguments=call.get("arguments"),
            output=_coerce_text(m.get("content")),
        ))
    return out


def capture_tool_exchanges(
    session_dir: Any, req_json: Any, *, activation_id: str | None = None
) -> int:
    """Bind every tool call+output exchange in ``req_json`` into the evidence
    binder as ``tool:<name>``. Idempotent by content hash (re-seeing the same
    exchange in a later turn binds nothing new). Returns the count of exchanges
    with output. Best-effort — never raises."""
    try:
        exchanges = extract_tool_exchanges(req_json)
    except Exception:  # noqa: BLE001
        return 0
    if not exchanges:
        return 0
    try:
        from ara.runtime.binder.api import EvidenceBinder
        from ara.runtime.binder.sources import register_tool_source
    except Exception:  # noqa: BLE001
        return 0

    bound = 0
    binder = None
    try:
        binder = EvidenceBinder(Path(session_dir))
        for ex in exchanges:
            if not ex.output:
                continue
            tool = (ex.tool_name or "unknown").strip() or "unknown"
            try:
                register_tool_source(tool)
                doc = {
                    "tool_call_id": ex.tool_call_id,
                    "tool_name": tool,
                    "command": ex.command,
                    "arguments": ex.arguments,
                    "output": ex.output,
                }
                meta: dict[str, Any] = {}
                if ex.command:
                    meta["args"] = ex.command[:2000]
                label = (ex.command or ex.output or "").strip()[:80]
                binder.put(
                    json.dumps(doc, sort_keys=True, default=str).encode("utf-8"),
                    source=f"tool:{tool}",
                    source_metadata=meta,
                    summary=f"tool {tool}: {label}",
                    content_type="application/json",
                    activation_id=activation_id,
                )
                bound += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return bound
    finally:
        try:
            if binder is not None:
                binder.close()
        except Exception:  # noqa: BLE001
            pass
    return bound
