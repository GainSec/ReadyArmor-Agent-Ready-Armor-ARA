"""OpenClaw gateway WebSocket tap (Stage 1 of the plan in
``docs/GATEWAY-WS-TAP-PLAN.md``).

Tap-only. Listens on a local TCP port, accepts TCP connections,
forwards every byte to the real gateway endpoint, relays the
reply back, and emits ``gateway_ws_tap`` events with byte counts
+ a utf-8-replacement preview. Does not parse HTTP upgrade headers,
WebSocket frames, or ACP envelopes — that's Stage 2.

Opt-in. ARA does NOT wire this up by default because redirecting
gateway clients (dashboards, channel bridges, ACP consumers)
requires rewriting the URL in their configs. Ship this as a
building block; operators flip it on once they're ready.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


DEFAULT_UPSTREAM_HOST = "127.0.0.1"
DEFAULT_UPSTREAM_PORT = 18789


@dataclass
class TapStats:
    connections_accepted: int = 0
    bytes_client_to_upstream: int = 0
    bytes_upstream_to_client: int = 0
    errors: int = 0
    acp_requests: int = 0
    acp_responses: int = 0
    acp_events: int = 0
    acp_denies: int = 0


@dataclass
class Handle:
    thread: threading.Thread
    stop_event: threading.Event
    listen_port: int
    upstream_host: str
    upstream_port: int
    state_root: "Path | None" = None
    stats: TapStats = field(default_factory=TapStats)

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=timeout)


def _default_sink_factory(events_path: Path) -> Callable[[dict], None]:
    def _sink(ev: dict) -> None:
        try:
            ev.setdefault("ts", time.time())
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass
    return _sink


def _pipe(
    src: socket.socket,
    dst: socket.socket,
    direction: str,
    client_id: int,
    stats: TapStats,
    event_sink: Callable[[dict], None],
    stop_event: threading.Event,
    state: "dict",
    state_root: "Path | None" = None,
    preview_limit: int = 256,
) -> None:
    """Byte pipe with frame-aware enforcement.

    Pre-upgrade: pure passthrough. Post-upgrade on c2u: each complete
    ACP request frame is evaluated against Bulwark before forwarding.
    A deny matches → a synthetic ARA_DENY response frame is sent to
    the client and the upstream connection is closed (safer than
    letting partial subsequent frames race through). u2c stays
    passthrough + observability only.
    """
    from . import acp_frames
    try:
        src.settimeout(0.5)
        while not stop_event.is_set():
            try:
                data = src.recv(65536)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                break
            if not data:
                break

            should_forward, deny_info = _emit_or_parse(
                data, direction, client_id, stats,
                event_sink, state, preview_limit, acp_frames,
                state_root=state_root,
            )
            if deny_info is not None and direction == "c2u":
                # Send the synthetic deny frame back to the client
                # (which is ``src`` on the c2u direction).
                try:
                    src.sendall(deny_info["wire_bytes"])
                except OSError:
                    pass
                event_sink({
                    "kind": "gateway_acp_deny",
                    "direction": direction,
                    "client_id": client_id,
                    "rule_id": deny_info.get("rule_id"),
                    "method": deny_info.get("method"),
                    "frame_id": deny_info.get("frame_id"),
                })
                stats.acp_denies += 1
                # Kill the upstream side so no more frames cross.
                stop_event.set()
                break

            if should_forward:
                try:
                    dst.sendall(data)
                except OSError:
                    break
                n = len(data)
                if direction == "c2u":
                    stats.bytes_client_to_upstream += n
                else:
                    stats.bytes_upstream_to_client += n
    finally:
        try:
            src.shutdown(socket.SHUT_RD)
        except OSError:
            pass


def _u2c_content_checks(
    body: bytes, client_id: int, env, event_sink: Callable[[dict], None],
    state_root: "Path",
) -> None:
    """Run registered classifiers + canary detection on a server→
    client ACP frame body. Emits ``classifier_hit`` and ``canary_hit``
    events (same shape the MITM reply-classifier emits).

    Non-enforcing: breaking a u2c frame mid-stream would strand the
    matching request on the client. Operators get observability
    instead — a caught canary or classifier flag surfaces on /audit
    as a verdict=flag event to act on.
    """
    try:
        content = body.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return
    if not content:
        return
    try:
        from . import classifiers as classifier_mod
        verdicts = classifier_mod.registry.classify_reply(content)
        for v in verdicts:
            event_sink({
                "kind": "classifier_hit",
                "client_id": client_id,
                "scope": "gateway_reply",
                "classifier": v.classifier,
                "action": v.action,
                "confidence": v.confidence,
                "reason": v.reason,
                "method": env.method,
                "frame_id": env.id,
            })
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import canary as canary_mod
        hits = canary_mod.check_content(state_root, content)
        for c in hits:
            canary_mod.record_hit(state_root, c.id)
            event_sink({
                "kind": "canary_hit",
                "client_id": client_id,
                "scope": "gateway_reply",
                "canary_id": c.id,
                "kind_canary": c.kind,
                "path": c.path,
                "method": env.method,
                "frame_id": env.id,
            })
    except Exception:  # noqa: BLE001
        pass


def _consult_bulwark_acp(
    state_root: "Path | None", method: str,
) -> "tuple[str, str, str] | None":
    """Return (action, rule_id, reason) if any Bulwark ``acp`` rule
    fires on ``method`` — action is ``"deny"`` or ``"approval"``.
    None means fall through to allow-and-forward."""
    if state_root is None or not method:
        return None
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
    event = {"layer": "acp", "method": method}
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
        return "deny", d.rule.rule_id, f"BULWARK_DENY:{d.rule.rule_id}"
    if d.action == "approval":
        return "approval", d.rule.rule_id, f"BULWARK_APPROVAL:{d.rule.rule_id}"
    return None


def _acp_approval_flow(
    state_root: "Path | None", env, rule_id: str, reason: str,
    event_sink: Callable[[dict], None], client_id: int,
) -> "tuple[bool, dict | None]":
    """Queue an approval for this ACP req and synchronously wait
    (with timeout) for the operator's verdict. Returns the same
    ``(should_forward, deny_info)`` shape ``_emit_or_parse`` returns.

    Approved/allow-always → forward the original frame.
    Denied/deny-always/timeout → synthesize ARA_DENY response.
    """
    if state_root is None:
        return True, None
    try:
        from . import approvals as _apr
    except Exception:  # noqa: BLE001
        return True, None
    # Pick a session_root to anchor the approval record. Prefer the
    # newest active session's dir; fall back to state_root itself so
    # records still land somewhere operators can see.
    sessions_dir = state_root / "sessions"
    session_root = None
    if sessions_dir.is_dir():
        candidates = [
            p for p in sessions_dir.iterdir()
            if p.is_dir() and (p / "session.json").exists()
        ]
        if candidates:
            session_root = max(candidates, key=lambda p: p.stat().st_mtime)
    if session_root is None:
        session_root = state_root

    approval_id = _apr.new_approval_id()
    import time as _t
    record = _apr.ApprovalRecord(
        id=approval_id,
        session_id=session_root.name,
        created_at=_t.time(),
        op=f"acp.{env.method}",
        command=env.method,
        args=[env.id or ""],
        rule_id=rule_id,
        rule_pattern=env.method,
    )
    _apr.write_pending(session_root, record)
    event_sink({
        "kind": "gateway_acp_approval_pending",
        "client_id": client_id,
        "approval_id": approval_id,
        "method": env.method,
        "rule_id": rule_id,
        "frame_id": env.id,
    })
    # Block this tap thread until the operator responds or we time out.
    resolved = _apr.wait_for_resolution_sync(session_root, approval_id)
    event_sink({
        "kind": "gateway_acp_approval_resolved",
        "client_id": client_id,
        "approval_id": approval_id,
        "status": resolved.status,
        "action": resolved.action,
    })
    if (resolved.action or "").startswith("allow"):
        return True, None
    # Operator said deny — or the request timed out. Synthesize an
    # ARA_DENY response and drop the session.
    try:
        from . import acp_frames as _af
    except Exception:  # noqa: BLE001
        return False, None
    wire = _af.synthesize_deny_response(
        request_id=env.id, rule_id=rule_id,
        reason=f"ARA_DENIED:{resolved.action or 'unknown'}",
    )
    return False, {
        "rule_id": rule_id,
        "method": env.method,
        "frame_id": env.id,
        "wire_bytes": wire,
    }


def _emit_or_parse(
    data: bytes, direction: str, client_id: int,
    stats: TapStats,
    event_sink: Callable[[dict], None],
    state: dict, preview_limit: int, acp_frames,
    *, state_root: "Path | None" = None,
) -> "tuple[bool, dict | None]":
    """Return ``(should_forward, deny_info)``.

    * ``should_forward=True, deny_info=None`` — caller writes ``data``
      through to ``dst`` unchanged.
    * ``should_forward=False, deny_info={..}`` — caller drops the
      chunk and sends ``deny_info['wire_bytes']`` back to the source.

    Pre-upgrade + plain passthrough: always forward. Only upgraded c2u
    frames carrying ACP req envelopes can trigger a deny.
    """
    # Accumulate into a per-direction buffer.
    buf_key = f"{direction}_buf"
    hdr_key = f"{direction}_header_done"
    state.setdefault(buf_key, bytearray())
    state.setdefault(hdr_key, False)
    state[buf_key].extend(data)
    deny_info: "dict | None" = None

    # Still in HTTP-upgrade phase: wait for end-of-headers, decide whether
    # to engage the WS parser for subsequent bytes.
    if not state[hdr_key]:
        idx = acp_frames.find_header_end(bytes(state[buf_key]))
        if idx < 0:
            return True, None
        headers = bytes(state[buf_key][:idx])
        is_upgrade = acp_frames.is_websocket_upgrade(headers)
        state[hdr_key] = True
        event_sink({
            "kind": "gateway_ws_tap_upgrade" if is_upgrade else "gateway_http_passthrough",
            "direction": direction,
            "client_id": client_id,
            "is_websocket": is_upgrade,
            "headers_preview": headers[:preview_limit].decode("utf-8", "replace"),
        })
        if is_upgrade:
            state["upgraded"] = True
        # Drop consumed headers.
        del state[buf_key][:idx]

    if not state.get("upgraded"):
        # Not a WS connection — plain TCP passthrough. Emit a coarse
        # tap event so operators still see activity.
        leftover = bytes(state[buf_key])
        if leftover:
            event_sink({
                "kind": "gateway_ws_tap_raw",
                "direction": direction,
                "client_id": client_id,
                "bytes": len(leftover),
                "preview": leftover[:preview_limit].decode("utf-8", "replace"),
            })
            state[buf_key].clear()
        return True, None

    # WebSocket phase: feed through the frame assembler, emit one
    # event per complete message.
    asm_key = f"{direction}_asm"
    if asm_key not in state:
        state[asm_key] = acp_frames.FrameAssembler()
    chunk = bytes(state[buf_key])
    state[buf_key].clear()
    messages = state[asm_key].feed(chunk)
    for body, opcode, was_text in messages:
        if opcode in (acp_frames.OP_PING, acp_frames.OP_PONG, acp_frames.OP_CLOSE):
            # Control frames — keep a thin trace.
            event_sink({
                "kind": "gateway_ws_control",
                "direction": direction,
                "client_id": client_id,
                "opcode": {0x8: "close", 0x9: "ping", 0xa: "pong"}.get(opcode),
            })
            continue
        env = acp_frames.parse_acp(body) if was_text else None
        if env is None:
            event_sink({
                "kind": "gateway_ws_frame",
                "direction": direction,
                "client_id": client_id,
                "bytes": len(body),
                "opcode": "text" if was_text else "binary",
                "preview": body[:preview_limit].decode("utf-8", "replace"),
            })
            continue
        ev = {
            "kind": "gateway_acp",
            "direction": direction,
            "client_id": client_id,
            "frame_kind": env.kind,
            "frame_id": env.id,
            "method": env.method,
            "params_summary": env.params_summary,
            "result_summary": env.result_summary,
            "error_code": env.error_code,
            "error_message": env.error_message,
        }
        if env.kind == "req":
            stats.acp_requests += 1
            # Bulwark enforcement point: consult acp rules for this
            # request. Deny → short-circuit with a synthesised ARA
            # response. Approval → queue + synchronously wait for
            # operator decision, then forward or deny accordingly.
            if direction == "c2u":
                match = _consult_bulwark_acp(state_root, env.method or "")
                if match is not None:
                    action, rule_id, reason = match
                    if action == "deny":
                        wire = acp_frames.synthesize_deny_response(
                            request_id=env.id, rule_id=rule_id, reason=reason,
                        )
                        deny_info = {
                            "rule_id": rule_id,
                            "method": env.method,
                            "frame_id": env.id,
                            "wire_bytes": wire,
                        }
                        event_sink(ev)
                        return False, deny_info
                    if action == "approval":
                        event_sink(ev)
                        should_forward, di = _acp_approval_flow(
                            state_root, env, rule_id, reason,
                            event_sink, client_id,
                        )
                        return should_forward, di
        elif env.kind == "res":
            stats.acp_responses += 1
        elif env.kind == "evt":
            stats.acp_events += 1
        event_sink(ev)

        # u2c enforcement: run classifiers + canary detection against
        # server→client frame bodies. Gateway-pushed channel messages
        # (Discord, Telegram, iMessage events) cross here, so canary
        # leakage + goal-drift phrasings are worth catching even if
        # the wire semantics make frame-dropping risky (clients expect
        # ordered replies; dropping a `res` leaves a req dangling).
        if direction == "u2c" and was_text and state_root is not None:
            try:
                _u2c_content_checks(body, client_id, env, event_sink, state_root)
            except Exception:  # noqa: BLE001
                pass

    return True, None


def _handle_client(
    client_sock: socket.socket,
    upstream_host: str,
    upstream_port: int,
    client_id: int,
    stats: TapStats,
    event_sink: Callable[[dict], None],
    stop_event: threading.Event,
    state_root: "Path | None" = None,
) -> None:
    try:
        upstream = socket.create_connection(
            (upstream_host, upstream_port), timeout=3.0,
        )
    except OSError as exc:
        stats.errors += 1
        event_sink({
            "kind": "gateway_ws_tap_error",
            "client_id": client_id,
            "error": repr(exc),
        })
        try:
            client_sock.close()
        except OSError:
            pass
        return

    event_sink({
        "kind": "gateway_ws_tap_open",
        "client_id": client_id,
        "upstream": f"{upstream_host}:{upstream_port}",
    })

    conn_state: dict = {}
    t1 = threading.Thread(
        target=_pipe, daemon=True,
        args=(client_sock, upstream, "c2u", client_id,
              stats, event_sink, stop_event, conn_state, state_root),
    )
    t2 = threading.Thread(
        target=_pipe, daemon=True,
        args=(upstream, client_sock, "u2c", client_id,
              stats, event_sink, stop_event, conn_state, state_root),
    )
    t1.start(); t2.start()
    t1.join(); t2.join()

    for s in (client_sock, upstream):
        try:
            s.close()
        except OSError:
            pass

    event_sink({
        "kind": "gateway_ws_tap_close",
        "client_id": client_id,
        "bytes_c2u": stats.bytes_client_to_upstream,
        "bytes_u2c": stats.bytes_upstream_to_client,
    })


def _serve_loop(handle: Handle, event_sink: Callable[[dict], None]) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", handle.listen_port))
    except OSError as exc:
        event_sink({
            "kind": "gateway_ws_tap_error",
            "detail": f"bind failed on 127.0.0.1:{handle.listen_port}: {exc!r}",
        })
        return
    # Resolve ephemeral port if caller passed 0.
    handle.listen_port = server.getsockname()[1]
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
                    args=(csock, handle.upstream_host, handle.upstream_port,
                          client_id, handle.stats, event_sink,
                          handle.stop_event, handle.state_root),
                ).start()
    finally:
        try:
            server.close()
        except OSError:
            pass


def start(
    state_root: Path,
    *,
    listen_port: int = 0,
    upstream_host: str = DEFAULT_UPSTREAM_HOST,
    upstream_port: int = DEFAULT_UPSTREAM_PORT,
    event_sink: Optional[Callable[[dict], None]] = None,
) -> Optional[Handle]:
    """Start the tap. Returns a Handle (with ``.stop()`` and the
    resolved ``.listen_port``). Always returns a handle — per-
    connection failures to reach the upstream surface as
    ``gateway_ws_tap_error`` events at handle-time, not start-time."""
    if event_sink is None:
        event_sink = _default_sink_factory(state_root / "events.jsonl")

    stop_event = threading.Event()
    handle = Handle(
        thread=None,  # populated below
        stop_event=stop_event,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        state_root=state_root,
    )

    def _runner():
        _serve_loop(handle, event_sink)

    t = threading.Thread(target=_runner, daemon=True, name="ara-gateway-ws-tap")
    handle.thread = t
    t.start()
    # Wait briefly for bind to resolve the actual port.
    for _ in range(20):
        if handle.listen_port != 0:
            break
        time.sleep(0.05)
    return handle


def maybe_start_from_env(state_root: Path) -> Optional[Handle]:
    """Opt-in entrypoint. If ``ARA_GATEWAY_WS_TAP_PORT`` is set in the
    env, start the tap on that port (or ephemeral if the value is 0).
    Otherwise return None."""
    val = os.environ.get("ARA_GATEWAY_WS_TAP_PORT")
    if val is None:
        return None
    try:
        port = int(val)
    except ValueError:
        return None
    upstream_host = os.environ.get("ARA_GATEWAY_WS_UPSTREAM_HOST",
                                   DEFAULT_UPSTREAM_HOST)
    upstream_port_env = os.environ.get("ARA_GATEWAY_WS_UPSTREAM_PORT")
    upstream_port = (
        int(upstream_port_env) if upstream_port_env
        else DEFAULT_UPSTREAM_PORT
    )
    return start(
        state_root, listen_port=port,
        upstream_host=upstream_host, upstream_port=upstream_port,
    )
