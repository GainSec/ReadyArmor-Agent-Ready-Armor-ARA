"""Exec-approvals proxy: JSONL protocol, Bulwark enforcement, upstream relay."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from ara import exec_approvals_proxy as proxy


class _StubUpstream:
    """Stand-in for openclaw's real exec-approvals socket.

    Parses the incoming request line and responds with a single-line
    decision. Default is always ``allow-once``; ``decision`` arg
    overrides.
    """

    def __init__(self, sock_path: Path, decision: str = "allow-once") -> None:
        self.path = sock_path
        self.decision = decision
        self.requests: list[dict] = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.path.exists():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.srv.bind(str(self.path))
        self.srv.listen(4)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                c.settimeout(2.0)
                buf = b""
                while b"\n" not in buf:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if b"\n" in buf:
                    line, _ = buf.split(b"\n", 1)
                    try:
                        self.requests.append(json.loads(line))
                    except Exception:
                        pass
                response = {"type": "decision", "decision": self.decision}
                c.sendall((json.dumps(response) + "\n").encode())
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass


def _tap_connect_then_request(tap_path: Path, request_obj: dict) -> dict:
    """Connect to the tap, send one JSONL request, read one line."""
    for _ in range(40):
        if tap_path.exists():
            break
        time.sleep(0.05)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(3.0)
    c.connect(str(tap_path))
    c.sendall((json.dumps(request_obj) + "\n").encode())
    try:
        c.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    buf = b""
    while True:
        try:
            chunk = c.recv(4096)
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf:
            break
    c.close()
    line, _ = buf.split(b"\n", 1) if b"\n" in buf else (buf, b"")
    return json.loads(line) if line else {}


def test_request_forwarded_and_decision_relayed(short_tmp: Path) -> None:
    upstream_path = short_tmp / "real.sock"
    up = _StubUpstream(upstream_path, decision="allow-once")
    try:
        state = short_tmp / ".ara"
        state.mkdir()
        events: list[dict] = []
        h = proxy.start(
            state,
            listen_path=short_tmp / "tap.sock",
            real_path=upstream_path,
            event_sink=events.append,
        )
        assert h is not None
        try:
            req = {
                "type": "request",
                "token": "test-tok",
                "id": "req-1",
                "request": {"command": "/usr/bin/ls", "argv": ["/usr/bin/ls", "/tmp"]},
            }
            reply = _tap_connect_then_request(h.listen_path, req)
            assert reply["type"] == "decision"
            assert reply["decision"] == "allow-once"
            # Upstream saw the request verbatim.
            time.sleep(0.2)
            assert len(up.requests) == 1
            assert up.requests[0]["id"] == "req-1"
        finally:
            h.stop()
        kinds = [e["kind"] for e in events]
        assert "exec_approval_request" in kinds
        assert "exec_approval_decision" in kinds
        assert h.stats.decisions_forwarded == 1
    finally:
        up.close()


def test_bulwark_deny_short_circuits(short_tmp: Path) -> None:
    upstream_path = short_tmp / "real.sock"
    up = _StubUpstream(upstream_path, decision="allow-once")
    try:
        state = short_tmp / ".ara"
        state.mkdir()
        (state / "bulwark.md").write_text(
            "## global\ndeny exec /usr/bin/rm\n"
        )
        events: list[dict] = []
        h = proxy.start(
            state,
            listen_path=short_tmp / "tap.sock",
            real_path=upstream_path,
            event_sink=events.append,
        )
        assert h is not None
        try:
            req = {
                "type": "request",
                "token": "test-tok",
                "id": "req-evil",
                "request": {"command": "/usr/bin/rm", "argv": ["rm", "-rf", "/"]},
            }
            reply = _tap_connect_then_request(h.listen_path, req)
            assert reply["type"] == "decision"
            assert reply["decision"] == "deny"
            assert reply.get("ara_rule_id")
            # Upstream never saw the request — short-circuited.
            time.sleep(0.2)
            assert len(up.requests) == 0
        finally:
            h.stop()
        kinds = [e["kind"] for e in events]
        assert "exec_approval_ara_deny" in kinds
        assert h.stats.decisions_denied_by_ara == 1
        assert h.stats.decisions_forwarded == 0
    finally:
        up.close()


def test_bulwark_allow_falls_through(short_tmp: Path) -> None:
    upstream_path = short_tmp / "real.sock"
    up = _StubUpstream(upstream_path, decision="allow-once")
    try:
        state = short_tmp / ".ara"
        state.mkdir()
        (state / "bulwark.md").write_text(
            "## global\nallow exec /usr/bin/ls\n"
        )
        h = proxy.start(
            state,
            listen_path=short_tmp / "tap.sock",
            real_path=upstream_path,
            event_sink=lambda _ev: None,
        )
        assert h is not None
        try:
            req = {
                "type": "request",
                "token": "t",
                "id": "r1",
                "request": {"command": "/usr/bin/ls", "argv": ["ls"]},
            }
            reply = _tap_connect_then_request(h.listen_path, req)
            # Allow rule fires but doesn't block — openclaw's decision wins.
            assert reply["decision"] == "allow-once"
        finally:
            h.stop()
    finally:
        up.close()


def test_upstream_unreachable_synthesizes_deny(short_tmp: Path) -> None:
    missing = short_tmp / "nonexistent.sock"
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dummy.bind(str(missing))
    dummy.listen(1)
    state = short_tmp / ".ara"
    state.mkdir()
    events: list[dict] = []
    h = proxy.start(
        state,
        listen_path=short_tmp / "tap.sock",
        real_path=missing,
        event_sink=events.append,
        upstream_timeout=1.0,
    )
    assert h is not None
    # Wait for the proxy's listen socket to bind before tearing down
    # the upstream — otherwise the test's connect() races the bind.
    for _ in range(40):
        if h.listen_path.exists():
            break
        time.sleep(0.05)
    dummy.close()
    missing.unlink()
    try:
        req = {"type": "request", "token": "t", "id": "x", "request": {}}
        reply = _tap_connect_then_request(h.listen_path, req)
        assert reply["decision"] == "deny"
        assert reply.get("ara_reason") == "UPSTREAM_UNREACHABLE"
    finally:
        h.stop()
    assert any(e["kind"] == "exec_approval_upstream_error" for e in events)


def test_redirect_config_for_does_not_write(tmp_path: Path) -> None:
    """ARA must not write into ~/.openclaw/exec-approvals.json. The
    redirect-config helper is kept as a callable surface that returns
    the would-be path but emits no file write — actual redirection
    happens at the LD_PRELOAD libc shim layer."""
    contained = tmp_path / "home"
    contained.mkdir()
    listen = tmp_path / "tap.sock"
    target = proxy.redirect_config_for(contained, listen, token="t123")
    # Returned path is the conventional location, but no file is written.
    assert target == contained / ".openclaw" / "exec-approvals.json"
    assert not target.exists(), \
        "redirect_config_for must not write into openclaw config"


def test_returns_none_without_upstream_when_required(tmp_path: Path) -> None:
    missing = tmp_path / "never.sock"
    assert proxy.start(tmp_path, real_path=missing, require_upstream=True) is None


def test_starts_without_upstream_by_default(tmp_path: Path) -> None:
    """Default behaviour: start even when the real socket isn't up yet.
    Contained workers are pointed at the proxy regardless; failure to
    forward synthesises a deny (safer default than no coverage)."""
    missing = tmp_path / "never.sock"
    h = proxy.start(tmp_path, real_path=missing)
    try:
        assert h is not None
    finally:
        if h is not None:
            h.stop()


def test_unparseable_request_denied(short_tmp: Path) -> None:
    upstream_path = short_tmp / "real.sock"
    up = _StubUpstream(upstream_path)
    try:
        state = short_tmp / ".ara"
        state.mkdir()
        h = proxy.start(
            state,
            listen_path=short_tmp / "tap.sock",
            real_path=upstream_path,
            event_sink=lambda _ev: None,
        )
        assert h is not None
        try:
            for _ in range(40):
                if h.listen_path.exists():
                    break
                time.sleep(0.05)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(3.0)
            c.connect(str(h.listen_path))
            c.sendall(b"not-json\n")
            try:
                c.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            buf = b""
            while True:
                try:
                    chunk = c.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
            c.close()
            line = buf.split(b"\n", 1)[0] if buf else b""
            reply = json.loads(line) if line else {}
            assert reply.get("decision") == "deny"
            assert reply.get("reason") == "unparseable"
        finally:
            h.stop()
    finally:
        up.close()
