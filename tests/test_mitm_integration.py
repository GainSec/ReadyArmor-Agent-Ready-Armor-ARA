"""Audit #9 — MITM integration tests against a fake HTTPS upstream.

These are end-to-end: we spin up the real ``MitmProxy`` listening on a
localhost port, a fake HTTPS upstream on another port, monkeypatch
``socket.create_connection`` and ``ssl.create_default_context`` inside
``mitm_proxy`` so outbound traffic goes to the fake instead of the real
Internet, then drive a raw CONNECT+TLS client through the proxy.

Each test exercises one of the three request-scope actions that ARA
enforces at the MITM layer:
  - deny    → synthetic SSE returned, upstream never contacted
  - rewrite → body is mutated before upstream sees it (agent sees the
              response to the mutated request)
  - passthrough → nothing changes

These tests do real TLS handshakes and real openssl CA signing; they
take noticeably longer than the unit tests (roughly 1–2 s each) but
prove the whole stack works together.
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
import time
from pathlib import Path

import pytest

from ara import armors as armors_mod
from ara import mitm_proxy as mitm_mod
from ara import rhema as rhema_mod


FAKE_HOST = "api.openai.com"  # one of MITM_HOSTS; provider → "openai"


# ---- fake upstream --------------------------------------------------------


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
            server.received_bodies.append(body)

            payload = json.dumps({"echo": True, "len": len(body)}).encode()
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
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

    def __init__(self, ctx: ssl.SSLContext):
        super().__init__(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.ctx = ctx
        self.received_bodies: list[bytes] = []


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
def rigged_proxy(short_tmp: Path, monkeypatch):
    """Spin up MitmProxy + fake upstream + monkeypatches. Yields a
    context carrying the mitm client-port, the ARA CA cert path, the
    fake upstream instance, and the state_root."""
    state = short_tmp / "state"
    state.mkdir()
    session = short_tmp / "session"
    session.mkdir()

    # Fake upstream with a self-signed cert for FAKE_HOST.
    ups_key, ups_crt = _gen_self_signed(short_tmp, FAKE_HOST)
    up_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    up_ctx.load_cert_chain(certfile=str(ups_crt), keyfile=str(ups_key))
    upstream = _FakeUpstream(up_ctx)
    up_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    up_thread.start()
    up_port = upstream.server_address[1]

    # Redirect MitmProxy's outbound create_connection to the fake, and
    # give it an SSL context that trusts the fake's self-signed cert.
    real_create_connection = socket.create_connection

    def _fake_create_connection(addr, *args, **kwargs):
        host, _port = addr
        if host == FAKE_HOST:
            return real_create_connection(("127.0.0.1", up_port), *args, **kwargs)
        return real_create_connection(addr, *args, **kwargs)

    monkeypatch.setattr(mitm_mod.socket, "create_connection", _fake_create_connection)

    client_trust_ctx = ssl.create_default_context()
    client_trust_ctx.load_verify_locations(cafile=str(ups_crt))

    def _fake_create_default_context(*args, **kwargs):
        return client_trust_ctx

    monkeypatch.setattr(mitm_mod.ssl, "create_default_context",
                        _fake_create_default_context)

    server, port = mitm_mod.start_proxy_threaded(state, session)

    ca_crt = mitm_mod.ca_cert_path(state)

    try:
        yield {
            "state_root": state,
            "session": session,
            "mitm_port": port,
            "ca_cert": ca_crt,
            "upstream": upstream,
        }
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def _drive_client(mitm_port: int, ca_cert: Path, body: dict) -> tuple[int, bytes]:
    """Open TCP → CONNECT api.openai.com:443 → TLS → POST. Return
    (status_code, response_body)."""
    body_bytes = json.dumps(body).encode("utf-8")
    sock = socket.create_connection(("127.0.0.1", mitm_port), timeout=10)
    try:
        sock.sendall(
            f"CONNECT {FAKE_HOST}:443 HTTP/1.1\r\n"
            f"Host: {FAKE_HOST}:443\r\n\r\n".encode(),
        )
        # Read the 200 Connection established.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("proxy closed during CONNECT")
            buf += chunk
        if b"200" not in buf.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"unexpected CONNECT reply: {buf!r}")

        trust_ctx = ssl._create_unverified_context()
        tls = trust_ctx.wrap_socket(sock, server_hostname=FAKE_HOST)
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


# ---- tests ----------------------------------------------------------------


def test_mitm_passthrough_no_rule(rigged_proxy):
    """No matching rhema rule: request reaches upstream verbatim."""
    status, body = _drive_client(
        rigged_proxy["mitm_port"], rigged_proxy["ca_cert"],
        {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 200
    assert rigged_proxy["upstream"].received_bodies, "upstream never saw a request"
    sent = json.loads(rigged_proxy["upstream"].received_bodies[0])
    assert sent["model"] == "gpt-5"
    assert sent["messages"][0]["content"] == "hi"


def test_mitm_deny_synthesizes_response_and_skips_upstream(rigged_proxy):
    """A request-scope deny rule: synthetic reply, upstream untouched."""
    rhema_mod.upsert_managed(
        rigged_proxy["state_root"], "test-deny",
        scope="request", pattern="secret",
        pattern_kind="substring", action="deny",
        reason="blocked by test",
    )
    status, body = _drive_client(
        rigged_proxy["mitm_port"], rigged_proxy["ca_cert"],
        {"model": "gpt-5", "messages": [{"role": "user", "content": "tell me a secret"}]},
    )
    assert status == 200, f"expected synthetic 200 SSE, got {status}"
    # Synthetic SSE for provider=openai is chat.completions-shaped.
    assert b"data:" in body or b"RHEMA" in body.upper(), (
        f"synthetic body didn't look SSE-ish: {body[:400]!r}"
    )
    assert rigged_proxy["upstream"].received_bodies == [], (
        "upstream was contacted despite deny rule"
    )


def test_mitm_rewrite_mutates_body_to_upstream(rigged_proxy):
    """An ``action=rewrite`` rule: upstream receives the mutated body."""
    rhema_mod.upsert_managed(
        rigged_proxy["state_root"], "test-rw",
        scope="request", pattern="gpt-5-large",
        pattern_kind="substring", action="rewrite",
        rewrite_with={"op": "replace", "field": "model", "text": "gpt-mini"},
        reason="model downgrade",
    )
    status, body = _drive_client(
        rigged_proxy["mitm_port"], rigged_proxy["ca_cert"],
        {"model": "gpt-5-large", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert status == 200
    assert len(rigged_proxy["upstream"].received_bodies) == 1, (
        f"expected exactly 1 upstream request, got "
        f"{len(rigged_proxy['upstream'].received_bodies)}"
    )
    sent = json.loads(rigged_proxy["upstream"].received_bodies[0])
    assert sent["model"] == "gpt-mini", (
        f"rewrite did not apply; upstream saw model={sent.get('model')!r}"
    )


def test_mitm_absolute_enforcing_rewrites_codex_instructions(rigged_proxy):
    """Promote flow: armor with absolute_enforce=True causes the live
    codex/responses request to have its ``instructions`` replaced."""
    state = rigged_proxy["state_root"]
    armors_mod.create_armor(state, "guard", description="t")
    armor = armors_mod.load_armor(state, "guard")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("NEW-PROMPT")
    armors_mod.set_active_armor(state, "guard")
    armors_mod.set_absolute_enforce(state, "guard", True)

    # codex/responses path is how active_absolute_rule is wired to match.
    # Use a custom client driver so we hit /codex/responses.
    body_obj = {"instructions": "ORIGINAL", "input": "hello"}
    body_bytes = json.dumps(body_obj).encode()
    sock = socket.create_connection(("127.0.0.1", rigged_proxy["mitm_port"]), timeout=10)
    try:
        sock.sendall(
            f"CONNECT {FAKE_HOST}:443 HTTP/1.1\r\n"
            f"Host: {FAKE_HOST}:443\r\n\r\n".encode(),
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(4096)
        trust_ctx = ssl._create_unverified_context()
        tls = trust_ctx.wrap_socket(sock, server_hostname=FAKE_HOST)
        req = (
            f"POST /codex/responses HTTP/1.1\r\n"
            f"Host: {FAKE_HOST}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        tls.sendall(req + body_bytes)
        # Drain response to let upstream finish.
        try:
            while True:
                chunk = tls.recv(8192)
                if not chunk:
                    break
        except (ssl.SSLError, OSError):
            pass
        try:
            tls.close()
        except Exception:
            pass
    finally:
        sock.close()

    assert len(rigged_proxy["upstream"].received_bodies) == 1
    sent = json.loads(rigged_proxy["upstream"].received_bodies[0])
    assert sent["instructions"] == "NEW-PROMPT", (
        f"absolute_enforce did not rewrite; upstream saw "
        f"instructions={sent.get('instructions')!r}"
    )
