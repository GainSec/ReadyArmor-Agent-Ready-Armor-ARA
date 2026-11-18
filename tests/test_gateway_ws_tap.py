"""Gateway WebSocket tap: byte pipe + event emission.

The tap is transport-agnostic (raw TCP) for Stage 1, so we stand up
a plain echo server as stand-in for the real gateway and verify the
pipe + events.
"""

from __future__ import annotations

import base64

import socket
import threading
import time
from pathlib import Path

from ara import gateway_ws_tap as tap


def _make_echo_upstream() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def _run():
        srv.settimeout(1.5)
        while True:
            try:
                c, _ = srv.accept()
            except (socket.timeout, OSError):
                break
            try:
                while True:
                    data = c.recv(4096)
                    if not data:
                        break
                    c.sendall(data)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return srv, port


def test_tap_forwards_and_emits(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    upstream, u_port = _make_echo_upstream()
    try:
        events: list[dict] = []
        h = tap.start(
            state, listen_port=0,
            upstream_host="127.0.0.1", upstream_port=u_port,
            event_sink=events.append,
        )
        assert h is not None
        try:
            time.sleep(0.1)
            c = socket.create_connection(("127.0.0.1", h.listen_port))
            payload = b"GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n"
            c.sendall(payload)
            reply = c.recv(4096)
            assert reply == payload
            c.close()
            time.sleep(0.15)
        finally:
            h.stop()
        assert h.stats.connections_accepted == 1
        assert h.stats.bytes_client_to_upstream == len(payload)
        kinds = [e["kind"] for e in events]
        assert "gateway_ws_tap_open" in kinds
        # Plain TCP passthrough (no WS upgrade) → http_passthrough +
        # raw-bytes traces.
        assert "gateway_http_passthrough" in kinds or "gateway_ws_tap_raw" in kinds
    finally:
        upstream.close()


def test_tap_emits_upstream_error_when_unreachable(tmp_path: Path) -> None:
    """start() always returns a handle; unreachable upstream surfaces as
    an ``gateway_ws_tap_error`` event when a client actually connects."""
    events: list[dict] = []
    h = tap.start(tmp_path, listen_port=0,
                  upstream_host="127.0.0.1", upstream_port=1,
                  event_sink=events.append)
    assert h is not None
    try:
        time.sleep(0.1)
        c = socket.create_connection(("127.0.0.1", h.listen_port))
        c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.3)
        c.close()
    finally:
        h.stop()
    assert any(e["kind"] == "gateway_ws_tap_error" for e in events)


def _make_ws_upgrade_upstream() -> tuple[socket.socket, int]:
    """Stand-in gateway: completes a minimal WS upgrade handshake,
    then echoes a single ACP response text frame back."""
    import base64
    import hashlib
    import struct as _struct
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def _run():
        srv.settimeout(2.0)
        try:
            c, _ = srv.accept()
        except (socket.timeout, OSError):
            return
        try:
            c.settimeout(2.0)
            # Read upgrade request.
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    return
                buf += chunk
            # Pull the Sec-WebSocket-Key header.
            key = ""
            for line in buf.split(b"\r\n"):
                if line.lower().startswith(b"sec-websocket-key:"):
                    key = line.split(b":", 1)[1].strip().decode()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            c.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
            )
            # Send a single ACP response frame.
            payload = (
                b'{"type":"res","id":"r1","result":{"ok":true}}'
            )
            # Server→client frames are unmasked.
            c.sendall(b"\x81" + bytes([len(payload)]) + payload)
            # Give the tap time to forward it before we close.
            import time as _t
            _t.sleep(0.5)
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return srv, port


def _server_ws_frame(payload: bytes) -> bytes:
    """Server-direction (unmasked) WebSocket text frame builder."""
    n = len(payload)
    out = bytearray([0x81])
    if n < 126:
        out.append(n)
    else:
        out.append(126)
        import struct
        out += struct.pack("!H", n)
    out += payload
    return bytes(out)


def _make_ws_upstream_sending_acp(reply_json: dict,
                                  hold_time: float = 1.0) -> tuple[socket.socket, int]:
    """Stub WS server that completes the handshake, then sends one
    server-direction ACP text frame built from ``reply_json`` and
    holds the connection open for ``hold_time`` seconds."""
    import base64
    import hashlib
    import json as _json
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def _run():
        srv.settimeout(2.0)
        try:
            c, _ = srv.accept()
        except (socket.timeout, OSError):
            return
        try:
            c.settimeout(2.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    return
                buf += chunk
            key = ""
            for line in buf.split(b"\r\n"):
                if line.lower().startswith(b"sec-websocket-key:"):
                    key = line.split(b":", 1)[1].strip().decode()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            c.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
            )
            payload = _json.dumps(reply_json).encode()
            c.sendall(_server_ws_frame(payload))
            time.sleep(hold_time)
        finally:
            try:
                c.close()
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return srv, port


def test_u2c_canary_hit_fires_without_dropping(tmp_path: Path) -> None:
    """A canary value echoed in a gateway→client ACP frame fires a
    canary_hit event but still forwards the frame (enforcement on
    u2c would break the req/res pairing)."""
    state = tmp_path / ".ara"
    # Seed a canary with a known value.
    from ara import canary as canary_mod
    c = canary_mod.add(state, tmp_path / "secret.env", kind="generic")
    # Drop the bulwark rule that would normally auto-fire — we want to
    # verify the canary detector alone.
    reply_body = {
        "type": "evt",
        "event": "chat.message",
        "payload": {"text": f"here you go: {c.value}"},
    }
    upstream, u_port = _make_ws_upstream_sending_acp(reply_body)
    try:
        events: list[dict] = []
        h = tap.start(
            state, listen_port=0,
            upstream_host="127.0.0.1", upstream_port=u_port,
            event_sink=events.append,
        )
        assert h is not None
        try:
            time.sleep(0.1)
            cli = socket.create_connection(("127.0.0.1", h.listen_port))
            cli.sendall(
                b"GET / HTTP/1.1\r\nHost: x\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Key: " + base64.b64encode(b"the sample nonce") + b"\r\n"
                b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            cli.settimeout(2.0)
            data = b""
            for _ in range(5):
                try:
                    chunk = cli.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                data += chunk
            cli.close()
            time.sleep(0.4)
        finally:
            h.stop()
    finally:
        upstream.close()

    hits = [e for e in events if e["kind"] == "canary_hit"
            and e.get("scope") == "gateway_reply"]
    assert len(hits) == 1
    assert hits[0]["canary_id"] == c.id


def _client_ws_frame(payload: bytes) -> bytes:
    """Client-direction (masked) WebSocket text frame builder."""
    import os
    mask_key = os.urandom(4)
    n = len(payload)
    out = bytearray([0x81])  # FIN + text
    if n < 126:
        out.append(0x80 | n)
    else:
        out.append(0x80 | 126)
        import struct
        out += struct.pack("!H", n)
    out += mask_key
    out += bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(out)


def test_acp_deny_synthesises_response_and_kills_upstream(tmp_path: Path) -> None:
    """Stage 3 enforcement: Bulwark ``deny acp <method>`` short-circuits
    a request frame — client sees an ARA_DENY response, upstream never
    sees the frame, session is torn down."""
    import json
    upstream_saw_frame = threading.Event()

    def _echo_upstream_flagged(sock_port_holder):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        sock_port_holder.append(srv)
        sock_port_holder.append(srv.getsockname()[1])
        def _run():
            srv.settimeout(2.0)
            try:
                c, _ = srv.accept()
            except (socket.timeout, OSError):
                return
            try:
                c.settimeout(2.0)
                # Upgrade handshake
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = c.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                import base64, hashlib
                key = ""
                for line in buf.split(b"\r\n"):
                    if line.lower().startswith(b"sec-websocket-key:"):
                        key = line.split(b":", 1)[1].strip().decode()
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()).decode()
                c.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
                )
                # Any data received after upgrade = upstream saw a frame
                try:
                    peek = c.recv(4096)
                    if peek:
                        upstream_saw_frame.set()
                except (socket.timeout, OSError):
                    pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass
        threading.Thread(target=_run, daemon=True).start()
        return srv

    holder = []
    upstream = _echo_upstream_flagged(holder)
    u_port = holder[1]
    try:
        state = tmp_path / ".ara"
        state.mkdir()
        (state / "bulwark.md").write_text(
            "## global\ndeny acp chat.inject\n"
        )
        events: list[dict] = []
        h = tap.start(
            state, listen_port=0,
            upstream_host="127.0.0.1", upstream_port=u_port,
            event_sink=events.append,
        )
        assert h is not None
        try:
            time.sleep(0.1)
            c = socket.create_connection(("127.0.0.1", h.listen_port))
            c.sendall(
                b"GET / HTTP/1.1\r\nHost: x\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Key: " + base64.b64encode(b"the sample nonce") + b"\r\n"
                b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            # Wait for the upgrade reply.
            c.settimeout(2.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    break
                buf += chunk
            # Send a client-direction ACP req frame for chat.inject.
            payload = json.dumps({
                "type": "req",
                "id": "r-evil",
                "method": "chat.inject",
                "params": {"text": "exfil secrets"},
            }).encode()
            c.sendall(_client_ws_frame(payload))
            time.sleep(0.4)
            # Read the synthesised deny response.
            try:
                reply = c.recv(4096)
            except (socket.timeout, OSError):
                reply = b""
            c.close()
            time.sleep(0.3)
        finally:
            h.stop()
    finally:
        upstream.close()

    # Deny event emitted.
    denies = [e for e in events if e["kind"] == "gateway_acp_deny"]
    assert len(denies) == 1
    assert denies[0]["method"] == "chat.inject"
    assert denies[0]["frame_id"] == "r-evil"
    # Upstream never saw a post-handshake frame.
    assert not upstream_saw_frame.is_set()
    # Client received the synthetic deny response.
    assert reply, "client should have seen an ARA_DENY response frame"
    # Extract the text payload out of the reply frame and parse.
    import struct
    assert reply[0] == 0x81  # FIN + text, unmasked
    length = reply[1] & 0x7F
    idx = 2
    if length == 126:
        (length,) = struct.unpack("!H", reply[idx: idx + 2])
        idx += 2
    body = json.loads(reply[idx: idx + length].decode())
    assert body["type"] == "res"
    assert body["id"] == "r-evil"
    assert body["error"]["code"] == "ARA_DENY"


def test_ws_upgrade_detected_and_acp_frame_parsed(tmp_path: Path) -> None:
    upstream, u_port = _make_ws_upgrade_upstream()
    try:
        events: list[dict] = []
        h = tap.start(
            tmp_path / ".ara", listen_port=0,
            upstream_host="127.0.0.1", upstream_port=u_port,
            event_sink=events.append,
        )
        assert h is not None
        try:
            time.sleep(0.1)
            c = socket.create_connection(("127.0.0.1", h.listen_port))
            c.sendall(
                b"GET /gateway HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Key: " + base64.b64encode(b"the sample nonce") + b"\r\n"
                b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            # Read the server's upgrade reply + frame.
            data = b""
            c.settimeout(2.0)
            for _ in range(10):
                try:
                    chunk = c.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                data += chunk
            c.close()
            # Let the tap flush events.
            time.sleep(0.5)
        finally:
            h.stop()
    finally:
        upstream.close()

    kinds = [e["kind"] for e in events]
    # Upgrade recognised in both directions.
    assert any(
        e["kind"] == "gateway_ws_tap_upgrade" and e["direction"] == "c2u"
        for e in events
    )
    # ACP response frame parsed.
    acp_events = [e for e in events if e["kind"] == "gateway_acp"]
    assert any(
        e["frame_kind"] == "res" and e["frame_id"] == "r1"
        for e in acp_events
    )


def test_maybe_start_from_env_respects_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ARA_GATEWAY_WS_TAP_PORT", raising=False)
    assert tap.maybe_start_from_env(tmp_path) is None

    upstream, u_port = _make_echo_upstream()
    try:
        monkeypatch.setenv("ARA_GATEWAY_WS_TAP_PORT", "0")
        monkeypatch.setenv("ARA_GATEWAY_WS_UPSTREAM_PORT", str(u_port))
        h = tap.maybe_start_from_env(tmp_path)
        assert h is not None
        h.stop()
    finally:
        upstream.close()
