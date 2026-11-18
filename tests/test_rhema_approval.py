"""Audit #21 — Rhema action=approval. On match the MITM writes an
ApprovalRecord to the session's pending dir and blocks the turn until
the operator responds (or the timeout fires).

These tests drive ``_try_enforce_request_approval`` directly against a
minimal ``MitmProxy`` instance; the socket it receives is a recording
stand-in so we can inspect what wire bytes got written."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")


import json
import os
import threading
import time
from pathlib import Path

import pytest

from ara import approvals as approvals_mod
from ara import mitm_proxy as mitm_mod
from ara import rhema as rhema_mod


class _RecordingSock:
    """Stand-in for ``tls_client``. Collects everything sent so we can
    assert on the synthetic SSE body."""
    def __init__(self) -> None:
        self.sent: bytes = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data


def _proxy(short_tmp: Path) -> mitm_mod.MitmProxy:
    state = short_tmp / "state"
    state.mkdir()
    session = short_tmp / "state" / "sessions" / ("s" * 32)
    session.mkdir(parents=True)
    return mitm_mod.MitmProxy(state, session)


def _add_approval_rule(state_root: Path, pattern: str = "secret") -> None:
    rhema_mod.upsert_managed(
        state_root, "test-approval",
        scope="request", pattern=pattern,
        pattern_kind="substring", action="approval",
        reason="test approval",
    )


def _req(body_text: str) -> tuple[dict, bytes]:
    parsed = {
        "start": "POST /v1/chat/completions HTTP/1.1",
        "headers": {"host": "api.openai.com"},
        "_lines": ["POST /v1/chat/completions HTTP/1.1", "Host: api.openai.com"],
    }
    return parsed, body_text.encode()


def test_approval_no_match_is_noop(short_tmp: Path):
    p = _proxy(short_tmp)
    sock = _RecordingSock()
    req_parsed, req_body = _req('{"input":"just normal"}')
    outcome = p._try_enforce_request_approval(
        "api.openai.com", 443, req_parsed, req_body, "openai", sock,
    )
    assert outcome == "none"
    assert sock.sent == b""


def test_approval_match_writes_pending_and_blocks(short_tmp: Path, monkeypatch):
    p = _proxy(short_tmp)
    _add_approval_rule(p.state_root)
    sock = _RecordingSock()
    req_parsed, req_body = _req('{"input":"the secret plan"}')

    # Tight timeout so the test doesn't hang if nothing resolves.
    monkeypatch.setenv("ARA_APPROVAL_TIMEOUT_S", "0.5")

    outcome = p._try_enforce_request_approval(
        "api.openai.com", 443, req_parsed, req_body, "openai", sock,
    )
    # Timed out → deny synthesized.
    assert outcome == "deny"
    assert sock.sent, "expected synthetic SSE reply on timeout"
    # A timeout record should have been written.
    resolved = approvals_mod.list_resolved(p.session_dir)
    assert len(resolved) == 1
    assert resolved[0].status == "timeout"


def test_approval_match_allowed_returns_allow(short_tmp: Path, monkeypatch):
    p = _proxy(short_tmp)
    _add_approval_rule(p.state_root)
    sock = _RecordingSock()
    req_parsed, req_body = _req('{"input":"needs secret approval"}')

    # Long enough to let a background thread resolve it.
    monkeypatch.setenv("ARA_APPROVAL_TIMEOUT_S", "3")

    def _resolver():
        # Wait for the pending record to appear, then allow it.
        for _ in range(50):
            pendings = approvals_mod.list_pending(p.session_dir)
            if pendings:
                approvals_mod.resolve(
                    p.session_dir, pendings[0].id, action="allow-once",
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=_resolver, daemon=True)
    t.start()

    outcome = p._try_enforce_request_approval(
        "api.openai.com", 443, req_parsed, req_body, "openai", sock,
    )
    t.join(timeout=1)
    assert outcome == "allow"
    assert sock.sent == b"", "caller continues — no synthetic reply"


def test_approval_match_denied_returns_deny(short_tmp: Path, monkeypatch):
    p = _proxy(short_tmp)
    _add_approval_rule(p.state_root)
    sock = _RecordingSock()
    req_parsed, req_body = _req('{"input":"my secret again"}')
    monkeypatch.setenv("ARA_APPROVAL_TIMEOUT_S", "3")

    def _resolver():
        for _ in range(50):
            pendings = approvals_mod.list_pending(p.session_dir)
            if pendings:
                approvals_mod.resolve(
                    p.session_dir, pendings[0].id, action="deny-once",
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=_resolver, daemon=True)
    t.start()

    outcome = p._try_enforce_request_approval(
        "api.openai.com", 443, req_parsed, req_body, "openai", sock,
    )
    t.join(timeout=1)
    assert outcome == "deny"
    assert sock.sent, "denied requests get a synthetic SSE reply"
