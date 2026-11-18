"""Tests for ActivationManager (forge-v1 orchestrator §9)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ara.runtime.activation import ActivationManager


def test_initial_mint_on_first_current(tmp_path: Path) -> None:
    am = ActivationManager(tmp_path)
    assert am.peek() is None
    aid = am.current()
    assert isinstance(aid, str) and len(aid) == 16
    assert am.peek() == aid
    assert (tmp_path / "activation.txt").read_text() == aid

    history = am.history()
    assert len(history) == 1
    assert history[0].activation_id == aid
    assert history[0].reason == "initial"
    assert history[0].chunk_id is None


def test_current_is_idempotent(tmp_path: Path) -> None:
    am = ActivationManager(tmp_path)
    a = am.current()
    b = am.current()
    assert a == b
    assert len(am.history()) == 1


def test_mint_changes_current_and_appends_history(tmp_path: Path) -> None:
    am = ActivationManager(tmp_path)
    a = am.current()
    b = am.mint(reason="chunk_swap", chunk_id="recon")
    assert b != a
    assert am.current() == b
    assert (tmp_path / "activation.txt").read_text() == b

    hist = am.history()
    assert len(hist) == 2
    assert hist[1].reason == "chunk_swap"
    assert hist[1].chunk_id == "recon"


def test_persistence_across_instances(tmp_path: Path) -> None:
    am1 = ActivationManager(tmp_path)
    am1.mint(reason="initial")
    am1.mint(reason="chunk_swap", chunk_id="exploit")

    am2 = ActivationManager(tmp_path)
    # Reloads current without minting
    assert am2.peek() is not None
    assert am2.current() == am2.peek()
    assert len(am2.history()) == 2


def test_history_format_on_disk(tmp_path: Path) -> None:
    am = ActivationManager(tmp_path)
    am.mint(reason="initial")
    am.mint(reason="chunk_swap", chunk_id="report")
    lines = (tmp_path / "activations.jsonl").read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[1])
    assert set(rec) == {"ts", "activation_id", "reason", "chunk_id"}
    assert rec["chunk_id"] == "report"


def test_concurrent_mint_serializes(tmp_path: Path) -> None:
    am = ActivationManager(tmp_path)
    n = 50
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        rid = am.mint(reason="chunk_swap", chunk_id="x")
        with lock:
            results.append(rid)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n
    assert len(set(results)) == n  # all unique
    # File reflects exactly one of them
    final = (tmp_path / "activation.txt").read_text()
    assert final in set(results)
    # History line count matches
    lines = (tmp_path / "activations.jsonl").read_text().splitlines()
    assert len(lines) == n


def test_creates_session_dir_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "sessions" / "abc123"
    am = ActivationManager(target)
    assert target.is_dir()
    am.current()
    assert (target / "activation.txt").exists()
