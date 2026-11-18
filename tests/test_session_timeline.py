"""Audit #16 — per-session timeline viz collects points from the three
JSONL streams and renders an SVG chart. We test the collector here;
the SVG emission is smoke-covered by the main test suite."""

from __future__ import annotations

import json
from pathlib import Path

from ara.armory.views import _collect_timeline_points, _render_session_timeline


def _mk(state: Path, sid: str) -> Path:
    sdir = state / "sessions" / sid
    sdir.mkdir(parents=True)
    (sdir / "events.jsonl").write_text(
        '{"ts":100.0,"kind":"spawn"}\n'
        '{"ts":101.0,"kind":"start"}\n'  # not a spawn — skipped
    )
    (sdir / "policy_hits.jsonl").write_text(
        '{"ts":100.5,"kind":"deny","verdict":"deny"}\n'
    )
    (sdir / "transcript.jsonl").write_text(
        '{"ts":100.2,"role":"request"}\n'
        '{"ts":100.3,"role":"rhema_approval_pending"}\n'
        '{"ts":100.4,"role":"noise"}\n'  # not a recognized role — skipped
    )
    return sdir


def test_collect_timeline_keeps_known_kinds_and_sorts(short_tmp: Path):
    state = short_tmp / "state"
    _mk(state, "a" * 32)
    pts = _collect_timeline_points(state, "a" * 32)
    lanes = [p["lane"] for p in pts]
    assert "spawn" in lanes
    assert "policy" in lanes
    assert "transcript" in lanes
    assert "approval" in lanes
    # Sorted by ts.
    ts_list = [p["ts"] for p in pts]
    assert ts_list == sorted(ts_list)
    # Noise roles are dropped.
    assert all(p["kind"] != "noise" for p in pts)


def test_collect_timeline_empty_session_returns_nothing(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    assert _collect_timeline_points(state, "nope") == []


def test_render_timeline_empty_state(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    html = _render_session_timeline(state, "nope")
    assert "No events recorded" in html


def test_render_timeline_emits_svg_with_points(short_tmp: Path):
    state = short_tmp / "state"
    _mk(state, "b" * 32)
    html = _render_session_timeline(state, "b" * 32)
    assert "<svg" in html
    assert "circle" in html
    assert "events across" in html
