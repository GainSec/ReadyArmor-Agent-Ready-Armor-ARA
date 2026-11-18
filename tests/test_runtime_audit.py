"""Tests for runtime.audit (sync hash-chained writer)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ara.audit import verify_chain as ara_verify_chain
from ara.hashchain import GENESIS_HASH
from ara.runtime import audit as raudit


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()


def test_first_append_uses_genesis_prev_hash(tmp_path: Path) -> None:
    sink = tmp_path / "audit.jsonl"
    ev = raudit.append(sink, {"x": 1}, kind="phase.transition")
    assert ev.prev_hash == GENESIS_HASH
    assert ev.kind == "phase.transition"
    assert ev.body == {"x": 1}

    rec = json.loads(sink.read_text().splitlines()[0])
    assert rec["prev_hash"] == GENESIS_HASH
    assert rec["kind"] == "phase.transition"
    assert rec["body"] == {"x": 1}


def test_chain_links_each_event(tmp_path: Path) -> None:
    sink = tmp_path / "audit.jsonl"
    e1 = raudit.append(sink, {"i": 1}, kind="k")
    e2 = raudit.append(sink, {"i": 2}, kind="k")
    e3 = raudit.append(sink, {"i": 3}, kind="k")
    assert e2.prev_hash == e1.self_hash
    assert e3.prev_hash == e2.self_hash
    ok, last, err = ara_verify_chain(sink)
    assert ok and err is None and last == 3


def test_resume_chain_after_writer_recreated(tmp_path: Path) -> None:
    sink = tmp_path / "audit.jsonl"
    raudit.append(sink, {"i": 1}, kind="k")
    raudit.append(sink, {"i": 2}, kind="k")
    raudit.reset_registry_for_tests()
    e3 = raudit.append(sink, {"i": 3}, kind="k")
    # Third event's prev_hash must equal the on-disk second event's self_hash
    lines = sink.read_text().splitlines()
    assert len(lines) == 3
    rec3 = json.loads(lines[2])
    assert rec3["prev_hash"] == e3.prev_hash
    ok, _, err = ara_verify_chain(sink)
    assert ok, err


def test_tampering_breaks_verify(tmp_path: Path) -> None:
    sink = tmp_path / "audit.jsonl"
    raudit.append(sink, {"i": 1}, kind="k")
    raudit.append(sink, {"i": 2}, kind="k")
    raudit.append(sink, {"i": 3}, kind="k")
    # Mutate event 2 body
    lines = sink.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["body"]["i"] = 99
    lines[1] = json.dumps(rec, sort_keys=True)
    sink.write_text("\n".join(lines) + "\n")
    ok, broken_at, err = ara_verify_chain(sink)
    assert not ok
    assert broken_at == 3  # event 3's prev_hash no longer matches mutated event 2
    assert err and "prev_hash" in err


def test_concurrent_appends_serialize(tmp_path: Path) -> None:
    sink = tmp_path / "audit.jsonl"
    n = 30
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        raudit.append(sink, {"i": i}, kind="k")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = sink.read_text().splitlines()
    assert len(lines) == n
    ok, last, err = ara_verify_chain(sink)
    assert ok, err
    assert last == n


def test_writer_for_returns_same_instance(tmp_path: Path) -> None:
    sink = tmp_path / "x.jsonl"
    a = raudit.writer_for(sink)
    b = raudit.writer_for(sink)
    assert a is b


def test_creates_parent_dir(tmp_path: Path) -> None:
    sink = tmp_path / "deep" / "nested" / "audit.jsonl"
    raudit.append(sink, {"x": 1}, kind="k")
    assert sink.exists()
