"""Exercises net.mjs AF_UNIX + AF_INET redirect helpers.

Boots two fake TCP listeners (``real`` and ``tap``) and runs a tiny Node
script that installs ``installHttpHook`` with a stub ``guard`` and
connects to the real port. Verifies the connection lands on the tap —
i.e. the ``ARA_GATEWAY_REAL_PORT`` → ``ARA_GATEWAY_TAP_PORT`` rewrite
happened inside ``net.Socket.prototype.connect``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NET_MJS = REPO_ROOT / "src" / "guard" / "hooks" / "net.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


def _ephemeral_listener() -> tuple[socket.socket, int, list[int]]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    port = s.getsockname()[1]
    hits: list[int] = []

    def accept_loop():
        s.settimeout(5.0)
        try:
            conn, _ = s.accept()
            hits.append(1)
            conn.close()
        except (OSError, socket.timeout):
            pass

    threading.Thread(target=accept_loop, daemon=True).start()
    return s, port, hits


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_inet_gateway_redirect(tmp_path: Path) -> None:
    real_sock, real_port, real_hits = _ephemeral_listener()
    tap_sock, tap_port, tap_hits = _ephemeral_listener()
    try:
        events_file = tmp_path / "events.jsonl"
        script = f"""
import {{ installHttpHook }} from {json.dumps(str(NET_MJS))};
import fs from 'node:fs';
import net from 'node:net';

const guard = {{
    localRules: {{ matchNet: () => null }},
    ipc: {{
        send: (ev) => fs.appendFileSync(
            {json.dumps(str(events_file))}, JSON.stringify(ev) + '\\n'),
    }},
}};
installHttpHook(guard);

const sock = net.createConnection({{ host: '127.0.0.1', port: {real_port} }});
sock.on('connect', () => {{ sock.end(); setTimeout(() => process.exit(0), 100); }});
sock.on('error', () => process.exit(2));
setTimeout(() => process.exit(3), 3000);
"""
        env = {
            **os.environ,
            "ARA_GATEWAY_REAL_PORT": str(real_port),
            "ARA_GATEWAY_TAP_PORT": str(tap_port),
        }
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0, (
            f"node exit {proc.returncode}\nstderr={proc.stderr}\nstdout={proc.stdout}"
        )
        # Give the accept threads a moment.
        time.sleep(0.3)
        assert tap_hits == [1], (
            f"connection did not land on tap port; "
            f"real_hits={real_hits} stderr={proc.stderr} stdout={proc.stdout}"
        )
        assert real_hits == [], "connection should not reach real port"

        events = [
            json.loads(line) for line in
            events_file.read_text().splitlines() if line.strip()
        ]
        redirects = [
            e for e in events
            if e.get("kind") == "policy_hit"
            and e.get("reason") == "AF_INET_REDIRECT_GATEWAY"
        ]
        assert len(redirects) == 1, f"expected 1 redirect event, got: {events}"
    finally:
        real_sock.close()
        tap_sock.close()


@pytest.mark.skipif(not _have_node(), reason="node not installed")
def test_unix_exec_approvals_redirect(short_tmp: Path) -> None:
    real_path = short_tmp / "r.sock"
    tap_path = short_tmp / "t.sock"
    events_file = short_tmp / "events.jsonl"

    tap_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    tap_srv.bind(str(tap_path))
    tap_srv.listen(2)
    tap_hits: list[int] = []

    def accept_tap():
        tap_srv.settimeout(5.0)
        try:
            conn, _ = tap_srv.accept()
            tap_hits.append(1)
            conn.close()
        except (OSError, socket.timeout):
            pass

    threading.Thread(target=accept_tap, daemon=True).start()
    try:
        script = f"""
import {{ installHttpHook }} from {json.dumps(str(NET_MJS))};
import fs from 'node:fs';
import net from 'node:net';

const guard = {{
    localRules: {{ matchNet: () => null }},
    ipc: {{
        send: (ev) => fs.appendFileSync(
            {json.dumps(str(events_file))}, JSON.stringify(ev) + '\\n'),
    }},
}};
installHttpHook(guard);

const sock = net.createConnection({json.dumps(str(real_path))});
sock.on('connect', () => {{ sock.end(); setTimeout(() => process.exit(0), 100); }});
sock.on('error', (e) => {{ console.error(e.message); process.exit(2); }});
setTimeout(() => process.exit(3), 3000);
"""
        env = {
            **os.environ,
            "ARA_EXEC_APPROVALS_REAL_PATH": str(real_path),
            "ARA_EXEC_APPROVALS_TAP_PATH": str(tap_path),
        }
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0, (
            f"node exit {proc.returncode}\nstderr={proc.stderr}"
        )
        time.sleep(0.2)
        assert tap_hits == [1], "unix connection did not land on tap sock"
        events = [
            json.loads(line) for line in
            events_file.read_text().splitlines() if line.strip()
        ]
        redirects = [
            e for e in events
            if e.get("reason") == "AF_UNIX_REDIRECT_EXEC_APPROVALS"
        ]
        assert len(redirects) == 1, f"expected 1 redirect, got: {events}"
    finally:
        tap_srv.close()
