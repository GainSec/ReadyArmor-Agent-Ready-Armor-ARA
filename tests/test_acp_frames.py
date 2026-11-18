"""WebSocket frame decoder + ACP envelope parser."""

from __future__ import annotations

import base64

import json
import struct

from ara import acp_frames as af


# ---- WebSocket frame tests -----------------------------------------

def _build_frame(opcode: int, payload: bytes, *, masked: bool = False,
                 fin: bool = True) -> bytes:
    b0 = (0x80 if fin else 0) | (opcode & 0x0F)
    out = bytearray([b0])
    mask_bit = 0x80 if masked else 0x00
    n = len(payload)
    if n < 126:
        out.append(mask_bit | n)
    elif n < 2**16:
        out.append(mask_bit | 126)
        out += struct.pack("!H", n)
    else:
        out.append(mask_bit | 127)
        out += struct.pack("!Q", n)
    if masked:
        mask_key = b"\x01\x02\x03\x04"
        out += mask_key
        out += bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return bytes(out)


def test_unmasked_text_frame_roundtrip() -> None:
    frame = _build_frame(af.OP_TEXT, b"hello")
    parsed = af.parse_frame(frame)
    assert parsed is not None
    assert parsed.opcode == af.OP_TEXT
    assert parsed.payload == b"hello"
    assert parsed.fin is True


def test_masked_client_frame_is_unmasked() -> None:
    frame = _build_frame(af.OP_TEXT, b"hello world", masked=True)
    parsed = af.parse_frame(frame)
    assert parsed is not None
    assert parsed.payload == b"hello world"
    assert parsed.masked is True


def test_extended_length_16() -> None:
    payload = b"x" * 300
    frame = _build_frame(af.OP_BINARY, payload)
    parsed = af.parse_frame(frame)
    assert parsed is not None
    assert len(parsed.payload) == 300


def test_partial_frame_returns_none() -> None:
    frame = _build_frame(af.OP_TEXT, b"abc")
    assert af.parse_frame(frame[:1]) is None
    assert af.parse_frame(frame[:-1]) is None


def test_frame_assembler_reassembles_fragmented_message() -> None:
    part1 = _build_frame(af.OP_TEXT, b"hel", fin=False)
    part2 = _build_frame(af.OP_CONTINUATION, b"lo", fin=True)
    asm = af.FrameAssembler()
    msgs = asm.feed(part1 + part2)
    assert len(msgs) == 1
    assert msgs[0][0] == b"hello"
    assert msgs[0][2] is True   # was_text


def test_frame_assembler_passes_control_through() -> None:
    ping = _build_frame(af.OP_PING, b"ping")
    asm = af.FrameAssembler()
    msgs = asm.feed(ping)
    assert len(msgs) == 1
    assert msgs[0][1] == af.OP_PING


def test_frame_assembler_handles_chunked_delivery() -> None:
    frame = _build_frame(af.OP_TEXT, b'{"type":"req","id":"x","method":"ping"}')
    asm = af.FrameAssembler()
    # Feed one byte at a time — assembler must not panic on partials.
    out: list = []
    for i in range(len(frame)):
        out.extend(asm.feed(frame[i : i + 1]))
    assert len(out) == 1
    assert out[0][0].startswith(b'{"type":"req"')


# ---- ACP envelope tests --------------------------------------------

def test_parse_request_frame() -> None:
    env = af.parse_acp(json.dumps({
        "type": "req",
        "id": "r1",
        "method": "commands.list",
        "params": {"agentId": "main"},
    }).encode())
    assert env is not None
    assert env.kind == "req"
    assert env.id == "r1"
    assert env.method == "commands.list"
    assert env.params_summary == '{"agentId":"main"}'


def test_parse_response_frame_with_error() -> None:
    env = af.parse_acp(json.dumps({
        "type": "res",
        "id": "r1",
        "error": {"code": "AUTH_FAILED", "message": "bad token"},
    }).encode())
    assert env is not None
    assert env.kind == "res"
    assert env.error_code == "AUTH_FAILED"
    assert env.error_message == "bad token"


def test_parse_event_frame() -> None:
    env = af.parse_acp(json.dumps({
        "type": "evt",
        "event": "chat.update",
        "payload": {"sessionId": "s1"},
    }).encode())
    assert env is not None
    assert env.kind == "evt"


def test_parse_non_json_returns_none() -> None:
    assert af.parse_acp(b"not-json") is None


def test_parse_non_object_returns_none() -> None:
    assert af.parse_acp(b"[1,2,3]") is None


def test_summarize_truncates_large_payload() -> None:
    env = af.parse_acp(json.dumps({
        "type": "req",
        "id": "big",
        "method": "x",
        "params": {"data": "a" * 5000},
    }).encode())
    assert env is not None
    assert env.params_summary is not None
    assert len(env.params_summary) <= 201


# ---- HTTP upgrade sniffing -----------------------------------------

def test_upgrade_detected() -> None:
    req = (
        b"GET /gateway HTTP/1.1\r\n"
        b"Host: 127.0.0.1:18789\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: " + base64.b64encode(b"the sample nonce") + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    assert af.is_websocket_upgrade(req) is True
    assert af.find_header_end(req) == len(req)


def test_non_upgrade_ignored() -> None:
    req = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
    assert af.is_websocket_upgrade(req) is False


def test_partial_headers_not_yet_complete() -> None:
    partial = b"GET / HTTP/1.1\r\nHost: x\r\n"
    assert af.find_header_end(partial) == -1
