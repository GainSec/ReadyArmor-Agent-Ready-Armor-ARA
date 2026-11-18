"""Minimal RFC 6455 WebSocket frame decoder + ACP envelope parser.

No dependencies. Handles text/binary frames, continuations, ping/pong
passthrough, close frames. Good enough to extract the JSON envelopes
OpenClaw's gateway ships between clients and the server.

ACP envelopes (from ``dist/plugin-sdk/src/gateway/protocol/schema.d.ts``)::

    RequestFrame  = { "type": "req", "id": <uuid>, "method": <str>, "params": <any> }
    ResponseFrame = { "type": "res", "id": <uuid>, "result": <any> }
                    or { "type": "res", "id": <uuid>, "error": <ErrorShape> }
    EventFrame    = { "type": "evt", ... }
    GatewayFrame  = union of the above

This module does NOT enforce — it parses + summarises. Callers
(``gateway_ws_tap``) decide what to do with each frame.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Optional


# ---- WebSocket frame decoding ---------------------------------------

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


@dataclass
class WsFrame:
    fin: bool
    opcode: int
    payload: bytes
    masked: bool
    frame_length: int  # total bytes consumed off the wire


def parse_frame(buf: bytes, offset: int = 0) -> Optional[WsFrame]:
    """Parse one WebSocket frame starting at ``buf[offset:]``.

    Returns None if ``buf`` doesn't contain a complete frame yet.
    Otherwise returns a ``WsFrame``. Caller advances its buffer by
    ``frame.frame_length`` bytes.
    """
    if len(buf) - offset < 2:
        return None
    b0 = buf[offset]
    b1 = buf[offset + 1]
    fin = (b0 & 0x80) != 0
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F
    idx = offset + 2
    if length == 126:
        if len(buf) - idx < 2:
            return None
        (length,) = struct.unpack("!H", buf[idx : idx + 2])
        idx += 2
    elif length == 127:
        if len(buf) - idx < 8:
            return None
        (length,) = struct.unpack("!Q", buf[idx : idx + 8])
        idx += 8
    mask_key = b""
    if masked:
        if len(buf) - idx < 4:
            return None
        mask_key = buf[idx : idx + 4]
        idx += 4
    if len(buf) - idx < length:
        return None
    payload = buf[idx : idx + length]
    if masked:
        payload = bytes(
            b ^ mask_key[i % 4] for i, b in enumerate(payload)
        )
    return WsFrame(
        fin=fin,
        opcode=opcode,
        payload=payload,
        masked=masked,
        frame_length=idx + length - offset,
    )


# ---- frame-reassembly state machine ---------------------------------

class FrameAssembler:
    """Buffer partial WebSocket streams, yield complete text/binary
    messages. Handles fragmented messages via the continuation opcode.

    Usage::

        asm = FrameAssembler()
        for msg_bytes, opcode, was_text in asm.feed(chunk):
            ...

    Control frames (ping/pong/close) pass through as ``(bytes, opcode,
    False)`` — callers generally ignore them, but this keeps the API
    symmetric.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._frag_opcode: Optional[int] = None
        self._frag: bytearray = bytearray()

    def feed(self, data: bytes) -> list[tuple[bytes, int, bool]]:
        self._buf.extend(data)
        out: list[tuple[bytes, int, bool]] = []
        offset = 0
        while True:
            frame = parse_frame(bytes(self._buf), offset)
            if frame is None:
                break
            offset += frame.frame_length
            op = frame.opcode
            if op in (OP_PING, OP_PONG, OP_CLOSE):
                out.append((bytes(frame.payload), op, False))
                continue
            if op == OP_CONTINUATION:
                self._frag.extend(frame.payload)
                if frame.fin:
                    msg = bytes(self._frag)
                    assert self._frag_opcode is not None
                    out.append((msg, self._frag_opcode,
                                self._frag_opcode == OP_TEXT))
                    self._frag = bytearray()
                    self._frag_opcode = None
                continue
            # Text or binary start.
            if frame.fin:
                out.append((bytes(frame.payload), op, op == OP_TEXT))
            else:
                self._frag_opcode = op
                self._frag = bytearray(frame.payload)
        # Compact buffer.
        if offset > 0:
            del self._buf[:offset]
        return out


# ---- ACP envelope parsing -------------------------------------------

@dataclass
class AcpEnvelope:
    kind: str            # "req" | "res" | "evt" | "other"
    id: Optional[str]
    method: Optional[str]
    params_summary: Optional[str]
    result_summary: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    raw: dict


def parse_acp(body: bytes) -> Optional[AcpEnvelope]:
    """Best-effort JSON → ACP envelope. Returns None for non-JSON or
    non-object bodies (e.g., streamed binary frames)."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    kind_raw = obj.get("type")
    kind = kind_raw if kind_raw in ("req", "res", "evt") else "other"
    frame_id = obj.get("id") if isinstance(obj.get("id"), str) else None
    method = obj.get("method") if isinstance(obj.get("method"), str) else None

    params_summary: Optional[str] = None
    if "params" in obj:
        params_summary = _summarize(obj["params"], 200)

    result_summary: Optional[str] = None
    if "result" in obj:
        result_summary = _summarize(obj["result"], 200)

    error_code: Optional[str] = None
    error_message: Optional[str] = None
    err = obj.get("error")
    if isinstance(err, dict):
        error_code = err.get("code") if isinstance(err.get("code"), str) else None
        error_message = err.get("message") if isinstance(err.get("message"), str) else None

    return AcpEnvelope(
        kind=kind,
        id=frame_id,
        method=method,
        params_summary=params_summary,
        result_summary=result_summary,
        error_code=error_code,
        error_message=error_message,
        raw=obj,
    )


def _summarize(value, max_len: int) -> str:
    try:
        s = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        s = repr(value)
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


# ---- HTTP-upgrade sniffing ------------------------------------------

def is_websocket_upgrade(chunk: bytes) -> bool:
    """Inexpensive check: do the first bytes look like an HTTP
    upgrade to WebSocket, from either direction?

    Client→server: ``Upgrade: websocket`` + ``Sec-WebSocket-Key:``.
    Server→client: ``HTTP/1.1 101 ...`` + ``Upgrade: websocket``
    (the accept header signals acceptance).
    """
    low = chunk[:2048].lower()
    if b"upgrade: websocket" not in low:
        return False
    # Request side.
    if b"sec-websocket-key:" in low:
        return True
    # Response side.
    if low.startswith(b"http/1.1 101") or low.startswith(b"http/1.0 101"):
        return True
    if b"sec-websocket-accept:" in low:
        return True
    return False


def find_header_end(chunk: bytes) -> int:
    """Return the byte offset immediately after the HTTP header block
    (i.e., the first byte of the WebSocket frame stream), or -1 if
    the header isn't fully present yet."""
    idx = chunk.find(b"\r\n\r\n")
    return -1 if idx < 0 else idx + 4


# ---- WebSocket frame encoding (for synthesising denies) ------------

def encode_frame(payload: bytes, *, opcode: int = OP_TEXT,
                 masked: bool = False, fin: bool = True) -> bytes:
    """Encode a single WebSocket frame. Server→client frames are
    unmasked (``masked=False``). Client→server must be masked; we
    rarely need to synthesise client-direction frames so that case
    is included for completeness."""
    import os as _os
    import struct as _struct
    b0 = (0x80 if fin else 0) | (opcode & 0x0F)
    mask_bit = 0x80 if masked else 0x00
    n = len(payload)
    out = bytearray([b0])
    if n < 126:
        out.append(mask_bit | n)
    elif n < 2**16:
        out.append(mask_bit | 126)
        out += _struct.pack("!H", n)
    else:
        out.append(mask_bit | 127)
        out += _struct.pack("!Q", n)
    if masked:
        mask_key = _os.urandom(4)
        out += mask_key
        out += bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return bytes(out)


def synthesize_deny_response(
    *, request_id: Optional[str], rule_id: Optional[str],
    reason: str = "ARA policy denied this call",
) -> bytes:
    """Build a full WebSocket text frame containing an ACP error
    response. ``request_id`` echoes the incoming request's id so the
    client correlates the response to its request.

    Returned bytes are server-direction (unmasked) and ready to
    ``sock.sendall()`` to the client."""
    body = {
        "type": "res",
        "id": request_id or "",
        "error": {
            "code": "ARA_DENY",
            "message": reason,
            "details": {"ara_rule_id": rule_id} if rule_id else {},
            "retryable": False,
        },
    }
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return encode_frame(payload, opcode=OP_TEXT, masked=False, fin=True)
