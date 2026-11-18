"""Tests for runtime.channels.base + registry."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ara.audit import verify_chain
from ara.runtime import audit as raudit
from ara.runtime.channels.base import (
    Channel,
    ChannelContext,
    ChannelRejection,
)
from ara.runtime.channels import registry


@dataclass
class _FactBody:
    statement: str
    confidence: float = 0.9


class _FactAssertChannel(Channel):
    name = "fact.assert"
    schema = _FactBody
    sink_subsystem = "facts"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        self.calls.append(body)
        return {"fact_id": f"f_{len(self.calls):04d}"}


class _OperatorOnlyChannel(Channel):
    name = "test.operator_only"
    schema = _FactBody
    sink_subsystem = "facts"
    allowed_callers = ("operator",)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        return {"ok": True}


def setup_function(_fn) -> None:
    registry.unregister_for_tests()
    raudit.reset_registry_for_tests()


def test_validate_accepts_full_body() -> None:
    ch = _FactAssertChannel()
    ch.validate({"statement": "x", "confidence": 0.5})


def test_validate_accepts_default_field_missing() -> None:
    ch = _FactAssertChannel()
    ch.validate({"statement": "x"})


def test_validate_rejects_missing_required() -> None:
    ch = _FactAssertChannel()
    with pytest.raises(ChannelRejection, match="missing required field"):
        ch.validate({"confidence": 0.5})


def test_validate_rejects_unknown_field() -> None:
    ch = _FactAssertChannel()
    with pytest.raises(ChannelRejection, match="unknown fields"):
        ch.validate({"statement": "x", "bogus": 1})


def test_call_executes_and_returns_result(tmp_path: Path) -> None:
    ch = _FactAssertChannel()
    ctx = ChannelContext(
        engagement_id="e1", activation_id="a1", caller="agent",
        audit_sink=tmp_path / "events.jsonl",
    )
    out = ch.call({"statement": "the sky is blue"}, ctx)
    assert out == {"fact_id": "f_0001"}
    assert ch.calls == [{"statement": "the sky is blue"}]


def test_call_audits_ok(tmp_path: Path) -> None:
    ch = _FactAssertChannel()
    sink = tmp_path / "events.jsonl"
    ctx = ChannelContext(engagement_id="e1", activation_id="a1",
                         caller="agent", audit_sink=sink)
    ch.call({"statement": "x"}, ctx)
    line = json.loads(sink.read_text().splitlines()[0])
    assert line["kind"] == "channel.fact.assert"
    body = line["body"]
    assert body["channel"] == "fact.assert"
    assert body["status"] == "ok"
    assert body["body_hash"].startswith("sha256:")
    assert body["result_hash"].startswith("sha256:")
    assert body["ctx"]["activation_id"] == "a1"


def test_call_audits_rejection(tmp_path: Path) -> None:
    ch = _FactAssertChannel()
    sink = tmp_path / "events.jsonl"
    ctx = ChannelContext(caller="agent", audit_sink=sink)
    with pytest.raises(ChannelRejection):
        ch.call({"confidence": 0.1}, ctx)
    line = json.loads(sink.read_text().splitlines()[0])
    assert line["body"]["status"] == "rejected"
    assert "missing required field" in line["body"]["reason"]
    # Result hash absent on rejection
    assert "result_hash" not in line["body"]


def test_caller_restriction(tmp_path: Path) -> None:
    ch = _OperatorOnlyChannel()
    sink = tmp_path / "events.jsonl"
    ctx = ChannelContext(caller="agent", audit_sink=sink)
    with pytest.raises(ChannelRejection, match="not allowed"):
        ch.call({"statement": "x"}, ctx)
    # Operator passes
    ctx.caller = "operator"
    assert ch.call({"statement": "x"}, ctx) == {"ok": True}


def test_audit_chain_well_formed(tmp_path: Path) -> None:
    ch = _FactAssertChannel()
    sink = tmp_path / "events.jsonl"
    ctx = ChannelContext(caller="agent", audit_sink=sink)
    for i in range(5):
        ch.call({"statement": f"f{i}"}, ctx)
    ok, last, err = verify_chain(sink)
    assert ok, err
    assert last == 5


def test_no_audit_when_sink_unset() -> None:
    ch = _FactAssertChannel()
    ctx = ChannelContext(caller="agent", audit_sink=None)
    out = ch.call({"statement": "x"}, ctx)
    assert out["fact_id"] == "f_0001"  # call still works


def test_registry_register_and_get() -> None:
    ch = _FactAssertChannel()
    registry.register(ch)
    assert registry.get("fact.assert") is ch
    assert ch in registry.all_channels()


def test_registry_register_idempotent_for_same_instance() -> None:
    ch = _FactAssertChannel()
    registry.register(ch)
    registry.register(ch)  # same instance — no error
    assert len(registry.all_channels()) == 1


def test_registry_rejects_duplicate_distinct_instances() -> None:
    registry.register(_FactAssertChannel())
    with pytest.raises(registry.DuplicateChannelError):
        registry.register(_FactAssertChannel())


def test_registry_get_unknown_raises() -> None:
    with pytest.raises(KeyError, match="no channel registered"):
        registry.get("nope.never")


def test_all_channels_sorted_by_name() -> None:
    registry.register(_OperatorOnlyChannel())
    registry.register(_FactAssertChannel())
    names = [c.name for c in registry.all_channels()]
    assert names == sorted(names)
