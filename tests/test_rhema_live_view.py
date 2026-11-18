from __future__ import annotations

import json
from pathlib import Path

from ara import armors as armors_mod
from ara.armory import views


def _chunk(*, reasoning: str | None = None, content: str | None = None) -> dict:
    delta = {}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if content is not None:
        delta["content"] = content
    return {"choices": [{"delta": delta, "finish_reason": None}]}


def test_summarize_payload_extracts_stream_content():
    payload = {"chunks": [_chunk(reasoning="thinking"), _chunk(content="answer")]}

    assert views._summarize_payload("response", payload) == "answer"


def test_summarize_payload_extracts_structured_body_text():
    payload = {
        "body_text": json.dumps({
            "success": True,
            "name": "Codebase-Inspection",
            "description": "Inspect codebases with language counts.",
        }),
    }

    assert views._summarize_payload(
        "request.out", payload,
    ) == "Codebase-Inspection: Inspect codebases with language counts."


def test_summarize_payload_extracts_structured_message_text():
    payload = {
        "messages": [
            {
                "role": "tool",
                "content": json.dumps({
                    "success": True,
                    "name": "codebase-inspection",
                    "description": "Inspect codebases w/ pygount: LOC, languages.",
                }),
            },
        ],
    }

    assert views._summarize_payload(
        "request", payload,
    ) == "codebase-inspection: Inspect codebases w/ pygount: LOC, languages."


def test_render_rhema_detail_readable_stream_chunks():
    rec = {
        "role": "response",
        "model": "qwen/qwen3.6-35b-a3b",
        "payload": {
            "stream": True,
            "complete": True,
            "chunks": [
                _chunk(reasoning="private reasoning"),
                _chunk(content="visible answer"),
            ],
        },
    }

    html = views._render_rhema_detail(rec)

    assert "visible answer" in html
    assert "private reasoning" in html
    assert "thoughts" in html
    assert "No assistant text extracted" not in html


def test_render_rhema_detail_readable_direct_request_messages():
    rec = {
        "role": "request",
        "model": "qwen/qwen3.6-35b-a3b",
        "payload": {
            "model": "qwen/qwen3.6-35b-a3b",
            "messages": [{"role": "user", "content": "hello rhema"}],
        },
    }

    html = views._render_rhema_detail(rec)

    assert "hello rhema" in html
    assert "Empty request body" not in html


def test_render_rhema_uses_full_session_links(tmp_path: Path):
    state = tmp_path / ".ara"
    sid = "118c81f8b4ac4fa5886baf4e80220878"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    records = [
        {
            "ts": 1,
            "role": "request",
            "model": "qwen",
            "payload": {"messages": [{"role": "user", "content": "hi"}]},
        },
        {
            "ts": 2,
            "role": "response",
            "model": "qwen",
            "payload": {"chunks": [_chunk(content="ok")]},
        },
    ]
    (session / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )

    html = views.render_rhema(state, "ara")

    assert f'href="/sessions/{sid}?t=ara"' in html


def test_render_rhema_live_uses_workbench_chrome(tmp_path: Path):
    state = tmp_path / ".ara"
    sid = "118c81f8b4ac4fa5886baf4e80220878"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    records = [
        {
            "ts": 1,
            "role": "request",
            "model": "qwen",
            "payload": {"messages": [{"role": "user", "content": "hi"}]},
        },
        {
            "ts": 2,
            "role": "response",
            "model": "qwen",
            "payload": {"chunks": [_chunk(content="ok")]},
        },
    ]
    (session / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )

    html = views.render_rhema(state, "ara")

    assert 'class="rhema-workbench"' in html
    assert 'class="rhema-mode-tabs"' in html
    assert "Interceptions" in html
    assert "1 pair" in html
    assert "Readable" in html


def test_render_rhema_live_uses_focus_and_envelope_hand_layout(tmp_path: Path):
    state = tmp_path / ".ara"
    sid = "118c81f8b4ac4fa5886baf4e80220878"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    records = [
        {
            "ts": 1,
            "role": "request",
            "model": "qwen",
            "payload": {"messages": [{"role": "user", "content": "first ask"}]},
        },
        {
            "ts": 2,
            "role": "response",
            "model": "qwen",
            "payload": {"chunks": [_chunk(content="first reply")]},
        },
        {
            "ts": 3,
            "role": "request",
            "model": "qwen",
            "payload": {"messages": [{"role": "user", "content": "second ask"}]},
        },
        {
            "ts": 4,
            "role": "response",
            "model": "qwen",
            "payload": {"chunks": [_chunk(content="second reply")]},
        },
    ]
    (session / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )

    html = views.render_rhema(state, "ara")

    assert "rhema-codex" in html
    assert 'class="rhema-codex-layout"' in html
    assert 'class="rhema-codex-controls"' in html
    assert 'class="rhema-focus-grid"' in html
    assert 'class="rhema-inspector"' in html
    assert 'class="rhema-ledger rhema-envelope-hand"' in html
    assert 'class="rhema-selected-card rhema-card"' in html
    assert 'class="rhema-hand-stage"' in html
    assert 'class="rhema-hand-drawer"' in html
    assert 'rhema-hand-card rhema-ledger-row rhema-pair' in html
    assert 'rhema-card-face rhema-card-face-ask' in html
    assert 'rhema-card-face rhema-card-face-reply' in html
    assert "Flow filters" in html
    assert "Envelope hand" in html
    assert "Selected envelope" in html
    assert "Rule stack" in html
    assert "Current envelope" in html
    assert "first ask" in html
    assert "second reply" in html
    assert "rhema-deck-filter" in html
    assert "rhema-events-refresh" in html


def test_render_envelope_uses_codex_forge_editor_layout(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    state.mkdir()
    armors_mod.create_armor(state, "hermes-default", description="test")
    armor = armors_mod.load_armor(state, "hermes-default")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text(
        "## Core oath\n\nHold the line.\n\n## Tool doctrine\n\nUse tools cleanly.",
    )
    armors_mod.set_active_armor(state, "hermes-default")
    sid = "capture-session"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    (session / "transcript.jsonl").write_text(
        json.dumps({
            "ts": 1,
            "role": "request",
            "model": "qwen",
            "payload": {
                "body": {
                    "instructions": (
                        "# Captured core\n\nBaseline prompt.\n\n"
                        "## Captured tools\n\nTool prompt."
                    ),
                    "model": "qwen",
                },
            },
        }) + "\n",
    )

    html = views._render_envelope_body(state, "ara")

    assert 'class="abs-source-drawer"' in html
    assert "<summary" in html
    assert "Source & armor" in html
    assert 'class="abs-forge-shell"' in html
    assert 'class="abs-forge-panel abs-forge-sections"' in html
    assert 'class="abs-forge-panel abs-forge-output"' in html
    assert 'class="abs-forge-panel abs-forge-source"' not in html
    assert "Armor, file, and provenance" in html
    assert "Section forge" in html
    assert "Composed envelope" in html
    assert 'class="abs-readable-output"' in html
    assert 'class="abs-raw-editor-drawer"' in html
    assert 'class="abs-raw-editor-drawer" open' not in html
    assert 'class="abs-captured-drawer"' in html
    assert 'class="abs-captured-drawer" open' not in html
    assert 'id="abs-editor-rows"' in html
    assert 'textarea name="contents"' in html
    assert 'class="abs-composed-textarea"' in html
    assert "Core oath" in html
    assert "Tool doctrine" in html


def test_render_rhema_live_features_completed_pair_before_orphan_reply(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".ara"
    sid = "118c81f8b4ac4fa5886baf4e80220878"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    records = [
        {
            "ts": 1,
            "role": "response",
            "model": "qwen",
            "payload": {"chunks": [_chunk(content="orphan reply")]},
        },
        {
            "ts": 2,
            "role": "request",
            "model": "qwen",
            "payload": {"messages": [{"role": "user", "content": "usable ask"}]},
        },
        {
            "ts": 3,
            "role": "response",
            "model": "qwen",
            "payload": {"chunks": [_chunk(content="usable reply")]},
        },
    ]
    (session / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )

    html = views.render_rhema(state, "ara")
    feature = html.split('class="rhema-inspector"', 1)[1].split(
        'class="rhema-rule-stack"', 1,
    )[0]

    assert "ASK / REPLY" in feature
    assert "usable ask" in feature
    assert "usable reply" in feature
    assert "SINGLE TURN" not in feature


def test_render_rhema_live_card_title_preserves_display_summary(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    sid = "118c81f8b4ac4fa5886baf4e80220878"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    records = [
        {
            "ts": 1,
            "role": "request.out",
            "model": "qwen",
            "payload": {
                "body_text": json.dumps({
                    "name": "MiXeD-Forge",
                    "description": "Keep Capitalized Preview",
                }),
            },
        },
        {
            "ts": 2,
            "role": "response.in",
            "model": "qwen",
            "payload": {"status": 200, "body_text": '{"ok":true}'},
        },
    ]
    (session / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )

    html = views.render_rhema(state, "ara")
    feature = html.split('class="rhema-inspector"', 1)[1].split(
        'class="rhema-rule-stack"', 1,
    )[0]

    assert (
        '<p class="rhema-card-title">MiXeD-Forge: Keep Capitalized Preview</p>'
        in feature
    )
    assert (
        '<p class="rhema-card-title">mixed-forge: keep capitalized preview</p>'
        not in feature
    )


def test_render_rhema_live_lazy_loads_large_visible_turns(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    sid = "118c81f8b4ac4fa5886baf4e80220878"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    huge = "VISIBLE-LARGE-RHEMA-PAYLOAD-" + ("x" * 90000)
    records = [
        {
            "ts": 1,
            "role": "request",
            "model": "qwen",
            "payload": {"messages": [{"role": "user", "content": huge}]},
        },
        {
            "ts": 2,
            "role": "response",
            "model": "qwen",
            "payload": {"chunks": [_chunk(content="short reply")]},
        },
    ]
    (session / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )

    html = views.render_rhema(state, "ara")

    assert len(html) < 70000
    assert huge not in html
    assert "rhema-detail-stub" in html
    assert "off=" in html
    assert "short reply" in html


def test_render_rhema_turn_detail_can_load_by_byte_offset(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    sid = "118c81f8b4ac4fa5886baf4e80220878"
    session = state / "sessions" / sid
    session.mkdir(parents=True)
    first = json.dumps({
        "ts": 1,
        "role": "request",
        "payload": {"messages": [{"role": "user", "content": "first"}]},
    }) + "\n"
    offset = len(first.encode())
    second_text = "loaded by offset"
    second = json.dumps({
        "ts": 2,
        "role": "response",
        "payload": {"chunks": [_chunk(content=second_text)]},
    }) + "\n"
    (session / "transcript.jsonl").write_text(first + second)

    html = views.render_rhema_turn_detail(state, sid, line_idx=None, offset=offset)

    assert html is not None
    assert second_text in html
    assert "first" not in html
