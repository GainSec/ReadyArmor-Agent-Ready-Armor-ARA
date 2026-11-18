"""Audit #15 — derived read-only token. Holders can GET every page
but cannot POST any mutation. The admin token keeps full access."""

from __future__ import annotations

import http.client
import socket
import threading
import time
from pathlib import Path

from ara.armory.auth import ensure_viewer_token, ensure_token
from ara.armory.app import run_armory


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start(state: Path) -> int:
    port = _free_port()
    t = threading.Thread(
        target=run_armory,
        kwargs={
            "state_root": state, "walls_file": None,
            "bind": "127.0.0.1", "port": port,
            "tls_cert": None, "tls_key": None, "behind_proxy": False,
        },
        daemon=True,
    )
    t.start()
    for _ in range(30):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/healthz")
            conn.getresponse().read()
            conn.close()
            return port
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("armory did not start")


def test_viewer_token_is_stable_and_distinct_from_admin(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    a = ensure_token(state)
    v = ensure_viewer_token(state)
    assert a and v and a != v
    # Stable across calls.
    assert ensure_viewer_token(state) == v


def test_viewer_can_get_but_not_post(short_tmp: Path):
    state = short_tmp / "state"
    port = _start(state)
    admin = ensure_token(state)
    viewer = ensure_viewer_token(state)

    # GET /status with viewer works.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", f"/status?t={viewer}")
    resp = conn.getresponse()
    assert resp.status == 200
    resp.read()
    conn.close()

    # POST /rhema/add with viewer is forbidden.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request(
        "POST", f"/rhema/add?t={viewer}",
        body="scope=request&pattern=x&pattern_kind=substring&action=deny",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    assert resp.status == 403, resp.read()
    conn.close()

    # Admin POST still works.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request(
        "POST", f"/rhema/add?t={admin}",
        body="scope=request&pattern=admintest&pattern_kind=substring&action=deny",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    # add route returns 303 redirect on success.
    assert resp.status in (200, 303)
    conn.close()


def test_login_form_renders_unauthenticated(short_tmp: Path):
    state = short_tmp / "state"
    port = _start(state)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", "/login")
    resp = conn.getresponse()
    assert resp.status == 200
    body = resp.read().decode()
    assert "Sign in" in body
    assert 'name="password"' in body
    conn.close()


def test_login_post_with_admin_token_sets_cookie_and_redirects(short_tmp: Path):
    state = short_tmp / "state"
    port = _start(state)
    admin = ensure_token(state)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request(
        "POST", "/login",
        body=f"password={admin}&next=/status",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    assert resp.status == 303
    cookie = resp.getheader("Set-Cookie") or ""
    assert "armory_token=" in cookie
    assert resp.getheader("Location") == "/status"
    conn.close()


def test_login_get_with_token_sets_cookie_and_redirects(short_tmp: Path):
    state = short_tmp / "state"
    port = _start(state)
    admin = ensure_token(state)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", f"/login?t={admin}&next=/armors")
    resp = conn.getresponse()
    assert resp.status == 303
    cookie = resp.getheader("Set-Cookie") or ""
    assert "armory_token=" in cookie
    assert resp.getheader("Location") == "/armors"
    conn.close()


def test_login_post_with_wrong_password_re_renders_form(short_tmp: Path):
    state = short_tmp / "state"
    port = _start(state)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request(
        "POST", "/login",
        body="password=garbage&next=/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    assert resp.status == 401
    body = resp.read().decode()
    assert "Wrong token" in body
    conn.close()


def test_cookie_auth_lets_subsequent_request_through(short_tmp: Path):
    state = short_tmp / "state"
    port = _start(state)
    admin = ensure_token(state)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request(
        "GET", "/status",
        headers={"Cookie": f"armory_token={admin}"},
    )
    resp = conn.getresponse()
    assert resp.status == 200
    conn.close()


def test_unknown_token_redirects_to_login(short_tmp: Path):
    """Unauthenticated GETs now redirect to /login carrying the
    original target as ``next`` so the user lands back where they
    tried to go after signing in."""
    state = short_tmp / "state"
    port = _start(state)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", "/status?t=garbage")
    resp = conn.getresponse()
    assert resp.status == 302
    loc = resp.getheader("Location") or ""
    assert loc.startswith("/login?next=")
    assert "%2Fstatus" in loc
    conn.close()
