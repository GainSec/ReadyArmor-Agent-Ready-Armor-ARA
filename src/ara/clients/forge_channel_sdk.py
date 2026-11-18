"""ForgeChannelClient — thin client for forge-v1 channel calls over IPC.

Used by guard plugins / agent SDKs to invoke channels on the
supervisor without importing the channel framework directly. Wraps
the line-delimited-JSON protocol that the supervisor's IPC server
speaks.

Typical usage::

    client = ForgeChannelClient(socket_path, session_token)
    await client.connect()
    try:
        result = await client.call("fact.assert", {
            "predicate": "target.host", "value": "example.com",
            "asserted_by_crumb": "c1", "evidence_refs": ["e:abc..."],
        })
    except ChannelCallError as e:
        # supervisor-side rejection or transport error
        ...
    finally:
        await client.close()

Message protocol matches ``Supervisor._on_channel_call``:

- request:  ``{type: "channel_call", seq: int, channel: str, body: dict}``
- response: ``{type: "channel_call_response", seq: int,
              ok: bool, channel?: str, result?: dict,
              error_kind?: str, error?: str}``

The client matches responses by ``seq`` so concurrent calls on the
same socket are safe.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ChannelCallError(Exception):
    """Raised on supervisor-side rejection or transport error."""

    def __init__(self, error_kind: str, message: str,
                 *, channel: str | None = None) -> None:
        super().__init__(f"{error_kind}: {message}")
        self.error_kind = error_kind
        self.message = message
        self.channel = channel


@dataclass
class _Pending:
    future: "asyncio.Future[dict]"


class ForgeChannelClient:
    """Async client over the supervisor's line-delimited-JSON socket.

    One client wraps one connection. Safe to share across coroutines —
    requests are matched by sequence number; the reader loop fans
    responses out to waiting futures.
    """

    def __init__(self, socket_path: Path, session_token: str | None = None) -> None:
        self.socket_path = Path(socket_path)
        self.session_token = session_token
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, _Pending] = {}
        self._reader_task: asyncio.Task | None = None
        self._next_seq: int = 1
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def connected(self) -> bool:
        return (
            self._writer is not None
            and not self._closed
            and not self._writer.is_closing()
        )

    async def connect(self) -> None:
        if self.connected:
            return
        self._reader, self._writer = await asyncio.open_unix_connection(
            str(self.socket_path), limit=16 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        if self.session_token is not None:
            await self._send_no_response({
                "type": "hello",
                "token": self.session_token,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "runtime": "forge-channel-sdk",
                "argv": [],
            })
            # Note: we don't wait for hello_ack here; the supervisor's
            # auth check is informational for channel_call. Real guards
            # do await it.

    async def call(self, channel: str, body: dict | None = None,
                    *, timeout: float = 30.0) -> dict:
        """Send a channel_call and await the result.

        Raises :class:`ChannelCallError` on supervisor-side rejection
        (validation failure, tool-not-in-surface, no weapon equipped,
        etc.) or transport problem (connection closed, timeout).
        """
        if not self.connected:
            await self.connect()
        seq = await self._reserve_seq()
        msg = {"type": "channel_call", "seq": seq,
               "channel": channel, "body": body or {}}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[seq] = _Pending(future=fut)
        try:
            await self._send_raw(msg)
            try:
                resp = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as e:
                raise ChannelCallError(
                    "timeout",
                    f"no response after {timeout}s",
                    channel=channel,
                ) from e
        finally:
            self._pending.pop(seq, None)
        if not resp.get("ok"):
            raise ChannelCallError(
                resp.get("error_kind") or "unknown",
                resp.get("error") or "channel call failed",
                channel=channel,
            )
        return resp.get("result") or {}

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        # Fail any in-flight calls
        for seq, pending in list(self._pending.items()):
            if not pending.future.done():
                pending.future.set_exception(ChannelCallError(
                    "transport_closed", "client closed mid-call",
                ))
            self._pending.pop(seq, None)

    # ---------- internals ----------

    async def _reserve_seq(self) -> int:
        async with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            return seq

    async def _send_raw(self, msg: dict) -> None:
        if self._writer is None:
            raise ChannelCallError("transport_closed", "no writer")
        line = (json.dumps(msg) + "\n").encode()
        self._writer.write(line)
        await self._writer.drain()

    async def _send_no_response(self, msg: dict) -> None:
        await self._send_raw(msg)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch_response(msg)
        except asyncio.CancelledError:
            return
        except Exception:
            # Drop the connection; pending futures will be failed on close
            return
        finally:
            # Fail any still-pending so callers don't hang
            for seq, pending in list(self._pending.items()):
                if not pending.future.done():
                    pending.future.set_exception(ChannelCallError(
                        "transport_closed", "supervisor closed connection",
                    ))
                self._pending.pop(seq, None)

    def _dispatch_response(self, msg: dict) -> None:
        if msg.get("type") != "channel_call_response":
            return  # ignore unrelated server messages (hello_ack, decisions, etc.)
        seq = msg.get("seq")
        if not isinstance(seq, int):
            return
        pending = self._pending.get(seq)
        if pending is None or pending.future.done():
            return
        pending.future.set_result(msg)


# Convenience for one-shot calls without managing a client lifetime.

async def call_once(
    socket_path: Path, channel: str, body: dict | None = None,
    *, session_token: str | None = None, timeout: float = 30.0,
) -> dict:
    """One-shot: connect, call, close. Useful for CLI / tests; not
    efficient for repeated calls (use :class:`ForgeChannelClient`).
    """
    client = ForgeChannelClient(socket_path, session_token=session_token)
    try:
        await client.connect()
        return await client.call(channel, body, timeout=timeout)
    finally:
        await client.close()


# Type alias used by tests / type-hints
__all__ = ["ForgeChannelClient", "ChannelCallError", "call_once"]


# Silence Any unused warning for now
_ = (Any,)
