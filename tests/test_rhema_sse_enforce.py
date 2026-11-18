"""Fix 2 — MITM-path Rhema enforcement.

Tests that request-scope deny rules on the MITM path:
  - produce provider-shaped synthetic SSE wire bytes
  - skip the upstream call
  - land the enforcement in the transcript with ``enforced=true``

No live network here — we drive the MITM class directly and mock the
client socket with a byte collector.
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")


import io
import json
import re
import socket
from pathlib import Path

from ara import rhema as rhema_mod
from ara.mitm_proxy import MitmProxy


# ---- unit: SSE synthesizer shapes ---------------------------------------

def _mk_rule(action: str = "deny", scope: str = "request", redirect: str = "",
             rewrite_with=None) -> rhema_mod.RhemaRule:
    return rhema_mod.RhemaRule(
        id="abc1234",
        scope=scope, pattern="anything", pattern_kind="substring",
        action=action, redirect=redirect, reason="test rule",
        rewrite_with=rewrite_with,
    )


def _parse_http_reply(raw: bytes) -> tuple[dict, bytes]:
    """Split ``raw`` (headers + chunked body) into a dict of headers +
    de-chunked body bytes. Tiny parser good enough for our synthetic
    wire format."""
    head, _, rest = raw.partition(b"\r\n\r\n")
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        headers[k.strip().lower().decode()] = v.strip().decode()
    # De-chunk: pattern `<hex>\r\n<data>\r\n...0\r\n\r\n`.
    body = bytearray()
    i = 0
    while i < len(rest):
        nl = rest.find(b"\r\n", i)
        if nl == -1:
            break
        try:
            size = int(rest[i:nl], 16)
        except ValueError:
            break
        i = nl + 2
        if size == 0:
            break
        body.extend(rest[i:i + size])
        i += size + 2
    return headers, bytes(body)


def test_synthesize_deny_sse_codex_shape():
    rule = _mk_rule(redirect="stop that")
    wire = rhema_mod.synthesize_deny_sse(rule, "codex")
    headers, body = _parse_http_reply(wire)
    assert headers["content-type"] == "text/event-stream"
    assert headers["transfer-encoding"] == "chunked"
    assert headers["x-ara-synthesized"] == "1"
    assert headers["x-ara-rule-id"] == "abc1234"
    events = [e for e in body.split(b"\n\n") if e.strip()]
    kinds = [e.split(b"\n")[0].decode().split(":", 1)[1].strip()
             for e in events]
    assert kinds[0] == "response.created"
    assert "response.output_text.delta" in kinds
    assert kinds[-1] == "response.completed"
    assert b"stop that" in body


def test_synthesize_deny_sse_openai_chat_completion_shape():
    rule = _mk_rule(redirect="nope")
    wire = rhema_mod.synthesize_deny_sse(rule, "openai")
    _headers, body = _parse_http_reply(wire)
    assert b"data: [DONE]" in body
    # First content chunk should carry the text.
    data_frames = [ln for ln in body.split(b"\n")
                   if ln.startswith(b"data: {")]
    parsed = [json.loads(ln[6:]) for ln in data_frames]
    contents = [
        c.get("delta", {}).get("content", "")
        for obj in parsed for c in obj.get("choices", [])
    ]
    assert "nope" in "".join(contents)


def test_synthesize_deny_sse_anthropic_shape():
    rule = _mk_rule(redirect="blocked")
    wire = rhema_mod.synthesize_deny_sse(rule, "anthropic")
    _headers, body = _parse_http_reply(wire)
    assert b"event: message_start" in body
    assert b"event: content_block_delta" in body
    assert b"event: message_stop" in body
    assert b"blocked" in body


def test_synthesize_deny_sse_generic_fallback_is_json():
    rule = _mk_rule(redirect="no")
    wire = rhema_mod.synthesize_deny_sse(rule, "some_unknown_provider")
    headers, body = _parse_http_reply(wire)
    assert headers["content-type"] == "application/json"
    obj = json.loads(body)
    assert obj["ara_synthesized"] is True
    assert obj["content"] == "no"


def test_synthesize_deny_uses_default_text_when_no_redirect():
    rule = _mk_rule(redirect="")
    wire = rhema_mod.synthesize_deny_sse(rule, "codex")
    _h, body = _parse_http_reply(wire)
    assert b"ARA-RHEMA" in body
    assert b"abc1234" in body


# ---- integration: _try_enforce_request_deny path ------------------------

class _FakeTlsClient:
    """Collects everything ``sendall`` writes so the test can inspect it."""
    def __init__(self) -> None:
        self.buf = bytearray()
    def sendall(self, data: bytes) -> None:
        self.buf.extend(data)


def test_try_enforce_matches_denies_request(tmp_path: Path):
    rhema_mod.add(
        tmp_path,
        scope="request", pattern="forbidden_phrase", pattern_kind="substring",
        action="deny", redirect="operator says no",
        reason="test",
    )
    # Session dir for transcript writes.
    session_root = tmp_path / "sessions" / "s"
    session_root.mkdir(parents=True)
    proxy = MitmProxy(tmp_path, session_root)

    req_parsed = {
        "start": "POST /backend-api/codex/responses HTTP/1.1",
        "headers": {"host": "chatgpt.com", "content-type": "application/json"},
        "_lines": [],
    }
    req_body = json.dumps({"instructions": "this has forbidden_phrase inside"}).encode()
    client = _FakeTlsClient()

    did_enforce = proxy._try_enforce_request_deny(
        host="chatgpt.com", port=443,
        req_parsed=req_parsed, req_body=req_body,
        provider="codex", tls_client=client,
    )
    assert did_enforce is True
    headers, body = _parse_http_reply(bytes(client.buf))
    assert headers["x-ara-synthesized"] == "1"
    assert b"operator says no" in body
    # Transcript should now have a rhema_deny record with enforced=true.
    tx = (session_root / "transcript.jsonl").read_text().splitlines()
    recs = [json.loads(l) for l in tx if l]
    denies = [r for r in recs if r.get("role") == "rhema_deny"]
    assert len(denies) == 1
    assert denies[0]["payload"]["enforced"] is True
    assert denies[0]["payload"]["path"] == "mitm"


def test_try_enforce_passes_through_when_no_rule_matches(tmp_path: Path):
    # Rule on a pattern the body doesn't contain.
    rhema_mod.add(
        tmp_path,
        scope="request", pattern="other_pattern", pattern_kind="substring",
        action="deny", redirect="no", reason="test",
    )
    session_root = tmp_path / "sessions" / "s"
    session_root.mkdir(parents=True)
    proxy = MitmProxy(tmp_path, session_root)
    req_parsed = {"start": "POST / HTTP/1.1", "headers": {}, "_lines": []}
    req_body = b'{"instructions": "clean payload"}'
    client = _FakeTlsClient()
    did_enforce = proxy._try_enforce_request_deny(
        host="chatgpt.com", port=443,
        req_parsed=req_parsed, req_body=req_body,
        provider="codex", tls_client=client,
    )
    assert did_enforce is False
    assert bytes(client.buf) == b""


def test_try_enforce_fails_open_on_evaluator_error(tmp_path: Path, monkeypatch):
    session_root = tmp_path / "sessions" / "s"
    session_root.mkdir(parents=True)
    proxy = MitmProxy(tmp_path, session_root)

    def _boom(*a, **kw):
        raise RuntimeError("synthetic evaluator failure")
    monkeypatch.setattr(rhema_mod, "evaluate", _boom)

    req_parsed = {"start": "POST / HTTP/1.1", "headers": {}, "_lines": []}
    client = _FakeTlsClient()
    did_enforce = proxy._try_enforce_request_deny(
        host="chatgpt.com", port=443,
        req_parsed=req_parsed, req_body=b"{}",
        provider="codex", tls_client=client,
    )
    # Evaluator failure should NOT enforce (fail-open — same posture as
    # the existing shadow path). Caller will proceed to upstream as normal.
    assert did_enforce is False
    assert bytes(client.buf) == b""


def test_rewrite_action_is_not_mistaken_for_deny(tmp_path: Path):
    rhema_mod.add(
        tmp_path,
        scope="request", pattern="always_matches",
        pattern_kind="substring",
        action="rewrite", redirect="",
        reason="test",
    )
    session_root = tmp_path / "sessions" / "s"
    session_root.mkdir(parents=True)
    proxy = MitmProxy(tmp_path, session_root)
    req_parsed = {"start": "POST / HTTP/1.1", "headers": {}, "_lines": []}
    req_body = b'{"instructions": "always_matches here"}'
    client = _FakeTlsClient()
    did_enforce = proxy._try_enforce_request_deny(
        host="chatgpt.com", port=443,
        req_parsed=req_parsed, req_body=req_body,
        provider="codex", tls_client=client,
    )
    # rewrite is not deny — pass-through.
    assert did_enforce is False
    assert bytes(client.buf) == b""
