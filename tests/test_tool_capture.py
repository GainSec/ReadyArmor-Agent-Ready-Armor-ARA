"""Tool call+output capture from the MITM-observed LLM request stream."""

from __future__ import annotations

import json
from pathlib import Path

from ara.runtime.binder.tool_capture import (
    extract_tool_exchanges,
    capture_tool_exchanges,
    _coerce_text,
)
from ara.runtime.binder.api import EvidenceBinder


def _req(command: str, output: str, *, cid: str = "call_1", name: str = "terminal") -> dict:
    return {
        "messages": [
            {"role": "user", "content": "scan it"},
            {"role": "assistant", "content": "will do", "tool_calls": [
                {"id": cid, "type": "function",
                 "function": {"name": name, "arguments": json.dumps({"command": command})}},
            ]},
            {"role": "tool", "name": name, "tool_call_id": cid,
             "content": json.dumps({"output": output})},
        ]
    }


# --- extraction ---------------------------------------------------------

def test_extract_pairs_command_and_output() -> None:
    ex = extract_tool_exchanges(_req("python3 -c \"print('BODY:',data)\"", "STATUS: 200\nBODY: {hash}"))
    assert len(ex) == 1
    assert ex[0].tool_name == "terminal"
    assert ex[0].tool_call_id == "call_1"
    assert ex[0].command and "python3" in ex[0].command
    assert ex[0].output == "STATUS: 200\nBODY: {hash}"  # unwrapped from {"output": ...}


def test_coerce_unwraps_output_json() -> None:
    assert _coerce_text(json.dumps({"output": "STATUS: 200"})) == "STATUS: 200"


def test_coerce_plain_string_passthrough() -> None:
    assert _coerce_text("raw stdout") == "raw stdout"


def test_coerce_anthropic_content_list() -> None:
    assert _coerce_text([{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]) == "hello\nworld"


def test_extract_no_messages() -> None:
    assert extract_tool_exchanges({}) == []
    assert extract_tool_exchanges("nope") == []
    assert extract_tool_exchanges({"messages": "bad"}) == []


def test_extract_tool_without_matching_call() -> None:
    # tool result whose call id isn't in an assistant message still yields output
    req = {"messages": [{"role": "tool", "name": "terminal", "tool_call_id": "x",
                         "content": json.dumps({"output": "orphan"})}]}
    ex = extract_tool_exchanges(req)
    assert len(ex) == 1 and ex[0].output == "orphan" and ex[0].command is None


# --- capture (binds real evidence) --------------------------------------

def test_capture_binds_tool_output_and_dedups(tmp_path: Path) -> None:
    sd = tmp_path / "sessions" / "s1"
    sd.mkdir(parents=True)
    req = _req("curl http://192.168.18.9/api/v1/users",
               "STATUS: 200\n[{\"username\":\"admin\",\"password_hash\":\"deadbeef\"}]")
    n = capture_tool_exchanges(sd, req, activation_id="act-1")
    assert n == 1

    binder = EvidenceBinder(sd)
    results = binder.search("password_hash")
    assert results, "the response body (with the hash) must be searchable as evidence"
    r = results[0].record
    assert r.source == "tool:terminal"
    body = binder.get(r.id).decode()
    assert "password_hash" in body and "deadbeef" in body  # the DATA, not just the command
    assert r.captured_during_activation_id == "act-1"
    binder.close()

    # Idempotent: same exchange again binds nothing new.
    n2 = capture_tool_exchanges(sd, req, activation_id="act-1")
    assert n2 == 1  # returns count seen
    b2 = EvidenceBinder(sd)
    assert len(b2.search("password_hash")) == 1  # still one record (deduped by hash)
    b2.close()


def test_capture_skips_empty_output(tmp_path: Path) -> None:
    sd = tmp_path / "sessions" / "s2"
    sd.mkdir(parents=True)
    req = _req("noop", "")
    assert capture_tool_exchanges(sd, req, activation_id="a") == 0


def test_capture_bad_session_never_raises(tmp_path: Path) -> None:
    # non-existent parent dir path must not raise
    capture_tool_exchanges(tmp_path / "no" / "such", _req("x", "y"))
