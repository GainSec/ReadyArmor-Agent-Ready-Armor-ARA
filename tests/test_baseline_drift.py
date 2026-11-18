"""Audit #29 — drift_score() summarizes a session's delta against a
baseline as a ratio + counts + sample lists."""

from __future__ import annotations

import time
from pathlib import Path

from ara import baseline as bl_mod


def _mk_session_with_hits(
    state: Path, sid: str, *,
    paths: list[str], commands: list[str], hosts: list[str],
) -> Path:
    sdir = state / "sessions" / sid
    sdir.mkdir(parents=True)
    import json as _json
    lines = []
    for p in paths:
        lines.append(_json.dumps({"ts": time.time(), "op": "fs.read", "path": p}))
    for c in commands:
        lines.append(_json.dumps({"ts": time.time(), "op": "exec.spawn",
                                  "command": c, "args": []}))
    for h in hosts:
        lines.append(_json.dumps({"ts": time.time(), "op": "net.connect",
                                  "host": h, "port": 443}))
    (sdir / "policy_hits.jsonl").write_text("\n".join(lines) + "\n")
    return sdir


def test_drift_zero_when_session_matches_baseline(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    bl = bl_mod.Baseline(
        name="b", created_at=0,
        paths=["/a", "/b"], commands=["ls"], hosts=["example.com"],
    )
    sdir = _mk_session_with_hits(
        state, "s" * 32, paths=["/a"], commands=["ls"], hosts=[],
    )
    score = bl_mod.drift_score(bl, sdir)
    assert score["new_count"] == 0
    assert score["drift_ratio"] == 0.0


def test_drift_counts_new_items_not_in_baseline(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    bl = bl_mod.Baseline(
        name="b", created_at=0,
        paths=["/a"], commands=[], hosts=[],
    )
    sdir = _mk_session_with_hits(
        state, "t" * 32,
        paths=["/a", "/b", "/c"], commands=["curl"], hosts=["evil.example"],
    )
    score = bl_mod.drift_score(bl, sdir)
    assert score["new_count"] == 4  # /b, /c, curl, evil.example
    assert "/b" in score["new_paths"]
    assert "/c" in score["new_paths"]
    assert "curl" in score["new_commands"]
    assert any("evil.example" in h for h in score["new_hosts"])
    # Baseline had 1 item; 4 new; ratio = 4.0.
    assert score["drift_ratio"] == 4.0


def test_drift_empty_baseline_reports_full_drift_when_session_has_activity(
    short_tmp: Path,
):
    state = short_tmp / "state"
    state.mkdir()
    bl = bl_mod.Baseline(name="empty", created_at=0)
    sdir = _mk_session_with_hits(
        state, "u" * 32, paths=["/x"], commands=[], hosts=[],
    )
    score = bl_mod.drift_score(bl, sdir)
    assert score["baseline_total"] == 0
    assert score["drift_ratio"] == 1.0


def test_drift_empty_baseline_and_empty_session_is_zero(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    bl = bl_mod.Baseline(name="empty", created_at=0)
    sdir = _mk_session_with_hits(
        state, "v" * 32, paths=[], commands=[], hosts=[],
    )
    score = bl_mod.drift_score(bl, sdir)
    assert score["drift_ratio"] == 0.0
