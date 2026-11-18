"""A — Tier-B response-scope deny synthesis. Drives the MITM end-to-end
against a fake HTTPS upstream that returns a known-bad response body;
verifies the client receives the synthetic SSE deny instead of the
original (and that the original would have come through if no rule
matched).
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")


import json
import socket
import socketserver
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from ara import mitm_proxy as mitm_mod
from ara import rhema as rhema_mod


FAKE_HOST = "api.openai.com"


class _FakeUpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "_FakeUpstream" = self.server  # type: ignore[assignment]
        tls = server.ctx.wrap_socket(self.request, server_side=True)
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = tls.recv(8192)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            clen = int(headers.get("content-length") or "0")
            body = rest
            while len(body) < clen:
                chunk = tls.recv(min(8192, clen - len(body)))
                if not chunk:
                    break
                body += chunk
            payload = server.response_body.encode()
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n"
            ) + payload
            tls.sendall(resp)
        finally:
            try:
                tls.close()
            except Exception:
                pass


class _FakeUpstream(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, ctx: ssl.SSLContext, response_body: str):
        super().__init__(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.ctx = ctx
        self.response_body = response_body


def _gen_self_signed(tmp: Path, cn: str) -> tuple[Path, Path]:
    key = tmp / "up.key"
    crt = tmp / "up.crt"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                   check=True, capture_output=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes", "-key", str(key),
         "-sha256", "-days", "30", "-subj", f"/CN={cn}",
         "-addext", f"subjectAltName=DNS:{cn}",
         "-out", str(crt)],
        check=True, capture_output=True,
    )
    return key, crt


@pytest.fixture
def proxy_with_upstream(short_tmp: Path, monkeypatch):
    """Spin up MitmProxy + fake upstream that returns a body containing
    'api_key=sk-leaked123'. Returns dict with mitm_port, ca_cert, the
    state_root, and a callable to set the upstream's response body."""
    state = short_tmp / "state"
    state.mkdir()
    session = short_tmp / "session"
    session.mkdir()

    ups_key, ups_crt = _gen_self_signed(short_tmp, FAKE_HOST)
    up_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    up_ctx.load_cert_chain(certfile=str(ups_crt), keyfile=str(ups_key))
    upstream = _FakeUpstream(up_ctx, response_body="initial")
    up_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    up_thread.start()
    up_port = upstream.server_address[1]

    real_create_connection = socket.create_connection
    def _fake_create_connection(addr, *args, **kwargs):
        host, _port = addr
        if host == FAKE_HOST:
            return real_create_connection(("127.0.0.1", up_port), *args, **kwargs)
        return real_create_connection(addr, *args, **kwargs)
    monkeypatch.setattr(mitm_mod.socket, "create_connection",
                        _fake_create_connection)

    client_trust_ctx = ssl.create_default_context()
    client_trust_ctx.load_verify_locations(cafile=str(ups_crt))
    monkeypatch.setattr(mitm_mod.ssl, "create_default_context",
                        lambda *a, **kw: client_trust_ctx)

    server, port = mitm_mod.start_proxy_threaded(state, session)

    try:
        yield {
            "state_root": state,
            "mitm_port": port,
            "set_response_body": lambda s: setattr(upstream, "response_body", s),
        }
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def _drive_post(mitm_port: int, body: dict) -> tuple[int, bytes]:
    body_bytes = json.dumps(body).encode()
    sock = socket.create_connection(("127.0.0.1", mitm_port), timeout=10)
    try:
        sock.sendall(
            f"CONNECT {FAKE_HOST}:443 HTTP/1.1\r\n"
            f"Host: {FAKE_HOST}:443\r\n\r\n".encode(),
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("proxy closed")
            buf += chunk
        tls = ssl._create_unverified_context().wrap_socket(
            sock, server_hostname=FAKE_HOST,
        )
        req = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: {FAKE_HOST}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        tls.sendall(req + body_bytes)
        resp = b""
        while True:
            try:
                chunk = tls.recv(8192)
            except (ssl.SSLError, OSError):
                break
            if not chunk:
                break
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("iso-8859-1")
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        try:
            tls.close()
        except Exception:
            pass
        return status, rest
    finally:
        try:
            sock.close()
        except Exception:
            pass


def test_response_deny_replaces_offending_body_with_synthetic_sse(
    proxy_with_upstream,
):
    rhema_mod.upsert_managed(
        proxy_with_upstream["state_root"], "test-resp-deny",
        scope="response", pattern="api_key", pattern_kind="substring",
        action="deny", reason="api_key leak in response",
    )
    proxy_with_upstream["set_response_body"](
        'data: {"choices":[{"delta":{"content":"here is api_key=sk-leak"}}]}'
        "\n\ndata: [DONE]\n"
    )
    status, body = _drive_post(
        proxy_with_upstream["mitm_port"],
        {"messages": [{"role": "user", "content": "give me a key"}]},
    )
    assert status == 200, f"expected synthetic 200, got {status}"
    body_text = body.decode("utf-8", errors="replace")
    # Synthetic reply should NOT contain the leaked key.
    assert "sk-leak" not in body_text, (
        f"original (offending) body leaked through: {body_text[:300]!r}"
    )
    # Synthetic reply SHOULD identify itself as a Rhema deny.
    assert "RHEMA" in body_text.upper() or "Blocked by rule" in body_text, (
        f"synthetic body didn't carry the deny marker: {body_text[:300]!r}"
    )


def test_response_no_match_passes_original_through(proxy_with_upstream):
    """When the response-scope rule exists but doesn't match this body,
    the original response is forwarded unchanged."""
    rhema_mod.upsert_managed(
        proxy_with_upstream["state_root"], "test-resp-deny",
        scope="response", pattern="this-phrase-never-appears",
        pattern_kind="substring", action="deny",
    )
    safe_body = (
        'data: {"choices":[{"delta":{"content":"hello world"}}]}\n\n'
        'data: [DONE]\n'
    )
    proxy_with_upstream["set_response_body"](safe_body)
    status, body = _drive_post(
        proxy_with_upstream["mitm_port"],
        {"messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 200
    assert b"hello world" in body, (
        f"original body should pass through: {body[:300]!r}"
    )


def test_no_response_deny_rule_uses_streaming_path(proxy_with_upstream):
    """With no response-scope deny rules, MITM uses the original
    streaming pass-through (no buffering). Original body still
    delivered."""
    proxy_with_upstream["set_response_body"](
        'data: {"choices":[{"delta":{"content":"streamed reply"}}]}\n\n'
        'data: [DONE]\n'
    )
    status, body = _drive_post(
        proxy_with_upstream["mitm_port"],
        {"messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 200
    assert b"streamed reply" in body
