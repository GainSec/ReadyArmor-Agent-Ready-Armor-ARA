"""Audit #13 — /shadows renders a field-level diff of original vs
variant, driven off the ``shadow_link`` record's pre/post fields."""

from __future__ import annotations

import json
from pathlib import Path

from ara.armory.views import (
    _collect_shadow_pairs,
    _render_shadow_spec_summary,
    _render_shadows_body,
    _render_shadow_request_diff,
    _render_shadow_response_diff,
    _unified_diff_html,
)


def _write_shadow_pair(
    sdir: Path, *,
    pair_id: str,
    ts: int,
    rule_id: str,
    original_text: str = "same body",
    variant_text: str = "same body",
    original_req_bytes: int = 100,
    error: dict | None = None,
) -> None:
    rows = [
        {
            "role": "shadow_link",
            "ts": ts,
            "payload": {
                "pair_id": pair_id,
                "rule_id": rule_id,
                "original_status": 200,
                "original_resp_bytes": len(original_text),
                "original_req_bytes": original_req_bytes,
                "field": "instructions",
                "original_field_value": "OLD",
                "mutated_field_value": "NEW",
                "original_response_body_text": original_text,
                "spec": {"op": "replace", "field": "instructions"},
            },
        },
        {
            "role": "shadow_variant_request",
            "ts": ts + 1,
            "payload": {"pair_id": pair_id, "body": {"instructions": "NEW"}},
        },
        {
            "role": "shadow_variant_response",
            "ts": ts + 2,
            "payload": {
                "pair_id": pair_id,
                "status": 200,
                "body_text": variant_text,
            },
        },
    ]
    if error:
        rows.append({
            "role": "shadow_error",
            "ts": ts + 3,
            "payload": {"pair_id": pair_id, **error},
        })
    with (sdir / "transcript.jsonl").open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_unified_diff_emits_plus_minus_lines():
    html = _unified_diff_html("alpha\nbeta\n", "alpha\nBETA\n")
    assert 'class="shadow-diff"' in html
    assert 'class="shadow-diff-line shadow-diff-del"' in html
    assert 'class="shadow-diff-line shadow-diff-add"' in html
    assert "-beta" in html
    assert "+BETA" in html
    assert "<pre" not in html


def test_unified_diff_empty_when_no_change():
    html = _unified_diff_html("same\n", "same\n")
    assert "no textual difference" in html


def test_render_shadow_spec_summary_is_structured_not_raw_json():
    html = _render_shadow_spec_summary({
        "op": "replace",
        "field": "instructions",
        "text": "## Preamble\n\n" + ("long prompt " * 80),
    })

    assert 'class="shadow-spec-grid"' in html
    assert "replace" in html
    assert "instructions" in html
    assert "Replacement text" in html
    assert "<pre" not in html


def test_request_diff_falls_back_gracefully_when_no_field_captured():
    link = {"pair_id": "abc", "rule_id": "x"}
    assert _render_shadow_request_diff(link) == ""


def test_request_diff_renders_when_link_has_pre_post():
    link = {
        "field": "instructions",
        "original_field_value": "OLD",
        "mutated_field_value": "NEW",
    }
    html = _render_shadow_request_diff(link)
    assert "instructions" in html
    assert "-OLD" in html
    assert "+NEW" in html


def test_response_diff_falls_back_to_variant_when_no_original():
    link = {}
    v_resp = {"body_text": "hello"}
    html = _render_shadow_response_diff(link, v_resp)
    assert "hello" in html
    assert "not captured" in html


def test_response_diff_renders_when_both_sides_present():
    link = {"original_response_body_text": "alpha\nbeta\n"}
    v_resp = {"body_text": "alpha\nBETA\n"}
    html = _render_shadow_response_diff(link, v_resp)
    assert "-beta" in html
    assert "+BETA" in html


def test_response_diff_hides_raw_audit_drawer_when_no_difference():
    html = _render_shadow_response_diff(
        {"original_response_body_text": "same body\n"},
        {"body_text": "same body\n"},
    )

    assert 'class="shadow-response-compare"' in html
    assert "Line diff" not in html
    assert 'class="shadow-raw-diff-drawer"' not in html


def test_response_diff_summarizes_event_streams_before_raw_diff():
    original = (
        "event: response.created\n"
        'data: {"response":{"id":"resp_orig","status":"completed","model":"gpt-a",'
        '"output_text":"Original answer","usage":{"total_tokens":12}}}\n'
    )
    variant = (
        "event: response.created\n"
        'data: {"response":{"id":"resp_var","status":"completed","model":"gpt-b",'
        '"output_text":"Variant answer","usage":{"total_tokens":18}}}\n'
    )
    html = _render_shadow_response_diff(
        {"original_response_body_text": original},
        {"body_text": variant},
    )

    assert 'class="shadow-response-compare"' in html
    assert 'class="shadow-response-card"' in html
    assert "Original answer" in html
    assert "Variant answer" in html
    assert "gpt-a" in html
    assert "gpt-b" in html
    assert "12 tokens" in html
    assert "18 tokens" in html
    assert "Line diff" in html
    assert 'class="shadow-diff"' in html
    assert "<pre" not in html


def test_response_diff_extracts_streamed_output_text_deltas():
    variant = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"I"}\n'
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":" am ready."}\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed","model":"gpt-x"}}\n'
    )
    html = _render_shadow_response_diff(
        {"original_response_body_text": "event: response.created\ndata: {}\n"},
        {"body_text": variant},
    )

    assert "I am ready." in html
    assert "gpt-x" in html


def test_response_diff_deduplicates_repeated_done_text():
    variant = (
        "event: response.output_text.done\n"
        'data: {"type":"response.output_text.done","text":"Ready."}\n'
        "event: response.output_text.done\n"
        'data: {"type":"response.output_text.done","text":"Ready."}\n'
    )
    html = _render_shadow_response_diff(
        {"original_response_body_text": "event: response.created\ndata: {}\n"},
        {"body_text": variant},
    )

    visible = html.split("<details", 1)[0]
    assert visible.count("Ready.") == 1


def test_collect_shadow_pairs_skips_string_payloads(tmp_path: Path):
    sdir = tmp_path / "sessions" / "s1"
    sdir.mkdir(parents=True)
    rows = [
        {"role": "shadow_link", "payload": "legacy malformed payload"},
        {
            "role": "shadow_link",
            "ts": 10,
            "payload": {
                "pair_id": "p1",
                "rule_id": "r1",
                "original_response_body_text": "alpha",
            },
        },
        {
            "role": "shadow_variant_response",
            "ts": 11,
            "payload": {"pair_id": "p1", "body_text": "alpha"},
        },
    ]
    (sdir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )

    pairs = _collect_shadow_pairs(tmp_path)

    assert [p["pair_id"] for p in pairs] == ["p1"]
    assert pairs[0]["link"]["rule_id"] == "r1"


def test_render_shadows_body_skips_string_payloads(tmp_path: Path):
    sdir = tmp_path / "sessions" / "s1"
    sdir.mkdir(parents=True)
    (sdir / "transcript.jsonl").write_text(
        json.dumps({"role": "shadow_link", "payload": "legacy malformed payload"})
        + "\n"
    )

    html = _render_shadows_body(tmp_path, "tok")

    assert "No shadow pairs captured yet" in html


def test_render_shadows_body_exposes_status_rule_and_sort_controls(tmp_path: Path):
    sdir = tmp_path / "sessions" / "s1"
    sdir.mkdir(parents=True)
    _write_shadow_pair(
        sdir,
        pair_id="older-small",
        ts=10,
        rule_id="rule-alpha",
        original_text="alpha",
        variant_text="alpha",
        original_req_bytes=100,
    )
    _write_shadow_pair(
        sdir,
        pair_id="newer-large",
        ts=20,
        rule_id="rule-beta",
        original_text="beta",
        variant_text="beta changed " * 30,
        original_req_bytes=5000,
        error={"message": "variant failed"},
    )

    html = _render_shadows_body(tmp_path, "tok")

    assert 'class="shadow-ops-bar"' in html
    assert 'name="status"' in html
    assert 'name="rule"' in html
    assert 'name="sort"' in html
    assert "All statuses" in html
    assert "Errors only" in html
    assert "Largest response delta" in html
    assert "rule-alpha" in html
    assert "rule-beta" in html


def test_render_shadows_body_filters_errors_and_sorts_by_delta(tmp_path: Path):
    sdir = tmp_path / "sessions" / "s1"
    sdir.mkdir(parents=True)
    _write_shadow_pair(
        sdir,
        pair_id="ok-pair",
        ts=30,
        rule_id="rule-alpha",
        original_text="same",
        variant_text="same",
    )
    _write_shadow_pair(
        sdir,
        pair_id="err-small",
        ts=20,
        rule_id="rule-beta",
        original_text="short",
        variant_text="changed",
        error={"message": "small failure"},
    )
    _write_shadow_pair(
        sdir,
        pair_id="err-large",
        ts=10,
        rule_id="rule-beta",
        original_text="tiny",
        variant_text="large " * 300,
        error={"message": "large failure"},
    )

    html = _render_shadows_body(
        tmp_path, "tok",
        status_filter="errors",
        rule_filter="rule-beta",
        sort_key="delta",
    )

    assert "ok-pair" not in html
    assert "err-small" in html
    assert "err-large" in html
    assert html.index("err-large") < html.index("err-small")


def test_render_shadows_body_uses_compact_dashboard_layout(tmp_path: Path):
    sdir = tmp_path / "sessions" / "s1"
    sdir.mkdir(parents=True)
    rows = []
    for idx in range(5):
        pair_id = f"p{idx}"
        rows.extend([
            {
                "role": "shadow_link",
                "ts": 10 + idx,
                "payload": {
                    "pair_id": pair_id,
                    "rule_id": "abs-live-default",
                    "original_status": 200,
                    "original_resp_bytes": 1200,
                    "original_req_bytes": 2400,
                    "field": "instructions",
                    "original_field_value": "OLD",
                    "mutated_field_value": "NEW",
                    "original_response_body_text": "same body",
                    "spec": {"op": "replace", "field": "instructions"},
                },
            },
            {
                "role": "shadow_variant_request",
                "ts": 11 + idx,
                "payload": {"pair_id": pair_id, "body": {"instructions": "NEW"}},
            },
            {
                "role": "shadow_variant_response",
                "ts": 12 + idx,
                "payload": {
                    "pair_id": pair_id,
                    "status": 200,
                    "body_text": "same body",
                },
            },
        ])
    (sdir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )

    html = _render_shadows_body(tmp_path, "tok")

    assert 'class="shadow-dashboard"' in html
    assert 'class="shadow-overview"' in html
    assert "Pairs captured" in html
    assert "Rules observed" in html
    assert "Promote-ready" in html
    assert 'class="shadow-readiness-drawer"' in html
    assert 'class="shadow-pair-list"' in html
    assert 'class="shadow-pair-card"' in html
    assert 'class="shadow-pair-summary"' in html
    assert 'class="shadow-pair-body"' in html
    assert 'class="shadow-spec-grid"' in html
    assert 'class="shadow-diff"' in html
    assert "Response diff" in html
