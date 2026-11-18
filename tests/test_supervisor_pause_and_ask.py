"""Tests for the v0.2 pause-and-ask classification flow.

The supervisor:
1. Registers a pending classification when ``_evaluate_trim_gate`` hits
   ``requires_classification=True``.
2. On the next outbound LLM request, injects a system-role message
   asking the agent to produce the classification JSON.
3. On the next response, parses the JSON reply and writes the registry
   entry; clears the pending state.

These tests exercise the helpers + state machine without booting the
full IPC server. The shared store lives in
``ara.pause_and_ask_state`` keyed by ``state_root``; pytest's per-test
``tmp_path`` gives natural isolation.
"""
from __future__ import annotations

import time
from pathlib import Path

from ara import armors as armors_mod
from ara import pause_and_ask_state as paas
from ara import tool_registry as tr
from ara import trims as trims_mod
from ara.policy import Decision
from ara.supervisor import Supervisor

_PendingClassification = paas.PendingClassification


class _FakeSup:
    """Minimum surface for exercising the pause-and-ask helpers."""
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root

    _evaluate_trim_gate = Supervisor._evaluate_trim_gate
    _maybe_inject_classification_request = (
        Supervisor._maybe_inject_classification_request
    )
    _maybe_consume_classification_response = (
        Supervisor._maybe_consume_classification_response
    )
    _inject_system_message = staticmethod(paas.inject_system_message)
    _extract_response_text = staticmethod(paas.extract_response_text)


def _pending(tmp_path: Path) -> dict:
    """Snapshot of the shared store for this engagement."""
    return {p.classification_key: p for p in paas.get_all(tmp_path)}


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


# ---------------------------------------------------------------------------
# Registration: trim-gate populates pending state
# ---------------------------------------------------------------------------

def test_first_use_registers_pending(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    msg = {
        "op": "oc.tool_call", "tool": "bash",
        "command": "nmap -sV portal.acme.com",
    }
    prior = Decision(verdict="allow", reason="ok",
                     rule_id=None, touched=None)
    out = sup._evaluate_trim_gate(msg, prior)
    assert out is not None
    assert out.verdict == "approval"

    pendings = _pending(tmp_path)
    assert len(pendings) == 1
    pending = next(iter(pendings.values()))
    assert pending.tool == "nmap"
    assert pending.classification_key == "nmap | -sV*"
    assert pending.asked is False
    assert "Before executing" in pending.classification_prompt


def test_already_classified_does_not_register(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    tr.record_classification(
        tmp_path, key="ls | -la*",
        classification=tr.Classification(),
        confidence=3, reasoning="readonly",
    )
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash", "command": "ls -la /tmp"}
    prior = Decision(verdict="allow", reason="ok", rule_id=None, touched=None)
    sup._evaluate_trim_gate(msg, prior)
    assert _pending(tmp_path) == {}


# ---------------------------------------------------------------------------
# Injection: outbound LLM request gets a system message prepended
# ---------------------------------------------------------------------------

def test_inject_prepends_system_message(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="[ARA] classify nmap…",
        queued_at=time.time(),
    ))
    payload = {"messages": [{"role": "user", "content": "do thing"}]}
    new = sup._maybe_inject_classification_request(payload)
    assert new is not None
    assert new is not payload
    assert new["messages"][0]["role"] == "system"
    assert "classify nmap" in new["messages"][0]["content"]
    assert new["messages"][1]["role"] == "user"

    pending = _pending(tmp_path)["nmap | -sV*"]
    assert pending.asked is True


def test_inject_only_one_per_turn(tmp_path: Path) -> None:
    """Multiple pending entries → one injection per turn, others wait."""
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="a", flag_pattern="*", classification_key="a | *",
        classification_prompt="prompt-a", queued_at=1.0,
    ))
    paas.queue(tmp_path, _PendingClassification(
        tool="b", flag_pattern="*", classification_key="b | *",
        classification_prompt="prompt-b", queued_at=2.0,
    ))
    payload = {"messages": [{"role": "user", "content": "x"}]}
    new = sup._maybe_inject_classification_request(payload)
    assert new is not None
    asked = [p for p in _pending(tmp_path).values() if p.asked]
    assert len(asked) == 1


def test_inject_skips_when_no_pending(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    payload = {"messages": [{"role": "user", "content": "x"}]}
    assert sup._maybe_inject_classification_request(payload) is None


def test_inject_skips_when_already_asked(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="x", flag_pattern="*", classification_key="x | *",
        classification_prompt="p", asked=True, queued_at=1.0,
    ))
    payload = {"messages": [{"role": "user", "content": "x"}]}
    assert sup._maybe_inject_classification_request(payload) is None


def test_inject_unknown_payload_shape_returns_none(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="x", flag_pattern="*", classification_key="x | *",
        classification_prompt="p", queued_at=1.0,
    ))
    payload = {"prompt": "hi"}
    assert sup._maybe_inject_classification_request(payload) is None


def test_inject_skips_outside_weapon_context(tmp_path: Path) -> None:
    """No active trim → no injection even if a pending entry leaked."""
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="x", flag_pattern="*", classification_key="x | *",
        classification_prompt="p", queued_at=1.0,
    ))
    payload = {"messages": [{"role": "user", "content": "x"}]}
    assert sup._maybe_inject_classification_request(payload) is None


# ---------------------------------------------------------------------------
# Consumption: parse classification reply + write registry
# ---------------------------------------------------------------------------

_GOOD_REPLY = (
    '{"active": true, "destructive": false, "intrusive": true, '
    '"scanner": true, "confidence": 3, "reasoning": "service version probe"}'
)


def test_consume_parses_and_records(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="[ARA] classify…",
        asked=True, queued_at=time.time(),
    ))
    payload = {"content": _GOOD_REPLY}
    sup._maybe_consume_classification_response(payload)
    assert _pending(tmp_path) == {}
    e = tr.lookup(tmp_path, "nmap | -sV*")
    assert e is not None
    assert e.classification.active is True
    assert e.confidence == 3


def test_consume_handles_choices_shape(tmp_path: Path) -> None:
    """OpenAI-compatible response shape."""
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="p", asked=True, queued_at=1.0,
    ))
    payload = {"choices": [{"message": {"content": _GOOD_REPLY}}]}
    sup._maybe_consume_classification_response(payload)
    assert _pending(tmp_path) == {}


def test_consume_handles_content_array(tmp_path: Path) -> None:
    """Anthropic-compatible: {content: [{type, text}, ...]}."""
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="p", asked=True, queued_at=1.0,
    ))
    payload = {"content": [{"type": "text", "text": _GOOD_REPLY}]}
    sup._maybe_consume_classification_response(payload)
    assert _pending(tmp_path) == {}


def test_consume_unparseable_leaves_pending(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="nmap", flag_pattern="-sV*",
        classification_key="nmap | -sV*",
        classification_prompt="p", asked=True, queued_at=1.0,
    ))
    payload = {"content": "I don't have enough info"}
    sup._maybe_consume_classification_response(payload)
    assert "nmap | -sV*" in _pending(tmp_path)


def test_consume_oldest_pending_wins(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="older", flag_pattern="*", classification_key="older | *",
        classification_prompt="p", asked=True, queued_at=1.0,
    ))
    paas.queue(tmp_path, _PendingClassification(
        tool="newer", flag_pattern="*", classification_key="newer | *",
        classification_prompt="p", asked=True, queued_at=2.0,
    ))
    payload = {"content": _GOOD_REPLY}
    sup._maybe_consume_classification_response(payload)
    pendings = _pending(tmp_path)
    assert "older | *" not in pendings
    assert "newer | *" in pendings


def test_consume_no_op_with_no_asked_pending(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    paas.queue(tmp_path, _PendingClassification(
        tool="x", flag_pattern="*", classification_key="x | *",
        classification_prompt="p", asked=False, queued_at=1.0,
    ))
    payload = {"content": _GOOD_REPLY}
    sup._maybe_consume_classification_response(payload)
    assert "x | *" in _pending(tmp_path)


# ---------------------------------------------------------------------------
# Full round-trip
# ---------------------------------------------------------------------------

def test_full_round_trip_register_inject_consume(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "nmap -sV portal.acme.com"}
    prior = Decision(verdict="allow", reason="ok", rule_id=None, touched=None)

    out1 = sup._evaluate_trim_gate(msg, prior)
    assert out1 is not None and out1.verdict == "approval"
    assert "nmap | -sV*" in _pending(tmp_path)

    req = {"messages": [{"role": "user", "content": "scan portal"}]}
    req2 = sup._maybe_inject_classification_request(req)
    assert req2 is not None
    assert any(
        m["role"] == "system" and "Before executing" in m["content"]
        for m in req2["messages"]
    )

    resp = {"content": _GOOD_REPLY}
    sup._maybe_consume_classification_response(resp)
    assert _pending(tmp_path) == {}
    assert tr.lookup(tmp_path, "nmap | -sV*") is not None

    # Round 2: registry entry now present, gate concurs.
    out2 = sup._evaluate_trim_gate(msg, prior)
    # active scan on prod active_testing=true, no destructive flag → allow.
    assert out2 is None or out2.verdict != "approval"
