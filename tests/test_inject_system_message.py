"""inject_system_message must never create two leading system messages
(vLLM/Qwen3 reject: "System message must be at the beginning")."""

from __future__ import annotations

from ara.pause_and_ask_state import inject_system_message


def _roles(p):
    return [m["role"] for m in p["messages"]]


def test_merges_into_existing_leading_system() -> None:
    payload = {"messages": [
        {"role": "system", "content": "you are hermes"},
        {"role": "user", "content": "scan"},
    ]}
    out = inject_system_message(payload, "NOTICE: reboot is out of scope")
    # exactly ONE leading system (merged), no duplicate
    assert _roles(out) == ["system", "user"]
    assert out["messages"][0]["content"] == "NOTICE: reboot is out of scope\n\nyou are hermes"
    # original payload untouched
    assert len(payload["messages"]) == 2


def test_prepends_when_no_leading_system() -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    out = inject_system_message(payload, "sys")
    assert _roles(out) == ["system", "user"]
    assert out["messages"][0]["content"] == "sys"


def test_merges_into_list_content_system() -> None:
    payload = {"messages": [
        {"role": "system", "content": [{"type": "text", "text": "base"}]},
        {"role": "user", "content": "hi"},
    ]}
    out = inject_system_message(payload, "notice")
    assert _roles(out) == ["system", "user"]
    assert out["messages"][0]["content"][0]["text"] == "notice"


def test_bad_shape_returns_none() -> None:
    assert inject_system_message({}, "x") is None
    assert inject_system_message({"messages": "nope"}, "x") is None


def test_never_two_leading_systems() -> None:
    # the exact bug: injecting on top of a conversation that already has a
    # system message must not yield [system, system, ...]
    payload = {"messages": [
        {"role": "system", "content": "orig"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]}
    out = inject_system_message(payload, "inj")
    systems_at_start = 0
    for m in out["messages"]:
        if m["role"] == "system":
            systems_at_start += 1
        else:
            break
    assert systems_at_start == 1
