"""Line-delimited JSON over unix socket. Supervisor side of the ARA <-> GUARD link."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

Handler = Callable[[dict], Awaitable[Optional[dict]]]


class Server:
    """One unix-socket server, line-delimited JSON, pluggable message handler."""

    def __init__(self, socket_path: Path, handler: Handler) -> None:
        self.socket_path = socket_path
        self.handler = handler
        self._server: Optional[asyncio.Server] = None
        self._clients: set[asyncio.StreamWriter] = set()

    async def broadcast(self, msg: dict) -> int:
        """Send an unsolicited message to every connected client. Returns
        the number of clients that received it."""
        line = (json.dumps(msg) + "\n").encode()
        sent = 0
        for writer in list(self._clients):
            try:
                writer.write(line)
                await writer.drain()
                sent += 1
            except Exception:
                self._clients.discard(writer)
                with contextlib.suppress(Exception):
                    writer.close()
                continue
        return sent

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        # Default StreamReader limit is 64 KB; raise to 16 MB so guards can
        # ship single-line IPC messages carrying large payloads (Codex
        # prompt envelopes routinely exceed 100 KB once tools + context +
        # captured system prompt are inlined). Above this we'd start
        # hitting LimitOverrunError on otherwise-legal messages.
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path),
            limit=16 * 1024 * 1024,
        )

    async def stop(self) -> None:
        if self._server is not None:
            # Drop active client connections so peers notice the shutdown.
            close_clients = getattr(self._server, "close_clients", None)
            if callable(close_clients):
                with contextlib.suppress(Exception):
                    close_clients()
            else:
                # 3.9-3.12 fallback: close tracked writers by hand.
                for writer in list(self._clients):
                    with contextlib.suppress(Exception):
                        writer.close()
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self.socket_path.exists():
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("server not started")
        await self._server.serve_forever()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._clients.add(writer)
        try:
            while not reader.at_eof():
                try:
                    line = await reader.readline()
                except (ConnectionResetError, BrokenPipeError):
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Malformed line: log-and-continue would be the supervisor's
                    # job; for the transport layer, just skip.
                    continue
                try:
                    reply = await self.handler(msg)
                except Exception as exc:  # noqa: BLE001
                    try:
                        from . import debug_log
                        debug_log.emit_exception(
                            "ipc", "handler.exception", exc,
                            state_root=os.environ.get("ARA_STATE_ROOT"),
                            msg_type=msg.get("type"),
                            op=msg.get("op"),
                            command=msg.get("command"),
                            seq=msg.get("seq"),
                        )
                    except Exception:
                        pass
                    reply = {"type": "error", "error": str(exc)}
                if reply is not None:
                    writer.write((json.dumps(reply) + "\n").encode())
                    await writer.drain()
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
