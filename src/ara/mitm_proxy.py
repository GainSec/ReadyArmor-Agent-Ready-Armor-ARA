"""ARA MITM HTTPS proxy.

Threaded, synchronous. Spawned as a session sidecar. Only MITMs requests
to known LLM hosts; everything else is a blind CONNECT tunnel.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import socket
import socketserver
import ssl
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import bulwark as bulwark_mod
from . import armors as armors_mod
from . import rhema as rhema_mod  # back-compat shim (see _rhema_shim.py)
from . import sse_synth
from . import classifiers as classifier_mod
from . import debug_log
from . import canary as canary_mod
from . import pause_and_ask_state as paas
from . import exec_deny_notices as _exec_deny_notices
from . import task_action_gate as task_action_gate_mod
from . import danger_class as danger_mod
from . import danger_registry as danger_registry_mod
from .mitm_ca import CA
from .transcript import TranscriptWriter


def _os_environ_get(name: str, default):
    return os.environ.get(name, default)


MITM_HOSTS = (
    "chatgpt.com",
    "api.openai.com",
    "api.anthropic.com",
    "api.x.ai",
    "generativelanguage.googleapis.com",
)

PROVIDER_BY_HOST = {
    "chatgpt.com": "codex",
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.x.ai": "xai",
    "generativelanguage.googleapis.com": "google",
}

_PSEUDO_TASK_CHANNELS = {
    "fact.assert",
    "lead.file",
    "finding.file",
}


def _provider_for(host: str) -> str:
    for h, p in PROVIDER_BY_HOST.items():
        if host.endswith(h):
            return p
    return "unknown"


def _tool_choice_for_filtered_tools(tools: list[dict]) -> str | dict:
    if len(tools) == 1 and isinstance(tools[0], dict):
        fn = tools[0].get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name == "mcp_ara_forge_chunks_emit_tag":
            return {"type": "function", "function": {"name": name}}
    return "required"


def _coerce_pseudo_param(value: str) -> object:
    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text


def _canonical_pseudo_tool_name(name: str) -> str | None:
    from .task_channel_gate import BAD_CHANNEL_ALIASES

    canonical = BAD_CHANNEL_ALIASES.get(name, name)
    if canonical in _PSEUDO_TASK_CHANNELS:
        return canonical
    return None


def _parse_pseudo_tool_calls(text: str) -> list[tuple[str, dict]]:
    if not text or "<tool_call>" not in text:
        return []
    calls: list[tuple[str, dict]] = []
    for block in re.finditer(
        r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>",
        text,
        flags=re.DOTALL,
    ):
        channel = _canonical_pseudo_tool_name(block.group(1).strip())
        if channel is None:
            continue
        body: dict = {}
        inner = block.group(2)
        for param in re.finditer(
            r"<parameter=([^>\s]+)>(.*?)</parameter>",
            inner,
            flags=re.DOTALL,
        ):
            key = param.group(1).strip()
            if not key:
                continue
            body[key] = _coerce_pseudo_param(param.group(2))
        calls.append((channel, body))
    return calls


def _system_prompt_chars(payload: dict | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    instructions = payload.get("instructions")
    if isinstance(instructions, str):
        return len(instructions)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    total = 0
    found = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
            found = True
    return total if found else None


def _tool_count(payload: dict | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    tools = payload.get("tools")
    if isinstance(tools, list):
        return len(tools)
    return None


def _pseudo_text_from_response_payload(payload: dict) -> str:
    pieces: list[str] = []
    chunks = payload.get("chunks")
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            if not isinstance(first, dict):
                continue
            delta = first.get("delta")
            if not isinstance(delta, dict):
                continue
            for key in ("reasoning_content", "content"):
                value = delta.get(key)
                if isinstance(value, str):
                    pieces.append(value)
    body_text = payload.get("body_text")
    if isinstance(body_text, str) and "<tool_call>" in body_text:
        for raw_line in body_text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            if not isinstance(first, dict):
                continue
            delta = first.get("delta")
            if not isinstance(delta, dict):
                continue
            for key in ("reasoning_content", "content"):
                value = delta.get(key)
                if isinstance(value, str):
                    pieces.append(value)
    return "".join(pieces)


def _dispatch_pseudo_task_channel_calls(
    state_root: Path,
    session_dir: Path,
    payload: dict,
) -> int:
    calls = _parse_pseudo_tool_calls(_pseudo_text_from_response_payload(payload))
    if not calls:
        return 0
    debug_log.emit(
        "mitm", "pseudo_tool_calls.detected",
        state_root=state_root,
        session_id=session_dir.name,
        calls=[{"channel": name, "body": body} for name, body in calls],
    )

    from . import weapons as weapons_mod
    from .task_channel_adapt import (
        adapt_task_channel_args,
        drop_unresolved_evidence_refs,
    )
    from .runtime.binder.api import EvidenceBinder
    from .runtime.bootstrap import install_engagement_channels
    from .runtime.channels import registry as _registry
    from .runtime.channels.base import ChannelContext, ChannelRejection

    weapon_dir = None
    try:
        weapon_id = weapons_mod.equipped_weapon_id(state_root)
        if weapon_id:
            weapon_dir = weapons_mod.weapon_dir(state_root, weapon_id)
    except Exception:
        weapon_dir = None
    install_engagement_channels(
        sessions_root=session_dir.parent,
        weapon_dir=weapon_dir,
        engagement_id=session_dir.name,
    )
    ctx = ChannelContext(
        engagement_id=session_dir.name,
        activation_id="_mitm_pseudo_tool_call",
        caller="agent",
        audit_sink=session_dir / "events.jsonl",
    )
    accepted = 0
    recent_evidence_refs = _latest_session_evidence_refs(session_dir, limit=3)
    binder = EvidenceBinder(session_dir)
    try:
        for channel_name, raw_body in calls:
            cleaned_body = drop_unresolved_evidence_refs(
                raw_body,
                lambda evidence_id: binder.get_record(evidence_id) is not None,
            )
            body = adapt_task_channel_args(
                channel_name, cleaned_body, recent_evidence_refs,
            )
            task_rejection = task_action_gate_mod.task_commit_channel_rejection_reason(
                state_root,
                channel_name,
            )
            if task_rejection:
                debug_log.emit(
                    "mitm", "pseudo_tool_call.task_rejected",
                    state_root=state_root,
                    session_id=session_dir.name,
                    channel=channel_name,
                    body=body,
                    reason=task_rejection,
                )
                continue
            try:
                channel = _registry.get(channel_name)
                channel.call(body, ctx)
            except (KeyError, ChannelRejection):
                debug_log.emit(
                    "mitm", "pseudo_tool_call.rejected",
                    state_root=state_root,
                    session_id=session_dir.name,
                    channel=channel_name,
                    body=body,
                    error_kind="channel_rejection_or_unknown",
                )
                continue
            except Exception as exc:
                debug_log.emit_exception(
                    "mitm", "pseudo_tool_call.error",
                    exc,
                    state_root=state_root,
                    session_id=session_dir.name,
                    channel=channel_name,
                    body=body,
                )
                continue
            if channel_name in {"fact.assert", "lead.file", "finding.file", "chunks.emit_tag"}:
                try:
                    task_action_gate_mod.record_commit(state_root, channel_name)
                except Exception:
                    pass
            debug_log.emit(
                "mitm", "pseudo_tool_call.accepted",
                state_root=state_root,
                session_id=session_dir.name,
                channel=channel_name,
                commit_channel=task_action_gate_mod.is_commit_channel(channel_name),
                body=body,
            )
            accepted += 1
    finally:
        try:
            binder.close()
        except Exception:
            pass
    return accepted


def _latest_session_evidence_refs(session_dir: Path, *, limit: int = 3) -> list[str]:
    db_path = Path(session_dir) / "evidence" / "index.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id FROM evidence ORDER BY captured_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        str(row[0])
        for row in rows
        if row and isinstance(row[0], str) and row[0]
    ]


def _should_mitm(host: str) -> bool:
    for h in MITM_HOSTS:
        if host == h or host.endswith("." + h):
            return True
    return False


def _read_line(sock: socket.socket) -> bytes:
    """Read a single \\r\\n-terminated line (small allocation). EOF → b''."""
    out = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            return bytes(out)
        out += b
        if out.endswith(b"\n"):
            return bytes(out)


def _force_connection_close(raw_headers: bytes) -> bytes:
    """Rewrite the response header block so it carries ``Connection: close``.

    The MITM closes the client TLS socket after every relayed response.
    Without an explicit ``Connection: close`` header, pooled HTTP clients
    (httpx, urllib3) treat the close as an unexpected peer-close on the
    *next* request they try to send through that connection. Honest
    framing prevents the spurious error and avoids retry storms.
    """
    if not raw_headers:
        return raw_headers
    sep = b"\r\n\r\n"
    head, sepfound, tail = raw_headers.partition(sep)
    if not sepfound:
        return raw_headers
    lines = head.split(b"\r\n")
    kept = [lines[0]] if lines else []
    for line in lines[1:]:
        name, _, _ = line.partition(b":")
        if name.strip().lower() in (b"connection", b"keep-alive", b"proxy-connection"):
            continue
        kept.append(line)
    kept.append(b"Connection: close")
    return b"\r\n".join(kept) + sep + tail


def _reframe_body_as_content_length(raw_headers: bytes, body: bytes) -> bytes:
    """Replace ``Transfer-Encoding: chunked`` with ``Content-Length: N``.

    The buffered response path runs ``_read_body``, which strips chunked
    framing and returns the de-chunked bytes. If we forward the original
    headers (still claiming ``Transfer-Encoding: chunked``) plus those
    de-chunked bytes, the client's HTTP parser tries to decode the body
    as chunked-encoded and reports "Malformed encoding found in
    chunked-encoding". Honest framing: declare what we actually send.
    """
    if not raw_headers:
        return raw_headers
    sep = b"\r\n\r\n"
    head, sepfound, tail = raw_headers.partition(sep)
    if not sepfound:
        return raw_headers
    lines = head.split(b"\r\n")
    kept = [lines[0]] if lines else []
    had_te = False
    for line in lines[1:]:
        name, _, _ = line.partition(b":")
        n = name.strip().lower()
        if n == b"transfer-encoding":
            had_te = True
            continue
        if n == b"content-length":
            continue  # we'll re-emit
        kept.append(line)
    kept.append(f"Content-Length: {len(body)}".encode("ascii"))
    if had_te:
        # Optional: signal to downstream caches that we re-framed.
        kept.append(b"X-ARA-Reframed: chunked-to-content-length")
    return b"\r\n".join(kept) + sep + tail


def _read_headers(sock: socket.socket) -> tuple[bytes, dict]:
    raw = bytearray()
    while True:
        line = _read_line(sock)
        if not line:
            break
        raw += line
        if line in (b"\r\n", b"\n"):
            break
    text = bytes(raw).decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    out = {"_lines": lines, "start": lines[0] if lines else "", "headers": {}}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            out["headers"][k.strip().lower()] = v.strip()
    return bytes(raw), out


def _read_body(
    sock: socket.socket,
    headers: dict,
    cap: int = 4 * 1024 * 1024,
    *,
    is_request: bool = False,
) -> bytes:
    cl = headers.get("content-length")
    te = (headers.get("transfer-encoding") or "").lower()
    # Request bodies always have an explicit length signal (Content-Length
    # or chunked Transfer-Encoding). Without one, the body is empty —
    # falling through to the read-to-EOF fallback would hang forever
    # because HTTP/1.1 clients don't close after sending the request,
    # they wait for our response.
    if is_request and cl is None and "chunked" not in te:
        return b""
    if cl is not None:
        try:
            n = min(int(cl), cap)
        except ValueError:
            return b""
        out = bytearray()
        while len(out) < n:
            chunk = sock.recv(min(8192, n - len(out)))
            if not chunk:
                break
            out += chunk
        return bytes(out)
    if "chunked" in te:
        out = bytearray()
        while True:
            size_line = _read_line(sock)
            if not size_line:
                break
            size_hex = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_hex, 16)
            except ValueError:
                break
            if size == 0:
                _read_line(sock)
                break
            while size > 0:
                chunk = sock.recv(min(8192, size))
                if not chunk:
                    break
                out += chunk
                size -= len(chunk)
            _read_line(sock)  # CRLF between chunks
            if len(out) >= cap:
                break
        return bytes(out[:cap])
    # Fallback: read to EOF with cap.
    out = bytearray()
    while len(out) < cap:
        chunk = sock.recv(min(8192, cap - len(out)))
        if not chunk:
            break
        out += chunk
    return bytes(out)


class MitmProxy:
    _TASK_COMMIT_MCP_TOOLS = {
        "mcp_ara_forge_fact_assert",
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_finding_file",
        "mcp_ara_forge_chunks_emit_tag",
    }
    _TASK_COMMIT_MCP_TOOL_ORDER = [
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_chunks_emit_tag",
        "mcp_ara_forge_fact_assert",
        "mcp_ara_forge_finding_file",
    ]

    def __init__(self, state_root: Path, session_dir: Path) -> None:
        self.state_root = state_root
        self.session_dir = session_dir
        self.ca = CA(state_root)
        self.ca.ensure()
        self.transcript = TranscriptWriter(session_dir / "transcript.jsonl")
        self._ctx_cache: dict[str, ssl.SSLContext] = {}
        self._cache_lock = threading.Lock()
        # Bulwark cache — loaded from <state_root>/bulwark.md, re-read on
        # mtime change. Per-proxy RuntimeState tracks counters/ttl across
        # requests so xN rules behave consistently on this MITM instance.
        self._bulwark: "bulwark_mod.Bulwark" = bulwark_mod.Bulwark()
        self._bulwark_mtime: float | None = None
        self._bulwark_state: "bulwark_mod.RuntimeState" = bulwark_mod.RuntimeState()
        self._bulwark_lock = threading.Lock()
        # Fix-1 migration: legacy ``abs-sync`` managed Rhema rules are no
        # longer written. Any that exist in rhema.json from an older ARA
        # build would bloat the rule list. Purge once on spin-up; the
        # ABSOLUTE.md file is now read live per-request instead.
        try:
            rhema_mod.purge_managed(state_root, "abs-sync")
        except Exception:  # noqa: BLE001
            pass

    def _bulwark_current(self) -> "bulwark_mod.Bulwark":
        """Return the live bulwark rule set, re-parsing on mtime change."""
        path = self.state_root / "bulwark.md"
        try:
            if not path.exists():
                return self._bulwark
            mtime = path.stat().st_mtime
        except OSError:
            return self._bulwark
        with self._bulwark_lock:
            if self._bulwark_mtime == mtime:
                return self._bulwark
            try:
                self._bulwark = bulwark_mod.parse_file(path)
                self._bulwark_mtime = mtime
                # Reset counters on reload (rule IDs regenerate).
                self._bulwark_state = bulwark_mod.RuntimeState()
            except Exception:  # noqa: BLE001
                pass
            return self._bulwark

    def _bulwark_evaluate_http(
        self,
        *,
        scope: str,                       # "request" | "response"
        host: str,
        method: str,
        url_path: str,
        body_json: object,
        body_text: str,
    ) -> "bulwark_mod.Decision | None":
        """Evaluate bulwark rules against a MITM'd HTTP event.

        Consults the ``url`` layer (method + host + path) and the
        ``ask``/``reply`` content layer (body text) depending on scope.
        Returns the first firing Decision, or None if no rule matched.
        """
        bw = self._bulwark_current()
        if not bw.rules:
            return None
        try:
            armor_id = armors_mod.active_armor_name(self.state_root)
        except Exception:  # noqa: BLE001
            armor_id = None

        # URL-layer policy describes task-plane web targets. Known MITM
        # hosts are control-plane LLM transports; their network reach is
        # governed at CONNECT/net, while ask/reply content stays inspected
        # below.
        if not _should_mitm(host):
            url = f"https://{host}{url_path}" if url_path else f"https://{host}"
            url_event = {
                "layer": "url", "method": (method or "").upper(),
                "host": host, "path": url_path, "url": url,
            }
            d = bulwark_mod.evaluate(
                bw, url_event, armor_id=armor_id, state=self._bulwark_state,
            )
            if d.rule is not None:
                return d

        # Content layer — ask for request scope, reply for response.
        content_layer = "ask" if scope == "request" else "reply"
        content = body_text or ""
        if not content and body_json is not None:
            try:
                content = json.dumps(body_json, separators=(",", ":"))
            except Exception:  # noqa: BLE001
                content = ""
        if content:
            content_event = {"layer": content_layer, "content": content}
            d2 = bulwark_mod.evaluate(
                bw, content_event, armor_id=armor_id,
                state=self._bulwark_state,
            )
            if d2.rule is not None:
                return d2
        return None

    def _bulwark_allows_connect(self, host: str, port: int) -> bool:
        """Return whether Bulwark allows this proxy CONNECT target.

        This is deliberately allowlist-aware for the network boundary:
        an explicit applicable ``allow net`` lets forge reach its LLM or
        local control-plane target before a broader default-deny
        ``deny net *:*`` is applied. General Bulwark evaluation still
        keeps deny-over-allow semantics for normal rule decisions.
        """
        bw = self._bulwark_current()
        if not bw.rules:
            return True
        try:
            armor_id = armors_mod.active_armor_name(self.state_root)
        except Exception:  # noqa: BLE001
            armor_id = None
        event = {"layer": "net", "host": host, "port": port}
        applicable = [
            r for r in bw.rules
            if r.layer == "net" and bulwark_mod.scope_matches(r.scope, armor_id)
        ]
        for rule in applicable:
            if rule.action == "allow" and bulwark_mod.rule_matches(rule, event):
                return True
        for rule in applicable:
            if rule.action == "deny" and bulwark_mod.rule_matches(rule, event):
                self._log(
                    "bulwark_deny",
                    {
                        "rule_id": rule.rule_id,
                        "scope": "connect",
                        "layer": "net",
                        "pattern": rule.match.primary,
                        "host": host,
                        "port": port,
                    },
                    model=_provider_for(host),
                )
                return False
        return True

    def _server_ctx_for(self, host: str) -> ssl.SSLContext:
        with self._cache_lock:
            if host in self._ctx_cache:
                return self._ctx_cache[host]
            key, crt = self.ca.sign_leaf(host)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(crt), keyfile=str(key))
            self._ctx_cache[host] = ctx
            return ctx

    def _stream_response_body(
        self,
        src: socket.socket,
        dst: socket.socket,
        *,
        cap: int = 2 * 1024 * 1024,
        chunk: int = 8192,
    ) -> bytes:
        """Byte-pump ``src`` → ``dst`` while recording up to ``cap`` bytes.

        Forwards every chunk to the client immediately (so long SSE streams
        like Codex/Responses API stay alive), and accumulates a bounded
        copy for transcript recording. Returns the bytes actually captured
        — truncated with a trailing marker when the cap trips. Stops only
        when the source closes or errors.
        """
        buf = bytearray()
        over_cap = False
        while True:
            try:
                data = src.recv(chunk)
            except (ConnectionResetError, ssl.SSLError, OSError):
                break
            if not data:
                break
            # Forward to client first — do NOT block that path on capture
            # errors.
            try:
                dst.sendall(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Client went away; stop relaying but keep draining briefly
                # so the upstream can close cleanly.
                try:
                    src.settimeout(1.0)
                except Exception:
                    pass
                try:
                    while True:
                        more = src.recv(chunk)
                        if not more:
                            break
                except Exception:
                    pass
                break
            if not over_cap:
                room = cap - len(buf)
                if room > 0:
                    buf.extend(data[:room])
                if len(buf) >= cap:
                    over_cap = True
        return bytes(buf)

    def _channel_for(self, req_body: bytes) -> Optional[str]:
        """Best-effort channel classification for an outgoing request.
        Cheap helper used by every Tier-B enforcer + the recorder so
        every transcript line emitted for one turn carries the same
        channel tag. Swallows decode/parse errors and returns None
        (treated as "unknown" by the dashboard filters)."""
        try:
            from . import armors as _armors_mod
            text = req_body.decode("utf-8", errors="replace") if req_body else ""
            try:
                rj = json.loads(text) if text else None
            except Exception:  # noqa: BLE001
                rj = None
            return _armors_mod.detect_channel(rj or {})
        except Exception:  # noqa: BLE001
            return None

    def _log(
        self, role: str, payload: dict, *,
        model: str = "proxy", channel: Optional[str] = None,
    ) -> None:
        try:
            rec = {
                "kind": "transcript", "role": role, "payload": payload,
                "pid": os.getpid(), "model": model,
            }
            if channel:
                # Detected channel id (e.g. "discord", "imessage"). Lets
                # /shadows + /audit + /rhema filter by source channel
                # without re-parsing the request body.
                rec["channel"] = channel
            self.transcript.append(rec)
        except Exception:
            pass

    def handle(self, client_sock: socket.socket) -> None:
        try:
            raw, parsed = _read_headers(client_sock)
            if not raw:
                return
            start = parsed["start"]
            if start.startswith("CONNECT "):
                self._handle_connect(start, client_sock)
            else:
                self._handle_plain_http(start, parsed, client_sock)
        except Exception as exc:
            self._log("mitm.error", {"exc": repr(exc)})
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def _handle_connect(self, start: str, client_sock: socket.socket) -> None:
        parts = start.split()
        if len(parts) < 2:
            return
        host, _, port_str = parts[1].rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            return
        if not self._bulwark_allows_connect(host, port):
            try:
                client_sock.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Connection: close\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 28\r\n\r\n"
                    b"ARA policy blocked CONNECT.\n"
                )
            except Exception:
                pass
            return
        if self._try_enforce_task_connect(host, port, client_sock):
            return
        if not _should_mitm(host):
            self._blind_tunnel(host, port, client_sock)
            return

        # Acknowledge CONNECT before TLS handshake.
        client_sock.sendall(
            b"HTTP/1.1 200 Connection established\r\nProxy-agent: ARA-MITM\r\n\r\n"
        )
        try:
            ctx = self._server_ctx_for(host)
            tls_client = ctx.wrap_socket(client_sock, server_side=True)
        except Exception as exc:
            self._log("mitm.tls_error", {"host": host, "exc": repr(exc)})
            return

        try:
            req_raw, req_parsed = _read_headers(tls_client)
            if not req_raw:
                return
            req_body = _read_body(tls_client, req_parsed["headers"], is_request=True)

            # --- Rhema request-scope enforcement (Tier-B) --------------
            # Order of checks:
            #  1. deny  → synthesize a response; don't call upstream.
            #  2. rewrite → mutate the body before sending upstream; the
            #               agent receives the response to the mutated
            #               request, not the original.
            # Both paths match supervisor-path semantics so one rule
            # behaves the same regardless of which worker runs.
            provider = _provider_for(host)
            # Bulwark-first: url + ask layers consulted before rhema. A
            # deny short-circuits; approval is treated the same as a deny
            # at this layer until per-rule approval surfaces on /bulwark.
            if self._try_enforce_bulwark_request(
                host, port, req_parsed, req_body, provider, tls_client,
            ):
                return
            if self._try_enforce_request_deny(
                host, port, req_parsed, req_body, provider, tls_client,
            ):
                return

            # Approval rules block the turn until the operator answers.
            # If operator denies (or the request times out) we synthesize
            # a deny reply the same way the deny path does.
            approval_outcome = self._try_enforce_request_approval(
                host, port, req_parsed, req_body, provider, tls_client,
            )
            if approval_outcome == "deny":
                return

            original_body_for_record = req_body
            # Provenance capture: bind the agent's tool call+output exchanges
            # (assistant tool_calls paired with their role:tool results) from this
            # LLM request into the evidence binder. Read-only, best-effort — never
            # affects the request. This is the universal capture of what the agent
            # ran and observed, including in-process work the shim can't see.
            self._try_capture_tool_exchanges(
                host, req_parsed, original_body_for_record,
            )
            rewritten = self._try_apply_request_rewrite(
                host, port, req_parsed, req_body, provider,
            )
            if rewritten is not None:
                req_body = rewritten
                # Rebuild req_raw so Content-Length matches the mutated
                # body. Keep method/path/headers minus the body-shape
                # ones that we must recompute.
                req_raw = self._rebuild_request_raw(req_parsed, req_body)

            # v0.2 pause-and-ask: the agent runtime (e.g. openclaw
            # 2026.4.x) doesn't emit ``prompt_intercept`` IPC events,
            # so the supervisor's inject path never fires. Inject the
            # classification system message here, on the bytes-on-wire
            # request to the LLM provider, before forwarding upstream.
            paas_injected = self._try_apply_pause_and_ask_inject(
                host, req_parsed, req_body, provider,
            )
            if paas_injected is not None:
                req_body = paas_injected
                req_raw = self._rebuild_request_raw(req_parsed, req_body)

            # Exec-deny feedback: if Bulwark hard-blocked one or more of
            # the agent's exec commands since its last turn, prepend a
            # one-shot system message naming what was blocked and telling
            # the agent to skip and continue, so it stops re-issuing the
            # denied command. Best-effort; a failure never affects
            # forwarding. Runs after pause-and-ask so both can inject.
            edn_injected = self._try_apply_exec_deny_notice(
                host, req_parsed, req_body, provider,
            )
            if edn_injected is not None:
                req_body = edn_injected
                req_raw = self._rebuild_request_raw(req_parsed, req_body)

            # Forward to real upstream.
            upstream = socket.create_connection((host, port), timeout=30)
            upstream_ctx = ssl.create_default_context()
            up_tls = upstream_ctx.wrap_socket(upstream, server_hostname=host)
            # Connect deadline is done; switch the TLS socket to blocking
            # reads so long SSE streams (qwen reasoning + answer can
            # easily exceed 30s) aren't cut off by the inherited connect
            # timeout. The streaming loop detects client-side hangups
            # via dst.sendall failure, so a hung upstream still cleans
            # up correctly.
            try:
                up_tls.settimeout(None)
            except OSError:
                pass
            try:
                up_tls.sendall(req_raw)
                if req_body:
                    up_tls.sendall(req_body)

                resp_raw, resp_parsed = _read_headers(up_tls)
                # Branch on whether any response-scope deny rules exist.
                # Without them: stream pass-through, headers go out
                # immediately so SSE clients see progressive output.
                # With them: buffer the full response (capped), evaluate
                # response-scope deny rules, and either pass it through
                # or replace it with synthesize_deny_sse(). The buffer
                # mode kills progressive streaming for that turn — a
                # documented trade-off for response-scope enforcement.
                # Force buffered path if we just injected a pause-and-ask
                # request; we need the full response body to parse the
                # classification JSON. Without this, an SSE-streamed
                # response would slip past us un-consumed.
                paas_pending_consume = (
                    paas_injected is not None
                    or paas.has_asked_pending(self.state_root)
                )
                buffer_for_response_eval = (
                    self._has_response_deny_rules()
                    or self._has_bulwark_reply_rules()
                    or paas_pending_consume
                )
                if buffer_for_response_eval:
                    resp_body = self._read_full_body(
                        up_tls, resp_parsed, cap=2 * 1024 * 1024,
                    )
                    resp_text = resp_body.decode("utf-8", errors="replace")
                    # Run content classifiers against the reply body.
                    # Non-blocking (audit-only) — fires classifier_hit
                    # events that operators can see on /audit.
                    try:
                        self._run_reply_classifiers(resp_text, provider)
                    except Exception:  # noqa: BLE001
                        pass
                    # Canary-hit check: any seeded honeytoken echoed back
                    # in the response is a hard signal of exfiltration.
                    try:
                        self._check_canaries_in_reply(resp_text, provider)
                    except Exception:  # noqa: BLE001
                        pass
                    # v0.2 pause-and-ask: try to parse a classification
                    # JSON out of the response and write the registry.
                    if paas_pending_consume:
                        try:
                            self._try_consume_pause_and_ask(
                                provider, resp_text,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    matched = self._try_enforce_bulwark_response(
                        host, port, resp_parsed, resp_body, provider,
                        tls_client,
                    ) or self._try_enforce_response_deny(
                        host, port, resp_parsed, resp_body, provider,
                        tls_client,
                    )
                    if not matched:
                        # No deny match — relay headers + body together.
                        # ``resp_body`` came from _read_full_body which
                        # de-chunks; we MUST drop Transfer-Encoding: chunked
                        # and switch to Content-Length so the client doesn't
                        # try to chunk-decode an already-decoded payload.
                        try:
                            relayed_headers = _reframe_body_as_content_length(
                                resp_raw, resp_body or b"",
                            )
                            relayed_headers = _force_connection_close(relayed_headers)
                            tls_client.sendall(relayed_headers)
                            if resp_body:
                                tls_client.sendall(resp_body)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                else:
                    # Streaming pass-through — original behavior.
                    # Force ``Connection: close`` so the client knows the
                    # MITM will close this socket after the response
                    # completes. Without this, httpx-style pools see the
                    # close as a server-side error on the next request.
                    tls_client.sendall(_force_connection_close(resp_raw))
                    resp_body = self._stream_response_body(
                        up_tls, tls_client,
                        cap=2 * 1024 * 1024,
                    )
                # Record the ORIGINAL request body (not mutated) so the
                # transcript shows what the agent actually asked for.
                # The rewrite event is logged separately by
                # _try_apply_request_rewrite.
                self._record(
                    host, port, req_parsed,
                    original_body_for_record, resp_parsed, resp_body,
                    rewrite_was_applied=(rewritten is not None),
                )
            finally:
                try:
                    up_tls.close()
                except Exception:
                    pass
        finally:
            try:
                tls_client.close()
            except Exception:
                pass

    def _try_enforce_task_connect(
        self, host: str, port: int, client_sock: socket.socket,
    ) -> bool:
        if _should_mitm(host):
            return False
        decision = task_action_gate_mod.evaluate_query(
            self.state_root,
            {
                "type": "query",
                "op": "net.connect",
                "host": host,
                "port": port,
            },
        )
        if decision is None:
            debug_log.emit(
                "mitm", "connect.allow",
                state_root=self.state_root,
                session_id=self.session_dir.name,
                host=host,
                port=port,
            )
            return False
        try:
            body = (
                f"ARA task gate blocked CONNECT: "
                f"{decision.reason or 'TASK_ACTION_DENIED'}\n"
            ).encode("utf-8")
            client_sock.sendall(
                b"HTTP/1.1 403 Forbidden\r\n"
                b"Connection: close\r\n"
                b"Content-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
        except Exception:
            pass
        self._log(
            "task_action_deny",
            {
                "host": host,
                "port": port,
                "reason": decision.reason,
                "rule_id": decision.rule_id,
            },
            model="target",
        )
        debug_log.emit(
            "mitm", "connect.deny",
            state_root=self.state_root,
            session_id=self.session_dir.name,
            host=host,
            port=port,
            reason=decision.reason,
            rule_id=decision.rule_id,
        )
        return True

    def _try_enforce_bulwark_request(
        self, host: str, port: int, req_parsed: dict, req_body: bytes,
        provider: str, tls_client: socket.socket,
    ) -> bool:
        """Consult Bulwark for the incoming request (url + ask layers).

        Returns True on a bulwark deny/approval match; the caller then
        skips the legacy rhema + upstream paths. Rewrites are not
        applied here — that would require re-serializing the body which
        is best done as its own path.
        """
        try:
            path = self._path(req_parsed["start"])
            method = self._method(req_parsed["start"])
            req_text = req_body.decode("utf-8", errors="replace") if req_body else ""
            try:
                req_json = json.loads(req_text) if req_text else None
            except Exception:  # noqa: BLE001
                req_json = None
            decision = self._bulwark_evaluate_http(
                scope="request",
                host=host, method=method, url_path=path,
                body_json=req_json, body_text=req_text,
            )
            if decision is None or decision.rule is None:
                return False
            if decision.action not in ("deny", "approval"):
                # allow / rewrite / shadow_rewrite: let rhema + upstream
                # decide. Allow is primarily for the UI to show "this
                # request matched a bulwark allow" without preempting
                # further evaluation.
                return False
        except Exception as exc:  # noqa: BLE001
            self._log(
                "bulwark_error",
                {"scope": "request", "exc": repr(exc)},
                model=provider,
            )
            return False

        info = sse_synth.DenyInfo(
            rule_id=decision.rule.rule_id,
            text=f"ARA policy (bulwark rule {decision.rule.rule_id}) blocked "
                 f"this request.",
        )
        try:
            tls_client.sendall(sse_synth.synthesize_deny(info, provider))
        except Exception as exc:  # noqa: BLE001
            self._log(
                "bulwark_enforce_error",
                {"rule_id": decision.rule.rule_id, "exc": repr(exc)},
                model=provider,
            )
            return True
        self._log(
            "bulwark_deny",
            {
                "rule_id": decision.rule.rule_id,
                "scope": "request",
                "layer": decision.rule.layer,
                "pattern": decision.rule.match.primary,
                "highlight": decision.highlight,
            },
            model=provider,
        )
        return True

    def _try_enforce_request_deny(
        self, host: str, port: int, req_parsed: dict, req_body: bytes,
        provider: str, tls_client: socket.socket,
    ) -> bool:
        """Evaluate request-scope Rhema rules. If a deny rule matches,
        send a synthetic SSE reply to ``tls_client`` and return True so
        the caller skips the upstream call. Returns False on no match or
        on any evaluator error (fail-open on the shadow path is already
        the norm; enforcement follows the same posture to avoid turning
        a Rhema bug into an outage)."""
        channel = self._channel_for(req_body)
        try:
            req_text = req_body.decode("utf-8", errors="replace") if req_body else ""
            try:
                req_json = json.loads(req_text) if req_text else None
            except Exception:  # noqa: BLE001
                req_json = None
            rule = rhema_mod.evaluate(
                self.state_root, "request",
                {"body": req_json, "headers": req_parsed.get("headers") or {}},
            )
            if rule is None or rule.action != "deny":
                return False
        except Exception as exc:  # noqa: BLE001
            self._log("rhema_error", {"scope": "request", "exc": repr(exc)},
                      model=provider, channel=channel)
            return False

        try:
            wire_bytes = rhema_mod.synthesize_deny_sse(rule, provider)
            tls_client.sendall(wire_bytes)
        except Exception as exc:  # noqa: BLE001
            # If the client disconnected mid-synthesis, fall through —
            # the connection will close, the agent will retry.
            self._log("rhema_enforce_error",
                      {"rule_id": rule.id, "exc": repr(exc)},
                      model=provider, channel=channel)
            return True  # still skip upstream; we attempted the deny

        # Record what we did so it shows up on /rhema, /audit, transcripts.
        self._log(
            "request",
            {
                "url": f"https://{host}:{port}{self._path(req_parsed['start'])}",
                "method": self._method(req_parsed["start"]),
                "headers": {k: v for k, v in (req_parsed.get("headers") or {}).items()
                            if k not in ("authorization", "cookie")},
                "body": req_json if 'req_json' in locals() else None,
                "body_text": None if 'req_json' in locals() and req_json is not None
                             else (req_text[:32_000] if 'req_text' in locals() else None),
                "ara_enforced_deny": True,
                "rule_id": rule.id,
            },
            model=provider, channel=channel,
        )
        self._log(
            "rhema_deny",
            {
                "rule_id": rule.id, "pattern": rule.pattern,
                "scope": rule.scope, "redirect": rule.redirect,
                "reason": rule.reason or "RHEMA_RULE_FIRED",
                "enforced": True,
                "path": "mitm",
                "note": "request-scope deny; synthetic reply sent, upstream skipped",
            },
            model=provider, channel=channel,
        )
        return True

    def _has_response_deny_rules(self) -> bool:
        """True iff any rule in the operator's rhema.json is a
        response-scope deny. Used to decide whether to switch to the
        slower buffer-then-evaluate path on the response side. Cached
        per-mitm-instance for one second so the rule list doesn't get
        re-loaded on every single turn."""
        import time as _time
        cache_at = getattr(self, "_resp_deny_cache_at", 0.0)
        now = _time.time()
        if now - cache_at < 1.0:
            return getattr(self, "_resp_deny_cache", False)
        try:
            rules = rhema_mod.load(self.state_root)
            has = any(
                r.scope in ("response", "any") and r.action == "deny"
                for r in rules
            )
        except Exception:  # noqa: BLE001
            has = False
        self._resp_deny_cache = has
        self._resp_deny_cache_at = now
        return has

    def _has_bulwark_reply_rules(self) -> bool:
        """Cheap predicate: does any loaded bulwark rule target reply
        (content) or url method=GET-agnostic response? Used to decide
        whether to buffer the response body for evaluation."""
        bw = self._bulwark_current()
        for r in bw.rules:
            if r.action not in ("deny", "approval"):
                continue
            if r.layer in ("reply", "url"):
                return True
        return False

    def _check_canaries_in_reply(self, content: str, provider: str) -> None:
        """Any canary value that surfaces in a reply is logged + recorded."""
        if not content:
            return
        hits = canary_mod.check_content(self.state_root, content)
        for c in hits:
            canary_mod.record_hit(self.state_root, c.id)
            self._log(
                "canary_hit",
                {
                    "canary_id": c.id,
                    "kind": c.kind,
                    "path": c.path,
                    "hit_count": c.hit_count + 1,
                },
                model=provider,
            )

    def _run_reply_classifiers(self, content: str, provider: str) -> None:
        """Run registered classifiers against a response body and audit
        any non-allow verdicts. Doesn't block — classifiers are a flag-
        only signal by default; operators who want enforcement write a
        Bulwark rule matching the classifier name in a follow-up turn.
        """
        if not content:
            return
        try:
            verdicts = classifier_mod.registry.classify_reply(content)
        except Exception as exc:  # noqa: BLE001
            self._log("classifier_error",
                      {"exc": repr(exc)}, model=provider)
            return
        for v in verdicts:
            self._log(
                "classifier_hit",
                {
                    "classifier": v.classifier,
                    "action": v.action,
                    "confidence": v.confidence,
                    "reason": v.reason,
                    "scope": "reply",
                },
                model=provider,
            )

    def _try_enforce_bulwark_response(
        self, host: str, port: int, resp_parsed: dict, resp_body: bytes,
        provider: str, tls_client: socket.socket,
    ) -> bool:
        """Response-scope Bulwark consult. Same shape as the request
        helper: evaluates url + reply layers, short-circuits on deny."""
        try:
            resp_text = resp_body.decode("utf-8", errors="replace") if resp_body else ""
            try:
                resp_json = json.loads(resp_text) if resp_text else None
            except Exception:  # noqa: BLE001
                resp_json = None
            decision = self._bulwark_evaluate_http(
                scope="response",
                host=host, method="", url_path="",
                body_json=resp_json, body_text=resp_text,
            )
            if decision is None or decision.rule is None:
                return False
            if decision.action not in ("deny", "approval"):
                return False
        except Exception as exc:  # noqa: BLE001
            self._log(
                "bulwark_error",
                {"scope": "response", "exc": repr(exc)},
                model=provider,
            )
            return False

        # Cognitive-inhibition: in a disinhibited phase (e.g. probe) relax the
        # reply-layer hard DENY so the agent can analyze auth/secrets during
        # investigation. The reply REWRITE/redact rules still mask secret
        # *values* downstream, and writeup re-tightens. Gated on
        # ARA_CHUNK_INHIBITION_ENFORCE and fail-strict (any error keeps deny).
        if decision.action == "deny" and self._reply_deny_relaxed_by_inhibition():
            self._log(
                "bulwark_reply_relaxed",
                {"rule_id": decision.rule.rule_id, "layer": decision.rule.layer},
                model=provider,
            )
            return False

        info = sse_synth.DenyInfo(
            rule_id=decision.rule.rule_id,
            text=f"ARA policy (bulwark rule {decision.rule.rule_id}) blocked "
                 f"this response.",
        )
        try:
            tls_client.sendall(sse_synth.synthesize_deny(info, provider))
        except Exception as exc:  # noqa: BLE001
            self._log(
                "bulwark_enforce_error",
                {"rule_id": decision.rule.rule_id, "exc": repr(exc)},
                model=provider,
            )
            return True
        self._log(
            "bulwark_deny",
            {
                "rule_id": decision.rule.rule_id,
                "scope": "response",
                "layer": decision.rule.layer,
                "pattern": decision.rule.match.primary,
                "highlight": decision.highlight,
            },
            model=provider,
        )
        return True

    def _reply_deny_relaxed_by_inhibition(self) -> bool:
        """True when the active weapon chunk disinhibits the reply layer
        (e.g. probe), so reply-scope DENY rules should be skipped for this
        turn. Gated on ``ARA_CHUNK_INHIBITION_ENFORCE`` and fail-strict:
        returns False (keep the deny) on any error or when the flag is unset.
        The reply REWRITE/redact rules run regardless, so secret *values* stay
        masked even when the hard deny is relaxed."""
        import os as _os
        if not _os.environ.get("ARA_CHUNK_INHIBITION_ENFORCE"):
            return False
        try:
            from .runtime.chunks.inhibition import active_inhibition_profile
            from . import weapons as _weapons_mod
            weapon_id = (_os.environ.get("ARA_TASK_WEAPON_ID") or "").strip()
            if not weapon_id:
                weapon_id = _weapons_mod.equipped_weapon_id(self.state_root) or ""
            prof = active_inhibition_profile(
                self.state_root, self.session_dir.name, weapon_id,
            )
            inh = float((prof.get("reply") or {}).get("inhibition_level", 1.0))
            return inh < 0.5
        except Exception:  # noqa: BLE001
            return False

    def _read_full_body(
        self, sock: socket.socket, resp_parsed: dict, *, cap: int = 2 * 1024 * 1024,
    ) -> bytes:
        """Pull the entire response body off the upstream socket up to
        ``cap`` bytes, honoring Content-Length / chunked / read-to-EOF
        the same way ``_read_body`` does for requests. Used by the
        response-scope deny path so we can evaluate before forwarding."""
        return _read_body(sock, resp_parsed.get("headers") or {}, cap=cap)

    def _try_enforce_response_deny(
        self, host: str, port: int, resp_parsed: dict, resp_body: bytes,
        provider: str, tls_client: socket.socket,
    ) -> bool:
        """Evaluate response-scope deny rules. Returns True iff a deny
        matched (and a synthetic SSE was written to the client). Caller
        must NOT relay the original response when True."""
        try:
            resp_text = resp_body.decode("utf-8", errors="replace") if resp_body else ""
            payload = {
                "url": f"https://{host}:{port}",
                "status": self._status(resp_parsed["start"]),
                "body_text": resp_text[:2_000_000],
            }
            rule = rhema_mod.evaluate(self.state_root, "response", payload)
            if rule is None or rule.action != "deny":
                return False
        except Exception as exc:  # noqa: BLE001
            self._log("rhema_error",
                      {"scope": "response_deny_eval", "exc": repr(exc)},
                      model=provider)
            return False

        try:
            wire_bytes = rhema_mod.synthesize_deny_sse(rule, provider)
            tls_client.sendall(wire_bytes)
        except Exception as exc:  # noqa: BLE001
            self._log("rhema_enforce_error",
                      {"rule_id": rule.id, "scope": "response", "exc": repr(exc)},
                      model=provider)
            return True  # we attempted; caller still skips upstream relay

        # Note: response-scope deny — channel detection from the
        # response body alone is unreliable, so we leave channel
        # untagged here. Phase-3 filters fall back to "any" for these.
        self._log(
            "rhema_deny",
            {
                "rule_id": rule.id, "pattern": rule.pattern,
                "scope": "response", "redirect": rule.redirect,
                "reason": rule.reason or "RHEMA_RULE_FIRED",
                "enforced": True,
                "path": "mitm_response",
                "note": "response-scope deny; synthetic reply sent to client, original response withheld",
            },
            model=provider,
        )
        return True

    def _try_enforce_request_approval(
        self, host: str, port: int, req_parsed: dict, req_body: bytes,
        provider: str, tls_client: socket.socket,
    ) -> str:
        """Evaluate request-scope Rhema rules. If a rule with
        ``action="approval"`` matches, write an ApprovalRecord to the
        session's pending dir and block synchronously until the operator
        answers (or the timeout fires). Returns one of:

          - ``"none"``   — no approval rule matched, caller continues
          - ``"allow"``  — operator approved, caller continues with the
                           original request body
          - ``"deny"``   — operator denied or timed out; a synthetic deny
                           SSE has been written to ``tls_client`` and the
                           caller must NOT contact upstream

        Fail-open: if evaluation or writing the record errors we return
        ``"none"`` and the request continues — an approval bug should
        not become an outage.
        """
        channel = self._channel_for(req_body)
        try:
            req_text = req_body.decode("utf-8", errors="replace") if req_body else ""
            try:
                req_json = json.loads(req_text) if req_text else None
            except Exception:  # noqa: BLE001
                req_json = None
            rule = rhema_mod.evaluate(
                self.state_root, "request",
                {"body": req_json, "headers": req_parsed.get("headers") or {}},
            )
            if rule is None or rule.action != "approval":
                return "none"
        except Exception as exc:  # noqa: BLE001
            self._log("rhema_error",
                      {"scope": "request_approval", "exc": repr(exc)},
                      model=provider, channel=channel)
            return "none"

        from . import approvals as _approvals
        import time as _time
        approval_id = _approvals.new_approval_id()
        record = _approvals.ApprovalRecord(
            id=approval_id,
            session_id=self.session_dir.name,
            created_at=_time.time(),
            op="rhema.request",
            path=self._path(req_parsed["start"]),
            rule_id=rule.id,
            rule_pattern=rule.pattern,
            reason=rule.reason,
        )
        try:
            _approvals.write_pending(self.session_dir, record)
        except OSError as exc:
            self._log("rhema_error",
                      {"scope": "request_approval_write", "exc": repr(exc)},
                      model=provider, channel=channel)
            return "none"

        self._log(
            "rhema_approval_pending",
            {
                "approval_id": approval_id, "rule_id": rule.id,
                "pattern": rule.pattern, "scope": rule.scope,
                "reason": rule.reason or "RHEMA_RULE_FIRED",
                "note": "turn blocked; awaiting operator response at /approvals",
            },
            model=provider, channel=channel,
        )

        # Sync poll loop — MITM is threaded, so we can block here. The
        # supervisor-owned /approvals flow writes resolved/<id>.json.
        # Per-rule override > env override > built-in default. Lets
        # operators set tight timeouts on high-risk approval rules and
        # generous ones on low-risk human-in-the-loop checks.
        # Timeout auto-grant applies only when (a) the operator hasn't set an
        # explicit per-rule timeout (that's a deliberate human-in-the-loop
        # choice we honor), (b) the feature is enabled, and (c) the request
        # path is routine — never a danger-classified endpoint.
        ag_eligible = (
            (rule.timeout_s is None or rule.timeout_s <= 0)
            and _approvals.autogrant_enabled(self.state_root)
            and danger_mod.is_auto_grant_eligible(
                "rhema.request", {"path": record.path})
        )
        timeout_s: float
        if rule.timeout_s is not None and rule.timeout_s > 0:
            timeout_s = float(rule.timeout_s)
        elif ag_eligible:
            timeout_s = _approvals.autogrant_timeout_s(self.state_root)
        else:
            try:
                timeout_s = float(
                    _os_environ_get("ARA_APPROVAL_TIMEOUT_S", "")
                    or _approvals.APPROVAL_TIMEOUT_DEFAULT
                )
            except (TypeError, ValueError):
                timeout_s = _approvals.APPROVAL_TIMEOUT_DEFAULT
        resolved_path = (
            _approvals.resolved_dir(self.session_dir) / f"{approval_id}.json"
        )
        deadline = _time.time() + timeout_s
        while _time.time() < deadline:
            if resolved_path.exists():
                break
            _time.sleep(0.2)

        resolved = None
        if resolved_path.exists():
            try:
                resolved = _approvals.ApprovalRecord(
                    **json.loads(resolved_path.read_text()),
                )
            except Exception:  # noqa: BLE001
                resolved = None
        if resolved is None:
            # Timeout — auto-grant routine ops if enabled, else deny; persist
            # so /approvals reflects it either way.
            try:
                resolved = _approvals.resolve(
                    self.session_dir, approval_id,
                    action=("allow-timeout" if ag_eligible else "timeout"),
                    reason=("approval_auto_granted_timeout"
                            if ag_eligible else "approval_timeout"),
                )
            except Exception:  # noqa: BLE001
                pass

        allowed = resolved is not None and resolved.status == "allowed"
        self._log(
            "rhema_approval_resolved",
            {
                "approval_id": approval_id, "rule_id": rule.id,
                "status": resolved.status if resolved else "timeout",
                "action": resolved.action if resolved else "timeout",
                "elapsed_s": round(_time.time() - record.created_at, 2),
            },
            model=provider, channel=channel,
        )
        if allowed:
            return "allow"

        # Denied / timed out: synthesize the same SSE deny payload the
        # explicit deny path uses.
        try:
            wire_bytes = rhema_mod.synthesize_deny_sse(rule, provider)
            tls_client.sendall(wire_bytes)
        except Exception as exc:  # noqa: BLE001
            self._log("rhema_enforce_error",
                      {"rule_id": rule.id, "exc": repr(exc)},
                      model=provider, channel=channel)
        return "deny"

    def _try_apply_request_rewrite(
        self, host: str, port: int, req_parsed: dict, req_body: bytes,
        provider: str,
    ) -> Optional[bytes]:
        """Evaluate request-scope Rhema + the ABSOLUTE live projection.
        If either resolves to an ``action="rewrite"`` rule, apply the
        rewrite spec to the JSON body and return the mutated bytes. The
        caller forwards those bytes to upstream — the agent sees the
        response to the mutated request. Returns ``None`` if nothing
        applies or if anything fails (fail-open, same posture as shadow
        dispatch)."""
        channel = self._channel_for(req_body)
        try:
            req_text = req_body.decode("utf-8", errors="replace") if req_body else ""
            try:
                req_json = json.loads(req_text) if req_text else None
            except Exception:  # noqa: BLE001
                req_json = None
            if req_json is None:
                return None

            rule = None
            try:
                op = rhema_mod.evaluate(
                    self.state_root, "request",
                    {"body": req_json, "headers": req_parsed.get("headers") or {}},
                )
                if (
                    op is not None
                    and op.action == "rewrite"
                    and isinstance(op.rewrite_with, dict)
                ):
                    rule = op
            except Exception as exc:  # noqa: BLE001
                self._log("rhema_error",
                          {"scope": "request_rewrite_op", "exc": repr(exc)},
                          model=provider, channel=channel)

            path = self._path(req_parsed["start"])
            is_envelope_shape = (
                "instructions" in req_json
                or isinstance(req_json.get("messages"), list)
            )
            is_envelope_endpoint = (
                "codex/responses" in path
                or "chat/completions" in path
                or path.endswith("/api/chat")
            )
            if rule is None and is_envelope_shape and is_envelope_endpoint:
                try:
                    abs_rule = rhema_mod.active_absolute_rule(
                        self.state_root, channel_id=channel,
                    )
                    if (
                        abs_rule is not None
                        and abs_rule.action == "rewrite"
                        and isinstance(abs_rule.rewrite_with, dict)
                    ):
                        rule = abs_rule
                except Exception as exc:  # noqa: BLE001
                    self._log("rhema_error",
                              {"scope": "request_rewrite_abs", "exc": repr(exc)},
                              model=provider, channel=channel)

            mutated = req_json
            rewrite_source = None
            if rule is not None:
                mutated = rhema_mod.apply_rewrite_spec(req_json, rule.rewrite_with)
                rewrite_source = (
                    "absolute_live" if rule.managed_key == "abs-live"
                    else "rhema_rule"
                )

            filtered = self._maybe_filter_task_channel_debt_tools(mutated)
            if filtered is not None:
                mutated = filtered

            if rule is None and filtered is None:
                return None

            new_bytes = json.dumps(mutated).encode("utf-8")
            rewrite_spec = rule.rewrite_with if rule is not None else None
            original_messages = (
                req_json.get("messages") if isinstance(req_json, dict) else None
            )
            mutated_messages = (
                mutated.get("messages") if isinstance(mutated, dict) else None
            )
            original_system_chars = _system_prompt_chars(req_json)
            mutated_system_chars = _system_prompt_chars(mutated)
            request_shape = {
                "message_count": (
                    len(original_messages) if isinstance(original_messages, list)
                    else None
                ),
                "mutated_message_count": (
                    len(mutated_messages) if isinstance(mutated_messages, list)
                    else None
                ),
                "tool_count": _tool_count(req_json),
                "mutated_tool_count": _tool_count(mutated),
                "system_chars": original_system_chars,
                "mutated_system_chars": mutated_system_chars,
            }
            absolute_meta = {}
            if rewrite_source == "absolute_live" and isinstance(rewrite_spec, dict):
                absolute_meta = {
                    "absolute_armor": rewrite_spec.get("armor"),
                    "absolute_name": rewrite_spec.get("absolute_name"),
                    "absolute_path": rewrite_spec.get("source_path"),
                    "absolute_chars": rewrite_spec.get("text_chars"),
                    "absolute_bytes": rewrite_spec.get("text_bytes"),
                    "absolute_enforcing": rewrite_spec.get("enforcing"),
                }
            self._log(
                "rhema_rewrite" if rule is not None else "task_channel_debt_tool_filter",
                {
                    "rule_id": rule.id if rule is not None else None,
                    "spec": rewrite_spec,
                    "source": rewrite_source or "task_channel_debt",
                    "original_bytes": len(req_body),
                    "mutated_bytes": len(new_bytes),
                    "enforced": True,
                    "task_channel_debt_tools_filtered": filtered is not None,
                    "note": "request body mutated before upstream; agent sees response to mutated request",
                    **request_shape,
                    **absolute_meta,
                },
                model=provider, channel=channel,
            )
            debug_log.emit(
                "mitm",
                "request.rewrite" if rule is not None else "task_debt.tools_filtered",
                state_root=self.state_root,
                session_id=self.session_dir.name,
                provider=provider,
                channel=channel,
                rule_id=rule.id if rule is not None else None,
                source=rewrite_source or "task_channel_debt",
                original_bytes=len(req_body),
                mutated_bytes=len(new_bytes),
                task_channel_debt_tools_filtered=filtered is not None,
                spec=rewrite_spec,
                **request_shape,
                **absolute_meta,
            )
            return new_bytes
        except Exception as exc:  # noqa: BLE001
            self._log("rhema_rewrite_error", {"exc": repr(exc)},
                      model=provider, channel=channel)
            return None

    def _task_channel_debt_active(self) -> bool:
        if os.environ.get("ARA_TASK_SUPERVISOR") != "armory-hermes-loop":
            return False
        task_id = str(os.environ.get("ARA_TASK_RUN_ID") or "").strip()
        if task_id:
            try:
                from . import task_session as _task_session_mod
                run = _task_session_mod.TaskRunSession.load(
                    self.state_root, task_id,
                )
                return run.phase == _task_session_mod.TaskPhase.COMMIT_REQUIRED
            except Exception:
                pass
        try:
            budget = int(os.environ.get("ARA_TASK_CHANNEL_EXEC_BUDGET", "12"))
        except ValueError:
            budget = 12
        if budget <= 0:
            return False
        events_path = self.session_dir / "events.jsonl"
        try:
            lines = events_path.read_text(errors="replace").splitlines()
        except OSError:
            return False
        spawns = 0
        commits = 0
        for line in lines:
            if '"kind": "spawn"' in line:
                spawns += 1
            if '"status": "rejected"' in line:
                continue
            if (
                '"channel": "fact.assert"' in line
                or '"channel": "lead.file"' in line
                or '"channel": "finding.file"' in line
                or '"kind": "channel.fact.assert"' in line
                or '"kind": "channel.lead.file"' in line
                or '"kind": "channel.finding.file"' in line
            ):
                commits += 1
        threshold = max(1, (budget * (commits + 1)) - 1)
        return spawns >= threshold

    def _maybe_filter_task_channel_debt_tools(self, payload: dict) -> dict | None:
        allowed_names = None
        try:
            from .task_action_gate import task_debt_mcp_tool_names
            allowed_names = task_debt_mcp_tool_names(self.state_root)
        except Exception:
            allowed_names = None
        if allowed_names is None:
            if not self._task_channel_debt_active():
                return None
            allowed_names = tuple(self._TASK_COMMIT_MCP_TOOL_ORDER)
        if not allowed_names:
            return None
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return None
        allowed_set = set(allowed_names)
        filtered = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            name = fn.get("name") if isinstance(fn, dict) else None
            if name in allowed_set:
                filtered.append(tool)
        if not filtered:
            return None
        name_to_tool = {}
        for tool in filtered:
            fn = tool.get("function") if isinstance(tool, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if name:
                name_to_tool[name] = tool
        ordered = [
            name_to_tool[name]
            for name in allowed_names
            if name in name_to_tool
        ]
        ordered.extend([
            tool for tool in filtered
            if tool not in ordered
        ])
        out = dict(payload)
        out["tools"] = ordered
        out["tool_choice"] = _tool_choice_for_filtered_tools(ordered)
        debug_log.emit(
            "mitm", "task_debt.tools_filtered",
            state_root=getattr(self, "state_root", None),
            session_id=getattr(getattr(self, "session_dir", None), "name", None),
            original_count=len(tools),
            filtered_count=len(ordered),
            kept=[
                (tool.get("function") or {}).get("name")
                for tool in ordered
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            ],
        )
        return out

    # ── v0.2 pause-and-ask helpers ─────────────────────────────────
    def _is_llm_completion_path(self, path: str) -> bool:
        """Return True for endpoints whose body is a chat completion /
        message request (anthropic, openai/litellm, xai, codex, gemini).
        Tool-list endpoints, files, embeddings etc. are excluded so we
        only inject into actual model turns.
        """
        if not path:
            return False
        return any(
            seg in path for seg in (
                "/v1/messages",
                "/v1/chat/completions",
                "/v1/responses",
                ":generateContent",
                ":streamGenerateContent",
            )
        )

    def _try_apply_pause_and_ask_inject(
        self, host: str, req_parsed: dict, req_body: bytes, provider: str,
    ) -> bytes | None:
        """If a pause-and-ask classification is queued for this state
        root and the request is a chat-completion / message turn,
        prepend a system prompt asking the agent to classify the
        unknown tool. Force ``stream=false`` for this turn so the
        response is a single JSON we can parse server-side.
        """
        if not _should_mitm(host):
            return None
        # ``req_parsed`` carries the raw start line (``GET /v1/... HTTP/1.1``)
        # in ``start`` — there's no precomputed ``path`` key. Use the
        # proxy's own helper for consistency with the rest of the file.
        try:
            path = self._path(req_parsed.get("start") or "")
        except Exception:  # noqa: BLE001
            path = ""
        if not self._is_llm_completion_path(path):
            return None
        if not req_body:
            return None
        try:
            req_json = json.loads(
                req_body.decode("utf-8", errors="replace")
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(req_json, dict):
            return None
        new_json = paas.maybe_inject_into_provider_request(
            self.state_root, provider, req_json,
        )
        if new_json is None:
            return None
        # Force non-streaming for this turn so resp_body is a single
        # JSON object we can parse without SSE assembly.
        new_json["stream"] = False
        try:
            new_bytes = json.dumps(new_json).encode("utf-8")
        except Exception:  # noqa: BLE001
            return None
        try:
            self._log(
                "pause_and_ask_inject",
                {
                    "path": path,
                    "provider": provider,
                    "original_bytes": len(req_body),
                    "mutated_bytes": len(new_bytes),
                },
                model=provider,
            )
        except Exception:  # noqa: BLE001
            pass
        return new_bytes

    def _try_capture_tool_exchanges(
        self, host: str, req_parsed: dict, req_body: bytes,
    ) -> None:
        """Bind the agent's tool call+output exchanges from this LLM request into
        the evidence binder as ``tool:<name>`` records (the actual command output,
        e.g. the response body — not just the command). Best-effort and read-only:
        any failure is swallowed and never affects the request."""
        if not _should_mitm(host):
            return
        try:
            path = self._path(req_parsed.get("start") or "")
        except Exception:  # noqa: BLE001
            path = ""
        if not self._is_llm_completion_path(path) or not req_body:
            return
        try:
            req_json = json.loads(req_body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return
        try:
            from .runtime.binder.tool_capture import capture_tool_exchanges
            from .runtime.activation import ActivationManager
            try:
                act = ActivationManager(self.session_dir).peek()
            except Exception:  # noqa: BLE001
                act = None
            bound = capture_tool_exchanges(
                self.session_dir, req_json, activation_id=act,
            )
            if bound:
                debug_log.emit(
                    "evidence-binder", "tool_capture",
                    state_root=self.state_root,
                    session_id=self.session_dir.name,
                    bound=bound,
                    activation_id=act,
                )
        except Exception:  # noqa: BLE001
            pass

    def _try_apply_exec_deny_notice(
        self, host: str, req_parsed: dict, req_body: bytes, provider: str,
    ) -> bytes | None:
        """Drain queued exec-deny notices and, if any, prepend a system
        message naming the blocked term(s) and telling the agent to skip
        and continue. Returns mutated request bytes, or ``None`` when
        there's nothing to inject or the request isn't a model turn.
        Best-effort: any failure returns ``None`` (no mutation). The
        request body is parsed *before* draining, so a malformed body
        never consumes notices.
        """
        if not _should_mitm(host):
            return None
        try:
            path = self._path(req_parsed.get("start") or "")
        except Exception:  # noqa: BLE001
            path = ""
        if not self._is_llm_completion_path(path):
            return None
        if not req_body:
            return None
        try:
            req_json = json.loads(req_body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(req_json, dict):
            return None
        # Parse succeeded — safe to drain (one-shot, destructive).
        try:
            notices = _exec_deny_notices.drain(self.state_root)
        except Exception:  # noqa: BLE001
            return None
        if not notices:
            return None
        try:
            message = _exec_deny_notices.compose_message(notices)
        except Exception:  # noqa: BLE001
            message = ""
        if not message:
            return None
        # Provider-aware: Anthropic top-level ``system`` vs OpenAI/litellm
        # role=system message (ARA's lmstudio provider is OpenAI-shaped).
        new_json = paas._inject_provider_specific(provider, req_json, message)
        if new_json is None:
            return None
        try:
            new_bytes = json.dumps(new_json).encode("utf-8")
        except Exception:  # noqa: BLE001
            return None
        try:
            self._log(
                "exec_deny_notice_inject",
                {
                    "path": path,
                    "provider": provider,
                    "notices": len(notices),
                    "original_bytes": len(req_body),
                    "mutated_bytes": len(new_bytes),
                },
                model=provider,
            )
        except Exception:  # noqa: BLE001
            pass
        return new_bytes

    def _try_consume_pause_and_ask(
        self, provider: str, resp_text: str,
    ) -> None:
        """Parse ``resp_text`` (a buffered LLM response body) for the
        agent's classification reply. On success, the shared module
        records the classification + clears the pending entry.
        """
        if not resp_text:
            return
        # Body is JSON because we forced stream=false on inject. If a
        # rare provider ignored it, fall through silently.
        try:
            payload = json.loads(resp_text)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(payload, dict):
            return
        consumed = paas.maybe_consume_classification_response(
            self.state_root, payload,
        )
        if consumed:
            try:
                self._log(
                    "pause_and_ask_consume",
                    {"provider": provider, "result": "classified"},
                    model=provider,
                )
            except Exception:  # noqa: BLE001
                pass

    def _rebuild_request_raw(self, req_parsed: dict, new_body: bytes) -> bytes:
        """Rebuild the request header block with a correct Content-Length
        for ``new_body``. Drops transfer-encoding/connection so upstream
        sees a clean single request. Preserves method/path/version and
        all other headers."""
        start = req_parsed["start"]
        hdrs_out: list[str] = []
        skipped = {"content-length", "transfer-encoding", "connection"}
        for line in req_parsed["_lines"][1:]:
            if not line:
                continue
            key = line.split(":", 1)[0].strip().lower()
            if key in skipped:
                continue
            hdrs_out.append(line)
        hdrs_out.append(f"Content-Length: {len(new_body)}")
        return (start + "\r\n" + "\r\n".join(hdrs_out) + "\r\n\r\n").encode(
            "iso-8859-1",
        )

    def _blind_tunnel(self, host: str, port: int, client_sock: socket.socket) -> None:
        try:
            upstream = socket.create_connection((host, port), timeout=30)
        except Exception:
            return
        # Drop the connect-time timeout; long-lived WebSockets (Discord's
        # gateway, Slack RTM, etc.) sit idle for 30-40s between heartbeats
        # and would otherwise TimeoutError the pipe and look like a 1006
        # abnormal closure to the client. Kernel TCP keepalive is on by
        # default — that's what we want to detect dead peers, not a
        # socket-level recv timeout.
        try:
            upstream.settimeout(None)
            client_sock.settimeout(None)
        except Exception:
            pass
        client_sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(8192)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=pipe, args=(client_sock, upstream), daemon=True)
        t2 = threading.Thread(target=pipe, args=(upstream, client_sock), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
        try:
            upstream.close()
        except Exception:
            pass

    def _handle_plain_http(self, start: str, parsed: dict, client_sock: socket.socket) -> None:
        parts = start.split(" ")
        if len(parts) < 2:
            return
        url = parts[1]
        p = urlparse(url)
        host = p.hostname or ""
        port = p.port or 80
        path = p.path or "/"
        if p.query:
            path = f"{path}?{p.query}"
        req_body = _read_body(client_sock, parsed["headers"], is_request=True)

        # Mechanical danger-class enforcement on target egress. Irreversible
        # endpoints (factory reset, firmware flash, disk format) are hard-
        # blocked and auto-added to the armor bulwark; provable ones are
        # recorded for the operator but allowed through, so the assessment can
        # still exercise them.
        full_url = f"http://{host}:{port}{path}"
        _dmethod = parts[0] if parts else ""
        _dflag = danger_mod.classify_url(full_url, _dmethod)
        if _dflag.is_danger:
            try:
                from . import armors as _armors_mod
                danger_registry_mod.flag_and_maybe_block(
                    self.state_root, op="net.request", flag=_dflag,
                    target=full_url,
                    armor_id=_armors_mod.active_armor_name(self.state_root),
                    session_id=self.session_dir.name,
                )
            except Exception as _dexc:  # noqa: BLE001
                self._log("danger_error", {"url": full_url, "exc": repr(_dexc)},
                          model=_provider_for(host), channel="")
            if _dflag.tier == danger_mod.IRREVERSIBLE:
                self._log(
                    "danger_deny",
                    {"url": full_url, "tier": _dflag.tier,
                     "pattern": _dflag.pattern, "path": "mitm_plain_http",
                     "enforced": True,
                     "note": "irreversible target op blocked"},
                    model=_provider_for(host), channel="",
                )
                body = (b"ARA policy: irreversible operation blocked "
                        b"(danger-class).\n")
                resp = (
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"\r\n" + body
                )
                try:
                    client_sock.sendall(resp)
                except Exception:  # noqa: BLE001
                    pass
                return

        new_start = f"{parts[0]} {path} {' '.join(parts[2:])}\r\n"
        hdr_out = [
            l for l in parsed["_lines"][1:]
            if l and not l.lower().startswith(("proxy-", "connection:"))
        ]
        req_raw = (new_start + "\r\n".join(hdr_out) + "\r\n\r\n").encode("iso-8859-1")

        try:
            upstream = socket.create_connection((host, port), timeout=30)
        except Exception:
            return
        try:
            upstream.sendall(req_raw)
            if req_body:
                upstream.sendall(req_body)
            resp_raw, resp_parsed = _read_headers(upstream)
            client_sock.sendall(resp_raw)
            resp_body = self._stream_response_body(
                upstream, client_sock, cap=2 * 1024 * 1024,
            )
            self._record(host, port, parsed, req_body, resp_parsed, resp_body)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _record(
        self, host: str, port: int,
        req_parsed: dict, req_body: bytes,
        resp_parsed: dict, resp_body: bytes,
        rewrite_was_applied: bool = False,
    ) -> None:
        provider = _provider_for(host)
        req_text = req_body.decode("utf-8", errors="replace")
        try:
            req_json = json.loads(req_text) if req_text else None
        except Exception:
            req_json = None
        # Phase-3 channel tagging: classify the turn once per _record
        # and stamp every transcript line we emit during this turn so
        # the dashboard can filter by channel without re-scanning the
        # body.
        from . import armors as _armors_mod
        channel = _armors_mod.detect_channel(req_json or {})
        self._log(
            "request",
            {
                "url": f"https://{host}:{port}{self._path(req_parsed['start'])}",
                "method": self._method(req_parsed["start"]),
                "headers": {k: v for k, v in req_parsed["headers"].items()
                            if k not in ("authorization", "cookie")},
                "body": req_json,
                "body_text": None if req_json is not None else req_text[:32_000],
            },
            model=provider, channel=channel,
        )
        resp_text = resp_body.decode("utf-8", errors="replace")
        response_payload = {
            "url": f"https://{host}:{port}",
            "status": self._status(resp_parsed["start"]),
            "body_text": resp_text[:2_000_000] + ("…" if len(resp_text) > 2_000_000 else ""),
        }
        self._log("response", response_payload, model=provider, channel=channel)
        self._capture_evidence(
            host, port, req_parsed, req_body, resp_parsed, resp_body,
        )
        if os.environ.get("ARA_TASK_SUPERVISOR") == "armory-hermes-loop":
            accepted = _dispatch_pseudo_task_channel_calls(
                self.state_root, self.session_dir, response_payload,
            )
            if accepted:
                self._log(
                    "pseudo_tool_channel_commit",
                    {"accepted": accepted},
                    model=provider, channel=channel,
                )

        # In-process Rhema evaluation. The supervisor's _on_prompt_intercept
        # path is the canonical one for Tier-A hooks; the MITM proxy sidesteps
        # that IPC because it captures bytes-on-wire directly. Running the
        # evaluator here bumps fire counts and logs a rhema_deny record so
        # operators see which rules would have fired, even though we don't
        # synthesize a response back to the agent from this code path.
        try:
            rule = rhema_mod.evaluate(self.state_root, "response", response_payload)
            if rule is not None and rule.action == "deny":
                self._log(
                    "rhema_deny",
                    {
                        "rule_id": rule.id, "pattern": rule.pattern,
                        "scope": rule.scope, "redirect": rule.redirect,
                        "reason": rule.reason or "RHEMA_RULE_FIRED",
                        "note": "captured-only; synthesized response needs supervisor IPC",
                    },
                    model=provider, channel=channel,
                )
        except Exception as exc:
            self._log("rhema_error", {"exc": repr(exc)},
                      model=provider, channel=channel)

        # --- Shadow-rewrite A/B dispatch --------------------------------
        # Evaluate the REQUEST against Rhema. If a matched rule's action is
        # ``shadow_rewrite``, fork a background thread that re-issues the
        # request with the mutated body and logs the variant response
        # alongside, tagged with a shared pair_id. The operator can then
        # diff original vs. variant in the Shadows view without the agent
        # ever seeing anything different.
        # --- Shadow-rewrite A/B dispatch --------------------------------
        # Two sources can fire a shadow variant for this request:
        #   1. An operator-written Rhema rule with action=shadow_rewrite.
        #   2. The equipped armor's ABSOLUTE.md (ephemeral projection —
        #      no managed rule is written to disk; the file IS the source
        #      of truth, read per-request so edits apply instantly).
        # If both produce a rule, both variants fire. Each gets its own
        # pair_id so they're distinct on /shadows.
        candidate_rules: list = []
        if rewrite_was_applied:
            # The enforcing rewrite already mutated the live request;
            # re-dispatching the same rule as a shadow would just log a
            # duplicate variant of the same response. Skip entirely.
            return
        try:
            if req_json is not None:
                op_rule = rhema_mod.evaluate(
                    self.state_root, "request",
                    {"body": req_json, "headers": req_parsed["headers"]},
                )
                if (
                    op_rule is not None
                    and op_rule.action == "shadow_rewrite"
                    and isinstance(op_rule.rewrite_with, dict)
                ):
                    candidate_rules.append(op_rule)
        except Exception as exc:
            self._log("shadow_error",
                      {"source": "rhema_eval", "exc": repr(exc)},
                      model=provider)

        try:
            path = self._path(req_parsed["start"])
            if req_json is not None and (
                (
                    "instructions" in req_json
                    or isinstance(req_json.get("messages"), list)
                )
                and (
                    "codex/responses" in path
                    or "chat/completions" in path
                    or path.endswith("/api/chat")
                )
            ):
                from . import armors as _armors_mod
                channel_id = _armors_mod.detect_channel(req_json or {})
                abs_rule = rhema_mod.active_absolute_rule(
                    self.state_root, channel_id=channel_id,
                )
                if abs_rule is not None:
                    candidate_rules.append(abs_rule)
        except Exception as exc:
            self._log("shadow_error",
                      {"source": "absolute_live", "exc": repr(exc)},
                      model=provider)

        for rule in candidate_rules:
            try:
                pair_id = rhema_mod.new_pair_id()
                mutated = rhema_mod.apply_rewrite_spec(
                    req_json, rule.rewrite_with,
                )
                # Capture pre/post field values so the /shadows diff view
                # can render a side-by-side comparison without replaying
                # the transcript. Only useful for single-field specs
                # (replace, regex_sub, trim_to_chars); pipeline/remove
                # are recorded as the whole field's old/new state.
                field = None
                if isinstance(rule.rewrite_with, dict):
                    field = rule.rewrite_with.get("field")
                pre_val = None
                post_val = None
                if field and isinstance(req_json, dict):
                    pre_val = req_json.get(field)
                if field and isinstance(mutated, dict):
                    post_val = mutated.get(field)
                self._log(
                    "shadow_link",
                    {
                        "pair_id": pair_id,
                        "rule_id": rule.id,
                        "spec": rule.rewrite_with,
                        "source": (
                            "absolute_live" if rule.managed_key == "abs-live"
                            else "rhema_rule"
                        ),
                        "original_request_url": (
                            f"https://{host}:{port}"
                            f"{self._path(req_parsed['start'])}"
                        ),
                        "original_status": self._status(resp_parsed["start"]),
                        "original_req_bytes": len(req_body),
                        "original_resp_bytes": len(resp_body),
                        "field": field,
                        "original_field_value": pre_val,
                        "mutated_field_value": post_val,
                        "original_response_body_text": resp_text[:200_000],
                    },
                    model=provider, channel=channel,
                )
                threading.Thread(
                    target=self._dispatch_shadow,
                    args=(host, port, req_parsed, mutated, rule, pair_id, provider, channel),
                    daemon=True,
                ).start()
            except Exception as exc:
                self._log("shadow_error",
                          {"source": "dispatch", "exc": repr(exc)},
                          model=provider, channel=channel)

    def _capture_evidence(
        self,
        host: str,
        port: int,
        req_parsed: dict,
        req_body: bytes,
        resp_parsed: dict,
        resp_body: bytes,
    ) -> None:
        try:
            from .runtime.binder.api import EvidenceBinder
            url = f"https://{host}:{port}{self._path(req_parsed['start'])}"
            method = self._method(req_parsed["start"])
            status = self._status(resp_parsed["start"])
            content = {
                "request": {
                    "url": url,
                    "method": method,
                    "headers": {
                        k: v for k, v in req_parsed.get("headers", {}).items()
                        if k.lower() not in ("authorization", "cookie")
                    },
                    "body_text": req_body[:64_000].decode("utf-8", errors="replace"),
                    "body_truncated": len(req_body) > 64_000,
                },
                "response": {
                    "status": status,
                    "headers": dict(resp_parsed.get("headers") or {}),
                    "body_text": resp_body[:256_000].decode("utf-8", errors="replace"),
                    "body_truncated": len(resp_body) > 256_000,
                },
            }
            EvidenceBinder(self.session_dir).put(
                json.dumps(content, sort_keys=True, indent=2).encode("utf-8"),
                source="mitm:request",
                source_metadata={
                    "url": url,
                    "method": method,
                    "status": status,
                    "direction": "roundtrip",
                },
                summary=f"{method} {url} -> {status}",
                content_type="application/json",
            )
        except Exception:
            pass

    def _dispatch_shadow(
        self,
        host: str, port: int,
        req_parsed: dict,
        mutated_body: dict,
        rule: "rhema_mod.RhemaRule",
        pair_id: str,
        provider: str,
        channel: Optional[str] = None,
    ) -> None:
        """Fire a rewritten version of a captured request upstream and log
        its response. Runs on its own thread; never touches the client
        socket. Any failure is swallowed + logged — shadow dispatch must
        not jeopardize the live path."""
        try:
            body_bytes = json.dumps(mutated_body).encode("utf-8")
            # Rebuild request start line + headers. Use the original
            # request's method and path; force Content-Length and
            # Content-Type to match the mutated body.
            original_method = self._method(req_parsed["start"])
            original_path = self._path(req_parsed["start"])
            hdrs_out: list[str] = []
            skipped = {"content-length", "transfer-encoding", "connection"}
            for line in req_parsed["_lines"][1:]:
                if not line:
                    continue
                key = line.split(":", 1)[0].strip().lower()
                if key in skipped:
                    continue
                hdrs_out.append(line)
            # Ensure content-type is present.
            if not any(l.lower().startswith("content-type:") for l in hdrs_out):
                hdrs_out.append("Content-Type: application/json")
            hdrs_out.append(f"Content-Length: {len(body_bytes)}")
            req_raw = (
                f"{original_method} {original_path} HTTP/1.1\r\n"
                + "\r\n".join(hdrs_out) + "\r\n\r\n"
            ).encode("iso-8859-1")

            sock = socket.create_connection((host, port), timeout=30)
            try:
                sock.settimeout(None)
            except Exception:
                pass
            ctx = ssl.create_default_context()
            tls = ctx.wrap_socket(sock, server_hostname=host)
            try:
                tls.sendall(req_raw)
                tls.sendall(body_bytes)
                resp_raw, resp_parsed = _read_headers(tls)
                resp_body = _read_body(tls, resp_parsed["headers"])
            finally:
                try:
                    tls.close()
                except Exception:
                    pass

            resp_text = resp_body.decode("utf-8", errors="replace")
            self._log(
                "shadow_variant_request",
                {
                    "pair_id": pair_id,
                    "rule_id": rule.id,
                    "url": f"https://{host}:{port}{original_path}",
                    "method": original_method,
                    "body": mutated_body,
                    "spec": rule.rewrite_with,
                },
                model=provider, channel=channel,
            )
            self._log(
                "shadow_variant_response",
                {
                    "pair_id": pair_id,
                    "rule_id": rule.id,
                    "url": f"https://{host}:{port}",
                    "status": self._status(resp_parsed["start"]),
                    "body_text": (
                        resp_text[:2_000_000]
                        + ("…" if len(resp_text) > 2_000_000 else "")
                    ),
                },
                model=provider, channel=channel,
            )
        except Exception as exc:
            self._log(
                "shadow_error",
                {"pair_id": pair_id, "rule_id": rule.id, "exc": repr(exc)},
                model=provider, channel=channel,
            )

    @staticmethod
    def _method(start: str) -> str:
        return start.split(" ", 1)[0] if start else ""

    @staticmethod
    def _path(start: str) -> str:
        parts = start.split(" ")
        target = parts[1] if len(parts) > 1 else "/"
        # Absolute-form request target (proxy-style "GET http://h/p HTTP/1.1"):
        # return only path+query so callers that prepend scheme://host:port
        # don't emit "https://host:80http://host/path".
        if target.startswith("http://") or target.startswith("https://"):
            from urllib.parse import urlsplit
            sp = urlsplit(target)
            target = (sp.path or "/") + (f"?{sp.query}" if sp.query else "")
        return target

    @staticmethod
    def _status(start: str) -> int:
        parts = start.split(" ", 2)
        try:
            return int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            return 0


class _ProxyHandler(socketserver.BaseRequestHandler):
    proxy: MitmProxy

    def handle(self) -> None:  # noqa: D401
        self.proxy.handle(self.request)


class _ThreadedProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_proxy_threaded(state_root: Path, session_dir: Path) -> tuple[_ThreadedProxy, int]:
    proxy = MitmProxy(state_root, session_dir)
    handler = type("Handler", (_ProxyHandler,), {"proxy": proxy})
    server = _ThreadedProxy(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, port


# Backwards-compat shim for the async caller in launch.py.
async def start_proxy(state_root: Path, session_dir: Path):
    server, port = start_proxy_threaded(state_root, session_dir)

    # launch.py expects `server.close()` + `await server.wait_closed()`
    # to be available. Wrap with tiny adapters.
    class _Adapter:
        def __init__(self, s): self._s = s
        def close(self): self._s.shutdown(); self._s.server_close()
        async def wait_closed(self): return

    return _Adapter(server), port


def ca_cert_path(state_root: Path) -> Path:
    return state_root / "mitm" / "ca.crt"
