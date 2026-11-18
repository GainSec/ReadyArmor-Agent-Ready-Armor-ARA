"""AF_UNIX proxy for OpenClaw's exec-approvals socket — JSONL-aware.

OpenClaw's exec-approvals protocol (source:
``~/.npm-global/lib/node_modules/openclaw/dist/exec-approvals-*.js``):

Client → server (one JSON object + ``\\n``)::

    {"type":"request","token":"<tok>","id":"<uuid>","request":<obj>}

Server → client (newline-delimited JSON until an accepted line)::

    {"type":"decision","decision":"allow-once"|"allow-always"|"deny"}

``request`` is opaque at the protocol layer; in practice exec-approval
requests carry ``command`` / ``argv`` / ``cwd`` fields (bash-tools
runtime populates them via ``requestExecApprovalViaSocket``).

This proxy:

1. Listens on an ARA-owned AF_UNIX socket (``<state_root>/
   exec-approvals-tap.sock``) that openclaw is pointed at via a
   rewritten ``exec-approvals.json``.
2. Parses each inbound request frame, emits an
   ``exec_approval_request`` event with the parsed request object.
3. Evaluates the request against Bulwark ``exec`` rules (if any).
4. **Deny**: synthesises ``{"type":"decision","decision":"deny"}`` and
   returns it without touching openclaw's real socket.
5. **Allow / approval / default-allow**: forwards to openclaw's real
   socket, relays the response, logs the outcome as
   ``exec_approval_decision``.
6. Emits ``exec_approval_timeout`` if upstream doesn't respond.

Unlike the first-pass tap, this is enforcement-capable: a Bulwark
``deny exec`` rule auto-blocks via this proxy before openclaw even
sees the request.

Auto-enabled by ``ara.containment`` when containment is active.
Non-contained sessions get a warning — the proxy only works if the
agent is pointed at its socket.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


DEFAULT_REAL_SOCKET = Path.home() / ".clawdbot" / "exec-approvals.sock"


@dataclass
class ProxyStats:
    connections_accepted: int = 0
    requests_parsed: int = 0
    decisions_forwarded: int = 0
    decisions_denied_by_ara: int = 0
    upstream_errors: int = 0
    parse_errors: int = 0
    timeouts: int = 0


@dataclass
class Handle:
    thread: threading.Thread
    stop_event: threading.Event
    listen_path: Path
    real_path: Path
    stats: ProxyStats = field(default_factory=ProxyStats)

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=timeout)
        try:
            if self.listen_path.exists():
                self.listen_path.unlink()
        except OSError:
            pass


# ---- event sink ------------------------------------------------------

def _default_sink_factory(
    events_path: Path,
) -> Callable[[dict], None]:
    def _sink(ev: dict) -> None:
        try:
            ev.setdefault("ts", time.time())
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass
    return _sink


# ---- Bulwark integration --------------------------------------------

def _consult_bulwark(
    state_root: Path, request: dict[str, Any]
) -> Optional[tuple[str, str]]:
    """Ask Bulwark whether to deny this exec. Returns:

    * ``(rule_id, matched_token)`` if any ``exec`` layer rule matches and
      evaluates deny. ``matched_token`` is the substring the deciding rule
      matched (e.g. ``reboot``), or ``""`` when not recoverable.
    * ``None`` otherwise — defer to openclaw's upstream decision.

    Extracts ``command`` / ``argv`` from the request object using the
    keys openclaw populates via ``requestExecApprovalViaSocket``.
    """
    try:
        from . import bulwark as _bw
    except Exception:  # noqa: BLE001
        return None
    bw_path = state_root / "bulwark.md"
    if not bw_path.exists():
        return None
    try:
        bw = _bw.parse_file(bw_path)
    except Exception:  # noqa: BLE001
        return None
    if not bw.rules:
        return None
    cmd = ""
    argv_s = ""
    if isinstance(request, dict):
        cmd = str(request.get("command") or request.get("cmd") or "")
        argv = request.get("argv") or request.get("args") or []
        if isinstance(argv, list):
            argv_s = " ".join(str(a) for a in argv)
    if not cmd and argv_s:
        # Some callers send only argv; the first arg is the binary.
        first = argv_s.split(" ", 1)[0]
        cmd = first
    event = {"layer": "exec", "path": cmd, "argv": argv_s}
    try:
        armor_id = None
        from . import armors as _armors
        try:
            armor_id = _armors.active_armor_name(state_root)
        except Exception:  # noqa: BLE001
            pass
        d = _bw.evaluate(bw, event, armor_id=armor_id)
    except Exception:  # noqa: BLE001
        return None
    if d.rule is None:
        return None
    if d.action == "deny":
        matched_token = ""
        try:
            from . import exec_deny_notices as _notices
            match = getattr(d.rule, "match", None)
            matched_token = _notices.extract_matched_token(
                getattr(match, "primary", "") or "",
                bool(getattr(match, "is_regex", False)),
                (cmd + " " + argv_s).strip(),
            )
        except Exception:  # noqa: BLE001
            matched_token = ""
        return (d.rule.rule_id, matched_token)
    return None


# ---- connection handler --------------------------------------------

def _read_line(sock: socket.socket, timeout: float) -> Optional[bytes]:
    """Read one newline-terminated frame. Returns None on EOF / error."""
    sock.settimeout(timeout)
    buf = bytearray()
    while True:
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, BlockingIOError, OSError):
            return bytes(buf) if buf else None
        if not chunk:
            return bytes(buf) if buf else None
        buf.extend(chunk)
        nl = buf.find(b"\n")
        if nl >= 0:
            return bytes(buf[: nl + 1])
        if len(buf) > 2 * 1024 * 1024:
            # Oversized request — drop.
            return None


def _forward_and_relay_decision(
    request_line: bytes,
    real_path: Path,
    timeout_s: float,
) -> Optional[bytes]:
    """Open the real openclaw socket, write the request, return the
    first decision-shaped response line. Returns None on error."""
    try:
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.connect(str(real_path))
    except OSError:
        return None
    try:
        up.sendall(request_line)
        # Shutdown write so upstream knows we're done writing, matching
        # openclaw's client.end() call.
        try:
            up.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        # Read response lines until we see a decision or EOF / timeout.
        up.settimeout(timeout_s)
        buf = bytearray()
        while True:
            try:
                chunk = up.recv(4096)
            except (socket.timeout, BlockingIOError, OSError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            # Return on first complete line that parses as a decision.
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[: nl + 1])
                buf = buf[nl + 1 :]
                try:
                    obj = json.loads(line.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict) and obj.get("type") == "decision":
                    return line
        return None
    finally:
        try:
            up.close()
        except OSError:
            pass


def _cmd_summary(request: Any) -> str:
    """Short human-readable summary of the exec request, for the deny notice."""
    if not isinstance(request, dict):
        return ""
    cmd = str(request.get("command") or request.get("cmd") or "")
    argv = request.get("argv") or request.get("args") or []
    if isinstance(argv, list) and argv:
        cmd = (cmd + " " + " ".join(str(a) for a in argv)).strip()
    return cmd[:200]


def _deny_response_line(extra: Optional[dict[str, Any]] = None) -> bytes:
    body: dict[str, Any] = {"type": "decision", "decision": "deny"}
    if extra:
        body.update(extra)
    return (json.dumps(body) + "\n").encode("utf-8")


def _handle_client(
    client_sock: socket.socket,
    state_root: Path,
    real_path: Path,
    client_id: int,
    stats: ProxyStats,
    event_sink: Callable[[dict], None],
    upstream_timeout: float,
) -> None:
    try:
        line = _read_line(client_sock, timeout=upstream_timeout)
        if not line:
            return
        try:
            req_obj = json.loads(line.decode("utf-8"))
        except Exception:  # noqa: BLE001
            stats.parse_errors += 1
            event_sink({
                "kind": "exec_approval_parse_error",
                "client_id": client_id,
                "preview": line[:256].decode("utf-8", "replace"),
            })
            try:
                client_sock.sendall(_deny_response_line({"reason": "unparseable"}))
            except OSError:
                pass
            return

        stats.requests_parsed += 1
        request_inner = (req_obj or {}).get("request") if isinstance(req_obj, dict) else None
        event_sink({
            "kind": "exec_approval_request",
            "client_id": client_id,
            "request_id": req_obj.get("id") if isinstance(req_obj, dict) else None,
            "request": request_inner,
        })

        # Bulwark enforcement: if a deny rule matches, short-circuit.
        deny = _consult_bulwark(
            state_root, request_inner if isinstance(request_inner, dict) else {},
        )
        if deny:
            deny_rule_id, matched_token = deny
            stats.decisions_denied_by_ara += 1
            reply = _deny_response_line({
                "id": req_obj.get("id") if isinstance(req_obj, dict) else None,
                "ara_rule_id": deny_rule_id,
                "ara_reason": f"BULWARK_DENY:{deny_rule_id}",
            })
            try:
                client_sock.sendall(reply)
            except OSError:
                pass
            # Additive, best-effort: queue a one-shot notice so the MITM
            # proxy can tell the agent (on its next model turn) what was
            # blocked and to skip it. Never affects the hard deny above.
            try:
                from . import exec_deny_notices as _notices
                _notices.append(
                    state_root,
                    rule_id=deny_rule_id,
                    matched_token=matched_token,
                    cmd_summary=_cmd_summary(request_inner),
                    ts=time.time(),
                )
            except Exception:  # noqa: BLE001
                event_sink({
                    "kind": "exec_deny_notice_error",
                    "client_id": client_id,
                    "rule_id": deny_rule_id,
                })
            event_sink({
                "kind": "exec_approval_ara_deny",
                "client_id": client_id,
                "request_id": req_obj.get("id") if isinstance(req_obj, dict) else None,
                "rule_id": deny_rule_id,
            })
            return

        # Forward to openclaw and relay the decision.
        decision_line = _forward_and_relay_decision(
            line, real_path, timeout_s=upstream_timeout,
        )
        if decision_line is None:
            stats.upstream_errors += 1
            event_sink({
                "kind": "exec_approval_upstream_error",
                "client_id": client_id,
                "request_id": req_obj.get("id") if isinstance(req_obj, dict) else None,
            })
            try:
                client_sock.sendall(_deny_response_line({
                    "id": req_obj.get("id") if isinstance(req_obj, dict) else None,
                    "ara_reason": "UPSTREAM_UNREACHABLE",
                }))
            except OSError:
                pass
            return

        stats.decisions_forwarded += 1
        try:
            client_sock.sendall(decision_line)
        except OSError:
            pass

        # Log the decision + its effect.
        try:
            decision_obj = json.loads(decision_line.decode("utf-8"))
        except Exception:  # noqa: BLE001
            decision_obj = {}
        event_sink({
            "kind": "exec_approval_decision",
            "client_id": client_id,
            "request_id": req_obj.get("id") if isinstance(req_obj, dict) else None,
            "decision": decision_obj.get("decision") if isinstance(decision_obj, dict) else None,
        })
    finally:
        try:
            client_sock.close()
        except OSError:
            pass


def _serve_loop(
    handle: Handle,
    state_root: Path,
    event_sink: Callable[[dict], None],
    upstream_timeout: float,
) -> None:
    try:
        if handle.listen_path.exists():
            handle.listen_path.unlink()
    except OSError:
        pass
    handle.listen_path.parent.mkdir(parents=True, exist_ok=True)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(handle.listen_path))
    except OSError as exc:
        event_sink({
            "kind": "exec_approval_bind_error",
            "listen_path": str(handle.listen_path),
            "error": repr(exc),
        })
        return
    os.chmod(handle.listen_path, 0o600)
    server.listen(16)
    server.settimeout(0.5)

    sel = selectors.DefaultSelector()
    sel.register(server, selectors.EVENT_READ)
    client_id = 0
    try:
        while not handle.stop_event.is_set():
            events = sel.select(timeout=0.5)
            for _key, _mask in events:
                try:
                    csock, _addr = server.accept()
                except OSError:
                    continue
                handle.stats.connections_accepted += 1
                client_id += 1
                threading.Thread(
                    target=_handle_client, daemon=True,
                    args=(csock, state_root, handle.real_path, client_id,
                          handle.stats, event_sink, upstream_timeout),
                ).start()
    finally:
        try:
            server.close()
        except OSError:
            pass


# ---- public entrypoint ---------------------------------------------

def start(
    state_root: Path,
    *,
    listen_path: Optional[Path] = None,
    real_path: Path = DEFAULT_REAL_SOCKET,
    event_sink: Optional[Callable[[dict], None]] = None,
    upstream_timeout: float = 15.0,
    require_upstream: bool = False,
) -> Optional[Handle]:
    """Start the proxy. By default starts even if openclaw's real
    socket isn't yet present — contained workers get pointed at the
    proxy and failure-to-forward synthesises a deny (safer default).

    Pass ``require_upstream=True`` to return None when the upstream
    doesn't exist (useful for tests + callers that explicitly want
    to know whether there's anything to enforce against)."""
    if require_upstream and not real_path.exists():
        return None
    if listen_path is None:
        listen_path = state_root / "exec-approvals-tap.sock"
    if event_sink is None:
        event_sink = _default_sink_factory(state_root / "events.jsonl")
    stop_event = threading.Event()
    handle = Handle(
        thread=None,  # populated below
        stop_event=stop_event,
        listen_path=listen_path,
        real_path=real_path,
    )

    def _runner():
        _serve_loop(handle, state_root, event_sink, upstream_timeout)

    t = threading.Thread(
        target=_runner, daemon=True,
        name="ara-exec-approvals-proxy",
    )
    handle.thread = t
    t.start()
    return handle


def redirect_config_for(
    contained_home: Path, listen_path: Path,
    *, token: Optional[str] = None,
) -> Path:
    """REMOVED — ARA must not write into ``~/.openclaw/exec-approvals.json``.

    Exec-approvals redirection happens entirely at the LD_PRELOAD libc
    shim layer: ``libara_shim.so`` watches for ``connect()`` calls
    targeting the openclaw exec-approvals socket path and rewrites the
    sockaddr to point at ``<state_root>/exec-approvals-tap.sock``
    instead. The agent's openclaw config remains authoritative.

    Kept as a callable surface that returns the would-be redirect path
    so existing callsites don't break, but emits no file write. Set
    ``ARA_DISABLE_EXEC_APPROVALS_TAP=1`` at the shim layer to opt out.
    """
    target = contained_home / ".openclaw" / "exec-approvals.json"
    return target


def looks_unsupervised(state_root: Path) -> bool:
    """True if openclaw's real exec-approvals socket is live but no
    ARA session currently has a proxy pointed at it. Used by the
    armory startup warning for non-contained runs."""
    if not DEFAULT_REAL_SOCKET.exists():
        return False
    # If no proxy socket for this state root, consider unsupervised.
    proxy_sock = state_root / "exec-approvals-tap.sock"
    return not proxy_sock.exists()
