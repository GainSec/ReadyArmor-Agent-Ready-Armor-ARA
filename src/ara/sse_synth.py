"""Provider-specific SSE synthesis for synthetic deny responses.

Used by the MITM proxy when Bulwark (or legacy Rhema) produces a deny
decision and we need to return a well-formed reply to the agent
without forwarding the request upstream.

The synthesizer accepts a minimal ``DenyInfo`` record (rule_id +
operator-facing text) so callers don't need to construct a full
RhemaRule. MITM builds one of these from a Bulwark ``Decision`` or a
Rhema rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class DenyInfo:
    rule_id: str
    text: str


def _frame(event: str, payload: dict) -> bytes:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def _codex_response(info: DenyInfo) -> bytes:
    """OpenAI Responses API event sequence (chatgpt.com/backend-api/codex)."""
    resp_id = f"resp_ara_{info.rule_id}"
    msg_id = f"msg_ara_{info.rule_id}"

    def _shell(status: str, output: list) -> dict:
        return {
            "id": resp_id, "object": "response", "status": status,
            "created_at": None, "model": "ara-synthetic",
            "output": output, "usage": None,
            "instructions": None, "error": None,
            "incomplete_details": None,
        }

    content_part = {"type": "output_text", "text": info.text, "annotations": []}
    msg_item = {
        "type": "message", "id": msg_id, "status": "completed",
        "role": "assistant", "content": [content_part],
    }
    frames = [
        _frame("response.created", {
            "type": "response.created", "response": _shell("in_progress", []),
        }),
        _frame("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {
                "type": "message", "id": msg_id, "status": "in_progress",
                "role": "assistant", "content": [],
            },
        }),
        _frame("response.content_part.added", {
            "type": "response.content_part.added", "item_id": msg_id,
            "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        }),
        _frame("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": msg_id,
            "output_index": 0, "content_index": 0, "delta": info.text,
        }),
        _frame("response.output_text.done", {
            "type": "response.output_text.done", "item_id": msg_id,
            "output_index": 0, "content_index": 0, "text": info.text,
        }),
        _frame("response.content_part.done", {
            "type": "response.content_part.done", "item_id": msg_id,
            "output_index": 0, "content_index": 0, "part": content_part,
        }),
        _frame("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0,
            "item": msg_item,
        }),
        _frame("response.completed", {
            "type": "response.completed", "response": _shell("completed", [msg_item]),
        }),
    ]
    return b"".join(frames)


def _openai_chat_completion(info: DenyInfo) -> bytes:
    """OpenAI / xAI chat.completions streaming shape."""
    cmp_id = f"chatcmpl_ara_{info.rule_id}"

    def _chunk(delta: dict, finish: "str | None") -> dict:
        return {
            "id": cmp_id, "object": "chat.completion.chunk",
            "created": 0, "model": "ara-synthetic",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    frames = [
        f"data: {json.dumps(_chunk({'role': 'assistant'}, None))}\n\n".encode(),
        f"data: {json.dumps(_chunk({'content': info.text}, None))}\n\n".encode(),
        f"data: {json.dumps(_chunk({}, 'stop'))}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]
    return b"".join(frames)


def _anthropic_message(info: DenyInfo) -> bytes:
    """Anthropic messages streaming shape."""
    msg_id = f"msg_ara_{info.rule_id}"
    frames = [
        _frame("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "content": [], "model": "ara-synthetic",
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }),
        _frame("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        _frame("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": info.text},
        }),
        _frame("content_block_stop",
               {"type": "content_block_stop", "index": 0}),
        _frame("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        }),
        _frame("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(frames)


def _generic_json(info: DenyInfo) -> bytes:
    return json.dumps({
        "ara_synthesized": True, "rule_id": info.rule_id,
        "role": "assistant", "content": info.text,
        "stop_reason": "ara_deny",
    }).encode("utf-8")


def synthesize_deny(info: DenyInfo, provider: str) -> bytes:
    """Return wire-ready bytes (HTTP status line + headers + body) to
    send back in place of the real upstream response.

    Providers: ``codex``, ``openai``, ``xai`` (aliased to openai shape),
    ``anthropic``, anything else → generic JSON.
    """
    if provider == "codex":
        body = _codex_response(info)
        ctype = "text/event-stream"
    elif provider in ("openai", "xai"):
        body = _openai_chat_completion(info)
        ctype = "text/event-stream"
    elif provider == "anthropic":
        body = _anthropic_message(info)
        ctype = "text/event-stream"
    else:
        body = _generic_json(info)
        ctype = "application/json"
    chunked = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    headers = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {ctype}\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "X-ARA-Synthesized: 1\r\n"
        f"X-ARA-Rule-ID: {info.rule_id}\r\n"
        "\r\n"
    ).encode("iso-8859-1")
    return headers + chunked
