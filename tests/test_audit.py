import asyncio
import json
from pathlib import Path

from ara.audit import JsonlWriter


def test_append_writes_json_lines(short_tmp: Path):
    path = short_tmp / "events.jsonl"
    writer = JsonlWriter(path)

    async def go():
        await writer.append({"kind": "one"})
        await writer.append({"kind": "two", "extra": 42})

    asyncio.run(go())

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    assert a["kind"] == "one"
    assert b["kind"] == "two"
    assert b["extra"] == 42
    assert "ts" in a and isinstance(a["ts"], (int, float))


def test_concurrent_appends_do_not_interleave(short_tmp: Path):
    path = short_tmp / "events.jsonl"
    writer = JsonlWriter(path)

    async def go():
        await asyncio.gather(
            *(writer.append({"kind": "stress", "i": i}) for i in range(50))
        )

    asyncio.run(go())

    lines = path.read_text().splitlines()
    assert len(lines) == 50
    for line in lines:
        record = json.loads(line)
        assert record["kind"] == "stress"
        assert "i" in record


def test_creates_parent_directory(short_tmp: Path):
    path = short_tmp / "nested" / "deep" / "log.jsonl"
    writer = JsonlWriter(path)
    asyncio.run(writer.append({"kind": "first"}))
    assert path.is_file()
