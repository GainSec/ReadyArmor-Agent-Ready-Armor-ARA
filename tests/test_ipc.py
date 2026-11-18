import asyncio
import json
from pathlib import Path

from ara.ipc import Server


def _run(coro):
    return asyncio.run(coro)


def test_server_round_trips_a_message(short_tmp: Path):
    received: list[dict] = []

    async def handler(msg: dict) -> dict:
        received.append(msg)
        return {"ack": msg.get("id")}

    async def scenario() -> dict:
        server = Server(short_tmp / "guard.sock", handler)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
            writer.write((json.dumps({"id": 1, "hello": "world"}) + "\n").encode())
            await writer.drain()
            reply_line = await reader.readline()
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()
        return json.loads(reply_line)

    reply = _run(scenario())
    assert reply == {"ack": 1}
    assert received == [{"id": 1, "hello": "world"}]


def test_server_skips_malformed_lines(short_tmp: Path):
    received: list[dict] = []

    async def handler(msg: dict) -> dict:
        received.append(msg)
        return {"ack": True}

    async def scenario() -> dict:
        server = Server(short_tmp / "guard.sock", handler)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
            writer.write(b"not json at all\n")
            writer.write((json.dumps({"valid": True}) + "\n").encode())
            await writer.drain()
            reply_line = await reader.readline()
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()
        return json.loads(reply_line)

    reply = _run(scenario())
    assert reply == {"ack": True}
    assert received == [{"valid": True}]


def test_handler_exception_returns_error_reply(short_tmp: Path):
    async def handler(_msg: dict) -> dict:
        raise RuntimeError("boom")

    async def scenario() -> dict:
        server = Server(short_tmp / "guard.sock", handler)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
            writer.write((json.dumps({"x": 1}) + "\n").encode())
            await writer.drain()
            reply_line = await reader.readline()
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()
        return json.loads(reply_line)

    reply = _run(scenario())
    assert reply["type"] == "error"
    assert "boom" in reply["error"]


def test_stop_removes_socket_file(short_tmp: Path):
    async def handler(_msg: dict) -> None:
        return None

    async def scenario() -> bool:
        server = Server(short_tmp / "guard.sock", handler)
        await server.start()
        existed = server.socket_path.exists()
        await server.stop()
        still = server.socket_path.exists()
        return existed and not still

    assert _run(scenario()) is True


def test_fire_and_forget_when_handler_returns_none(short_tmp: Path):
    received: list[dict] = []

    async def handler(msg: dict) -> None:
        received.append(msg)
        return None

    async def scenario() -> None:
        server = Server(short_tmp / "guard.sock", handler)
        await server.start()
        try:
            _, writer = await asyncio.open_unix_connection(str(server.socket_path))
            writer.write((json.dumps({"event": "ping"}) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            # Give the server loop a tick to process before stopping.
            await asyncio.sleep(0.05)
        finally:
            await server.stop()

    _run(scenario())
    assert received == [{"event": "ping"}]


def test_broadcast_prunes_failed_writers(short_tmp: Path):
    class FailedWriter:
        closed = False

        def write(self, _line: bytes) -> None:
            raise BrokenPipeError("gone")

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    async def handler(_msg: dict) -> None:
        return None

    async def scenario() -> tuple[int, bool, int]:
        server = Server(short_tmp / "guard.sock", handler)
        writer = FailedWriter()
        server._clients.add(writer)  # type: ignore[arg-type]
        sent = await server.broadcast({"type": "sync_rules_update"})
        return sent, writer.closed, len(server._clients)

    sent, closed, remaining = _run(scenario())
    assert sent == 0
    assert closed is True
    assert remaining == 0
