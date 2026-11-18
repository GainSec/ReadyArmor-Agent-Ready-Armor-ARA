"""Rhema request-scope rules must not re-fire on phrases preserved in
conversation history. Strips body.input / body.messages history
(everything except the most-recent user-role entry) before matching.
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")


from pathlib import Path

from ara import rhema as rhema_mod


def _add_substring_deny(state: Path, pattern: str) -> None:
    rhema_mod.upsert_managed(
        state, f"k-{pattern}",
        scope="request", pattern=pattern,
        pattern_kind="substring", action="deny",
        reason="history-strip test",
    )


def test_codex_request_input_history_is_stripped(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    _add_substring_deny(state, "TRIPWIRE")
    payload = {
        "body": {
            "instructions": "system prompt without trip",
            "input": [
                {"role": "user", "content": "earlier message TRIPWIRE here"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "new safe message"},
            ],
        },
    }
    assert rhema_mod.evaluate(state, "request", payload) is None


def test_codex_request_input_latest_user_still_matches(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    _add_substring_deny(state, "TRIPWIRE")
    payload = {
        "body": {
            "instructions": "system prompt",
            "input": [
                {"role": "user", "content": "old safe"},
                {"role": "user", "content": "current: TRIPWIRE!"},
            ],
        },
    }
    rule = rhema_mod.evaluate(state, "request", payload)
    assert rule is not None and "TRIPWIRE" in rule.pattern


def test_openai_messages_history_is_stripped(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    _add_substring_deny(state, "BLOCK_PHRASE")
    payload = {
        "body": {
            "model": "gpt-5",
            "messages": [
                {"role": "user", "content": "old: BLOCK_PHRASE"},
                {"role": "assistant", "content": "thanks"},
                {"role": "user", "content": "current safe message"},
            ],
        },
    }
    assert rhema_mod.evaluate(state, "request", payload) is None


def test_instructions_field_still_searched(short_tmp: Path):
    """Out-of-conversation fields like ``instructions`` aren't stripped
    — they describe the current turn's policy and should still match."""
    state = short_tmp / "state"
    state.mkdir()
    _add_substring_deny(state, "FORBIDDEN_PHRASE")
    payload = {
        "body": {
            "instructions": "please include FORBIDDEN_PHRASE in reply",
            "input": [{"role": "user", "content": "hi"}],
        },
    }
    rule = rhema_mod.evaluate(state, "request", payload)
    assert rule is not None


def test_response_scope_unchanged(short_tmp: Path):
    """Response-scope haystack must NOT be touched — the response body
    is the model's reply for one turn, never has conversation history."""
    state = short_tmp / "state"
    state.mkdir()
    rhema_mod.upsert_managed(
        state, "leak-rule",
        scope="response", pattern="api_key",
        pattern_kind="substring", action="deny",
    )
    rule = rhema_mod.evaluate(
        state, "response",
        {"body_text": "here is your api_key=sk-..."}
    )
    assert rule is not None
