"""Tests for the MITM-side v0.2 pause-and-ask wiring.

The MITM proxy (``ara.mitm_proxy.MitmProxy``) is the hook used when the
agent runtime doesn't emit ``prompt_intercept`` IPC events for outbound
LLM calls (e.g., openclaw 2026.4.x → litellm goes direct). Same-process
state is shared with the supervisor via ``ara.pause_and_ask_state``.

Coverage:
- ``_try_apply_pause_and_ask_inject`` mutates request bodies on
  ``/v1/chat/completions`` (OpenAI/litellm) and ``/v1/messages``
  (Anthropic).
- Forces ``stream=False`` so the response is a single JSON object the
  proxy can parse without SSE assembly.
- Skips for non-LLM paths, non-MITM hosts, and outside weapon context.
- ``_try_consume_pause_and_ask`` parses the agent's classification JSON,
  writes the registry, and clears the pending entry.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ara import armors as armors_mod
from ara import pause_and_ask_state as paas
from ara import tool_registry as tr
from ara import trims as trims_mod
from ara.mitm_proxy import MitmProxy


def _setup_engagement(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement={
        "engagement_id": "eng1", "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": "prod"},
        "posture": "blackbox",
        "active_testing": True,
    })
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)


def _proxy(tmp_path: Path) -> MitmProxy:
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    return MitmProxy(state_root=tmp_path, session_dir=session_dir)


def _queue_pending(state_root: Path, key: str = "nmap | -sV*") -> None:
    paas.queue(state_root, paas.PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key=key,
        classification_prompt="[ARA] classify nmap -sV* — reply JSON…",
        queued_at=time.time(),
    ))


_GOOD_REPLY = (
    '{"active": true, "destructive": false, "intrusive": true, '
    '"scanner": true, "confidence": 3, "reasoning": "service version probe"}'
)


# ---------------------------------------------------------------------------
# Request injection
# ---------------------------------------------------------------------------

def test_inject_openai_chat_completions(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    _queue_pending(tmp_path)
    proxy = _proxy(tmp_path)

    req_body = json.dumps({
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "scan portal"}],
        "stream": True,
    }).encode()
    req_parsed = {"start": "POST /v1/chat/completions HTTP/1.1"}

    out = proxy._try_apply_pause_and_ask_inject(
        host="api.openai.com", req_parsed=req_parsed,
        req_body=req_body, provider="openai",
    )
    assert out is not None
    payload = json.loads(out)
    assert payload["messages"][0]["role"] == "system"
    assert "classify nmap" in payload["messages"][0]["content"]
    assert payload["messages"][1]["role"] == "user"
    # Must force non-streaming so the proxy can parse a single response.
    assert payload["stream"] is False


def test_inject_anthropic_messages(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    _queue_pending(tmp_path)
    proxy = _proxy(tmp_path)

    # Anthropic shape: top-level `system` string + messages with
    # role=user/assistant only.
    req_body = json.dumps({
        "model": "claude-opus-4-7",
        "system": "you are a pen-tester",
        "messages": [{"role": "user", "content": "scan portal"}],
        "stream": True,
    }).encode()
    req_parsed = {"start": "POST /v1/messages HTTP/1.1"}

    out = proxy._try_apply_pause_and_ask_inject(
        host="api.anthropic.com", req_parsed=req_parsed,
        req_body=req_body, provider="anthropic",
    )
    assert out is not None
    payload = json.loads(out)
    # Anthropic injection: prepended into the system field, not messages.
    assert "classify nmap" in payload["system"]
    assert "you are a pen-tester" in payload["system"]
    assert payload["messages"][0]["role"] == "user"
    assert payload["stream"] is False


def test_inject_skips_non_llm_path(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    _queue_pending(tmp_path)
    proxy = _proxy(tmp_path)

    req_body = json.dumps({"messages": []}).encode()
    out = proxy._try_apply_pause_and_ask_inject(
        host="api.openai.com",
        req_parsed={"start": "POST /v1/files HTTP/1.1"},  # not a completion path
        req_body=req_body, provider="openai",
    )
    assert out is None


def test_inject_skips_non_mitm_host(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    _queue_pending(tmp_path)
    proxy = _proxy(tmp_path)

    req_body = json.dumps({"messages": []}).encode()
    out = proxy._try_apply_pause_and_ask_inject(
        host="example.com",  # not a MITM host
        req_parsed={"start": "POST /v1/chat/completions HTTP/1.1"},
        req_body=req_body, provider="unknown",
    )
    assert out is None


def test_inject_skips_outside_weapon_context(tmp_path: Path) -> None:
    # No armor / no trim → not weapon context.
    _queue_pending(tmp_path)
    proxy = _proxy(tmp_path)

    req_body = json.dumps({
        "messages": [{"role": "user", "content": "x"}],
    }).encode()
    out = proxy._try_apply_pause_and_ask_inject(
        host="api.openai.com",
        req_parsed={"start": "POST /v1/chat/completions HTTP/1.1"},
        req_body=req_body, provider="openai",
    )
    assert out is None


def test_inject_skips_when_no_pending(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    proxy = _proxy(tmp_path)

    req_body = json.dumps({
        "messages": [{"role": "user", "content": "x"}],
    }).encode()
    out = proxy._try_apply_pause_and_ask_inject(
        host="api.openai.com",
        req_parsed={"start": "POST /v1/chat/completions HTTP/1.1"},
        req_body=req_body, provider="openai",
    )
    assert out is None


# ---------------------------------------------------------------------------
# Response consume
# ---------------------------------------------------------------------------

def test_consume_openai_response_records_classification(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    paas.queue(tmp_path, paas.PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="p", asked=True, queued_at=time.time(),
    ))
    proxy = _proxy(tmp_path)
    resp_text = json.dumps({
        "choices": [{"message": {"content": _GOOD_REPLY}}],
    })
    proxy._try_consume_pause_and_ask("openai", resp_text)
    assert paas.get_all(tmp_path) == []
    e = tr.lookup(tmp_path, "nmap | -sV*")
    assert e is not None
    assert e.classification.active is True


def test_consume_anthropic_response_records_classification(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    paas.queue(tmp_path, paas.PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="p", asked=True, queued_at=time.time(),
    ))
    proxy = _proxy(tmp_path)
    resp_text = json.dumps({
        "content": [{"type": "text", "text": _GOOD_REPLY}],
    })
    proxy._try_consume_pause_and_ask("anthropic", resp_text)
    assert paas.get_all(tmp_path) == []


def test_consume_unparseable_leaves_pending(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    paas.queue(tmp_path, paas.PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="p", asked=True, queued_at=time.time(),
    ))
    proxy = _proxy(tmp_path)
    proxy._try_consume_pause_and_ask("openai", "not json at all")
    assert any(
        p.classification_key == "nmap | -sV*" for p in paas.get_all(tmp_path)
    )


def test_consume_skips_outside_weapon_context(tmp_path: Path) -> None:
    paas.queue(tmp_path, paas.PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="p", asked=True, queued_at=time.time(),
    ))
    proxy = _proxy(tmp_path)
    resp_text = json.dumps({
        "choices": [{"message": {"content": _GOOD_REPLY}}],
    })
    proxy._try_consume_pause_and_ask("openai", resp_text)
    # No registry write, pending stays.
    assert tr.lookup(tmp_path, "nmap | -sV*") is None
    assert any(
        p.classification_key == "nmap | -sV*" for p in paas.get_all(tmp_path)
    )


# ---------------------------------------------------------------------------
# Round-trip: inject → consume → registry populated
# ---------------------------------------------------------------------------

def test_roundtrip_inject_then_consume(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    _queue_pending(tmp_path)
    proxy = _proxy(tmp_path)

    # Inject.
    req_body = json.dumps({
        "messages": [{"role": "user", "content": "scan portal"}],
        "stream": True,
    }).encode()
    out = proxy._try_apply_pause_and_ask_inject(
        host="api.openai.com",
        req_parsed={"start": "POST /v1/chat/completions HTTP/1.1"},
        req_body=req_body, provider="openai",
    )
    assert out is not None

    # Simulated upstream JSON response (stream forced false).
    resp_text = json.dumps({
        "choices": [{"message": {"content": _GOOD_REPLY}}],
    })
    proxy._try_consume_pause_and_ask("openai", resp_text)

    assert paas.get_all(tmp_path) == []
    e = tr.lookup(tmp_path, "nmap | -sV*")
    assert e is not None
    assert e.classification.active is True
    assert e.confidence == 3
