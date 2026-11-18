"""Tests for the shared Armory data layer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ara.armory.data import (
    iter_policy_hits,
    iter_recent_events,
    list_sessions,
    session_detail,
)


def _mk_session(
    root: Path, sid: str, *,
    events=None, hits=None, transcript=None,
    contained=False, pid=None, started_at=None,
):
    sdir = root / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    meta = {"id": sid}
    if pid is not None:
        meta["pid"] = pid
    if started_at is not None:
        meta["started_at"] = started_at
    (sdir / "session.json").write_text(json.dumps(meta))
    if events:
        (sdir / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )
    if hits:
        (sdir / "policy_hits.jsonl").write_text(
            "\n".join(json.dumps(h) for h in hits) + "\n"
        )
    if transcript:
        (sdir / "transcript.jsonl").write_text(
            "\n".join(json.dumps(t) for t in transcript) + "\n"
        )
    if contained:
        (sdir / "contain.manifest.json").write_text(
            json.dumps({"home": str(sdir / "contain" / "home")})
        )
        (sdir / "contain" / "home").mkdir(parents=True, exist_ok=True)
        (sdir / "contain" / "home" / "stub.txt").write_text("x")
    return sdir


def test_empty_state_root(tmp_path: Path):
    assert list_sessions(tmp_path) == []
    assert session_detail(tmp_path, "nope") is None
    assert list(iter_policy_hits(tmp_path)) == []
    assert iter_recent_events(tmp_path) == []


def test_list_sessions_sorted_newest_first(tmp_path: Path):
    _mk_session(tmp_path, "a", started_at=time.time() - 1000)
    time.sleep(0.01)
    _mk_session(tmp_path, "b", started_at=time.time())
    rows = list_sessions(tmp_path)
    assert [r.id for r in rows] == ["b", "a"]


def test_list_sessions_counts_hits_denies_guards(tmp_path: Path):
    _mk_session(
        tmp_path, "s1",
        events=[{"kind": "hello", "pid": 1}, {"kind": "heartbeat"}],
        hits=[
            {"verdict": "allow", "ts": time.time()},
            {"verdict": "deny", "ts": time.time()},
            {"verdict": "allow", "ts": time.time()},
        ],
    )
    row = list_sessions(tmp_path)[0]
    assert row.hits == 3
    assert row.denies == 1
    assert row.guards == 1


def test_list_sessions_active_flag_tracks_pid(tmp_path: Path):
    # Live process: our own pid.
    _mk_session(tmp_path, "live", pid=os.getpid())
    # Dead pid: pick something unlikely to exist.
    _mk_session(tmp_path, "dead", pid=999_999_999)
    rows = {r.id: r for r in list_sessions(tmp_path)}
    assert rows["live"].is_active is True
    assert rows["dead"].is_active is False


def test_list_sessions_contained_flag(tmp_path: Path):
    _mk_session(tmp_path, "c", contained=True)
    row = list_sessions(tmp_path)[0]
    assert row.is_contained is True
    assert row.contain_files >= 1
    assert row.contain_bytes >= 1
    assert row.contain_home.endswith("home")


def test_session_detail_returns_none_for_missing(tmp_path: Path):
    assert session_detail(tmp_path, "nope") is None


def test_session_detail_populates_all_streams(tmp_path: Path):
    _mk_session(
        tmp_path, "s",
        events=[{"kind": "hello", "pid": 42, "ts": 1.0}],
        hits=[{"verdict": "deny", "ts": 2.0}],
        transcript=[{"role": "request", "ts": 3.0}],
        contained=True,
    )
    d = session_detail(tmp_path, "s")
    assert d is not None
    assert d.row.id == "s"
    assert len(d.events) == 1
    assert len(d.policy_hits) == 1
    assert d.row.denies == 1
    assert d.contain_manifest is not None
    assert len(d.transcript) == 1


def test_session_detail_tails_streams(tmp_path: Path):
    big_events = [{"kind": "heartbeat", "ts": float(i)} for i in range(500)]
    _mk_session(tmp_path, "s", events=big_events)
    d = session_detail(tmp_path, "s", events_tail=50)
    assert d is not None
    assert len(d.events) == 50
    # Last 50 entries — tails preserve order.
    assert d.events[-1]["ts"] == 499.0


def test_iter_policy_hits_window(tmp_path: Path):
    now = time.time()
    _mk_session(
        tmp_path, "s",
        hits=[
            {"verdict": "deny", "ts": now - 7200},   # older than 1h window
            {"verdict": "allow", "ts": now},
        ],
    )
    hits = list(iter_policy_hits(tmp_path, window_s=3600.0))
    assert len(hits) == 1
    assert hits[0]["verdict"] == "allow"


def test_iter_policy_hits_single_session_filter(tmp_path: Path):
    now = time.time()
    _mk_session(tmp_path, "a", hits=[{"verdict": "allow", "ts": now}])
    _mk_session(tmp_path, "b", hits=[{"verdict": "deny", "ts": now}])
    only_b = list(iter_policy_hits(tmp_path, session="b"))
    assert len(only_b) == 1
    assert only_b[0]["verdict"] == "deny"


def test_iter_recent_events_tags_source(tmp_path: Path):
    now = time.time()
    _mk_session(
        tmp_path, "s",
        events=[{"kind": "hello", "ts": now}],
        hits=[{"verdict": "allow", "ts": now}],
    )
    rows = iter_recent_events(tmp_path)
    sources = {r["_source"] for r in rows}
    assert sources == {"events.jsonl", "policy_hits.jsonl"}


def test_iter_recent_events_sorted_newest_first(tmp_path: Path):
    _mk_session(
        tmp_path, "s",
        events=[
            {"kind": "heartbeat", "ts": 1.0},
            {"kind": "heartbeat", "ts": 3.0},
            {"kind": "heartbeat", "ts": 2.0},
        ],
    )
    ts = [r.get("ts") for r in iter_recent_events(tmp_path)]
    assert ts == sorted(ts, reverse=True)
