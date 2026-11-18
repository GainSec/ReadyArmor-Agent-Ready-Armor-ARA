"""Tests for Rhema shadow-rewrite specs (A/B plumbing, offline unit-only)."""

from __future__ import annotations

from ara.rhema import apply_rewrite_spec, new_pair_id


def test_replace_field() -> None:
    body = {"instructions": "old big prompt", "other": 1}
    out = apply_rewrite_spec(body, {"op": "replace", "field": "instructions", "text": "new"})
    assert out["instructions"] == "new"
    assert out["other"] == 1
    # Input is not mutated.
    assert body["instructions"] == "old big prompt"


def test_trim_to_chars() -> None:
    body = {"instructions": "x" * 500}
    out = apply_rewrite_spec(body, {"op": "trim_to_chars", "field": "instructions", "chars": 100})
    assert len(out["instructions"]) == 100


def test_regex_sub() -> None:
    body = {"instructions": "Hello WORLD, hello again."}
    out = apply_rewrite_spec(body, {
        "op": "regex_sub", "field": "instructions",
        "pattern": r"(?i)hello", "replacement": "hi",
    })
    assert out["instructions"] == "hi WORLD, hi again."


def test_remove_field() -> None:
    body = {"instructions": "keep", "drop_me": "yes"}
    out = apply_rewrite_spec(body, {"op": "remove_field", "field": "drop_me"})
    assert "drop_me" not in out
    assert out["instructions"] == "keep"


def test_apply_envelope_replaces_codex_instructions() -> None:
    body = {"instructions": "old prompt", "input": "hello"}
    out = apply_rewrite_spec(body, {"op": "apply_envelope", "text": "ABS"})
    assert out["instructions"] == "ABS"
    assert out["input"] == "hello"
    assert body["instructions"] == "old prompt"


def test_apply_envelope_replaces_existing_system_message() -> None:
    body = {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": "Hermes persona"},
            {"role": "user", "content": "hi"},
        ],
    }
    out = apply_rewrite_spec(body, {"op": "apply_envelope", "text": "ABS"})
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"] == "ABS"
    assert "Hermes persona" not in out["messages"][0]["content"]
    assert out["messages"][1]["content"] == "hi"
    assert body["messages"][0]["content"] == "Hermes persona"


def test_apply_envelope_inserts_system_message_when_missing() -> None:
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = apply_rewrite_spec(body, {"op": "apply_envelope", "text": "ABS"})
    assert out["messages"][0] == {"role": "system", "content": "ABS"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_unknown_op_is_noop() -> None:
    body = {"instructions": "x"}
    out = apply_rewrite_spec(body, {"op": "frobnicate", "field": "instructions"})
    assert out == body


def test_missing_field_is_noop() -> None:
    body = {"instructions": "x"}
    out = apply_rewrite_spec(body, {"op": "replace", "field": "nope", "text": "y"})
    assert out == body


def test_malformed_regex_is_noop() -> None:
    body = {"instructions": "abc"}
    out = apply_rewrite_spec(body, {
        "op": "regex_sub", "field": "instructions",
        "pattern": "(", "replacement": "z",
    })
    assert out["instructions"] == "abc"


def test_non_dict_body_returns_dict() -> None:
    # Defensive: if something weird is passed, don't blow up.
    assert apply_rewrite_spec([], {"op": "replace"}) == {}  # type: ignore[arg-type]
    assert apply_rewrite_spec({"a": 1}, None) == {"a": 1}  # type: ignore[arg-type]


def test_pair_id_shape() -> None:
    pid = new_pair_id()
    assert isinstance(pid, str)
    assert len(pid) == 12
    assert pid != new_pair_id()
