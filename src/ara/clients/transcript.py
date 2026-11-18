"""ARA Tier-A transcript client.

Drop into any ARA-aware agent (OpenClaw is the reference consumer) to:
  - record what's about to be sent to the LLM
  - get back a decision: allow / deny / rewrite / inject

Reads ``ARA_SOCKET`` + ``ARA_SESSION_TOKEN`` from the environment that
ARA's launcher sets. ``ARA_TRANSCRIPT_PROTOCOL`` advertises the channel
version (currently ``v1``); call sites should no-op if missing.

Usage:

    from ara.clients.transcript import intercept

    # Before sending an LLM request:
    decision = intercept(
        role="request",
        payload={"system": ..., "messages": [...], "tools": [...]},
        model="gpt-5-codex",
    )
    if decision["action"] == "deny":
        # Surface a synthesized assistant turn back into the agent loop.
        return decision["injected_response"]
    if decision["action"] == "rewrite":
        payload = decision["new_payload"]

    # After receiving the model's response:
    intercept(role="response", payload={"content": [...], "usage": {...}})

The function is sync + blocking on purpose — agents serialize LLM calls
anyway and the supervisor decision is a single short round-trip.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Optional


_SEQ_LOCK = threading.Lock()
_SEQ = 1000
_SOCK_LOCK = threading.Lock()
_SOCK: Optional[socket.socket] = None


def _run_task_active() -> bool:
    return bool(os.environ.get("ARA_TASK_RUN_ID", "").strip())


def _next_seq() -> int:
    global _SEQ
    with _SEQ_LOCK:
        _SEQ += 1
        return _SEQ


def _connect() -> Optional[socket.socket]:
    """Open (and cache) a unix-socket connection to the supervisor.
    Returns None if the channel isn't configured — caller should fall
    through to a no-op allow."""
    global _SOCK
    if _SOCK is not None:
        return _SOCK
    sock_path = os.environ.get("ARA_SOCKET")
    token = os.environ.get("ARA_SESSION_TOKEN")
    if not sock_path or not token:
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Short timeout — this socket is wrapped around every LLM call's
    # ``Completions.create``, so any stall here stacks: openai-python
    # retries the underlying create on APIConnectionError (with
    # exponential backoff), and each retry pays the timeout again.
    # 10s × 3 retries × 2 internal cycles = 60s of latent stall surfaced
    # as a connection error before any HTTP is even attempted. The
    # transcript channel is best-effort (libc shim + MITM still record
    # at network/syscall layer), so a 1s ceiling fails open quickly.
    # Override with ARA_INTERCEPT_TIMEOUT=N (seconds, float) when the
    # supervisor is known to be slow on a specific host.
    try:
        _to = float(os.environ.get("ARA_INTERCEPT_TIMEOUT", "1.0"))
    except ValueError:
        _to = 1.0
    s.settimeout(_to)
    try:
        s.connect(sock_path)
    except OSError:
        s.close()
        return None
    # Hello handshake — same shape every other ARA client uses.
    hello = {
        "type": "hello",
        "token": token,
        "session": os.environ.get("ARA_SESSION_ID", ""),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "runtime": "transcript-client",
        "seq": _next_seq(),
    }
    s.sendall((json.dumps(hello) + "\n").encode("utf-8"))
    # Drain the hello_ack so it doesn't pollute later replies.
    _readline(s)
    _SOCK = s
    return s


def _readline(s: socket.socket) -> str:
    buf = bytearray()
    while True:
        chunk = s.recv(4096)
        if not chunk:
            return buf.decode("utf-8", errors="replace")
        buf.extend(chunk)
        if b"\n" in chunk:
            break
    line, _, _ = buf.partition(b"\n")
    return line.decode("utf-8", errors="replace")


def intercept(
    role: str,
    payload: dict,
    *,
    model: Optional[str] = None,
) -> dict:
    """Send a transcript turn to the supervisor and return its decision.

    On any IPC failure, returns ``{"action": "allow"}`` so a broken
    supervisor connection never wedges the agent — fail-open at the
    application layer is appropriate for a transcript channel because
    the libc shim + Node guard still enforce policy at the syscall layer.
    """
    if os.environ.get("ARA_TRANSCRIPT_PROTOCOL") != "v1":
        return {"action": "allow"}
    s = _connect()
    if s is None:
        if _run_task_active():
            return {
                "action": "deny",
                "reason": "ARA_INTERCEPT_UNAVAILABLE",
                "injected_response": {
                    "role": "assistant",
                    "content": "ARA denied this turn because the Run Task supervisor channel is unavailable.",
                },
            }
        return {"action": "allow"}
    seq = _next_seq()
    msg = {
        "type": "prompt_intercept",
        "seq": seq,
        "role": role,
        "payload": payload,
        "pid": os.getpid(),
        "model": model,
    }
    try:
        with _SOCK_LOCK:
            s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            line = _readline(s)
        rec = json.loads(line)
        if rec.get("seq") == seq and isinstance(rec.get("action"), str):
            return rec
    except (OSError, json.JSONDecodeError):
        # Drop the dead socket so the next call retries the connect.
        global _SOCK
        try:
            if _SOCK is not None:
                _SOCK.close()
        except OSError:
            pass
        _SOCK = None
        if _run_task_active():
            return {
                "action": "deny",
                "reason": "ARA_INTERCEPT_UNAVAILABLE",
                "injected_response": {
                    "role": "assistant",
                    "content": "ARA denied this turn because the Run Task supervisor channel failed.",
                },
            }
    return {"action": "allow"}


def query(op: str, **payload) -> dict:
    """Send a guard-style policy query to the supervisor.

    This is used by Python subprocess enforcement. It intentionally raises on
    transport failure so Run Task callers can fail closed before execution.
    """
    if os.environ.get("ARA_TRANSCRIPT_PROTOCOL") != "v1":
        raise RuntimeError("ARA transcript protocol is not enabled")
    s = _connect()
    if s is None:
        raise RuntimeError("ARA supervisor channel unavailable")
    seq = _next_seq()
    msg = {
        "type": "query",
        "seq": seq,
        "op": op,
        "pid": os.getpid(),
        **payload,
    }
    with _SOCK_LOCK:
        s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        line = _readline(s)
    rec = json.loads(line)
    if rec.get("seq") != seq:
        raise RuntimeError("ARA supervisor query sequence mismatch")
    return rec


def emit(role: str, payload: dict, *, model: Optional[str] = None) -> None:
    """Fire-and-forget shorthand around ``intercept`` for cases where the
    caller doesn't need a decision (post-response logging).  The
    supervisor still records the turn — only the return value is
    discarded."""
    intercept(role, payload, model=model)
