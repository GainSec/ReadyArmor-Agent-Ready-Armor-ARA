import asyncio
import json
from pathlib import Path
from ara.audit import JsonlWriter, verify_chain, _GENESIS_HASH


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_chain_links_and_verifies(short_tmp: Path):
    p = short_tmp / "hits.jsonl"
    w = JsonlWriter(p, chain=True)

    async def go():
        await w.append({"op": "fs.open", "verdict": "allow"})
        await w.append({"op": "fs.open", "verdict": "deny", "rule_id": "deny_path:1"})
        await w.append({"op": "exec.spawn", "verdict": "allow"})
    _run(go())

    lines = p.read_text().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["prev_hash"] == _GENESIS_HASH
    # verify_chain walks cleanly
    ok, lineno, err = verify_chain(p)
    assert ok, (lineno, err)
    assert lineno == 3


def test_chain_detects_tamper(short_tmp: Path):
    p = short_tmp / "hits.jsonl"
    w = JsonlWriter(p, chain=True)

    async def go():
        for i in range(4):
            await w.append({"op": "fs.open", "i": i, "verdict": "allow"})
    _run(go())

    lines = p.read_text().splitlines()
    # Flip the verdict on the middle line — prev_hash stays but body changes.
    rec = json.loads(lines[1])
    rec["verdict"] = "deny"
    lines[1] = json.dumps(rec, sort_keys=True)
    p.write_text("\n".join(lines) + "\n")

    ok, lineno, err = verify_chain(p)
    assert not ok
    # Tampered line is line 2; next line's prev_hash won't match recomputed.
    assert lineno == 3, (lineno, err)


def test_chain_resumes_across_writers(short_tmp: Path):
    p = short_tmp / "hits.jsonl"

    async def first():
        w = JsonlWriter(p, chain=True)
        await w.append({"op": "fs.open", "verdict": "allow"})
        await w.append({"op": "fs.open", "verdict": "allow"})
    _run(first())

    async def second():
        w = JsonlWriter(p, chain=True)
        await w.append({"op": "fs.open", "verdict": "deny"})
    _run(second())

    ok, _, err = verify_chain(p)
    assert ok, err


def test_unchained_writer_works_as_before(short_tmp: Path):
    p = short_tmp / "events.jsonl"
    w = JsonlWriter(p)

    async def go():
        await w.append({"kind": "hello"})
    _run(go())
    rec = json.loads(p.read_text().splitlines()[0])
    assert "prev_hash" not in rec
