"""Credential-shaped values in these tests are synthetic canary fixtures."""

from __future__ import annotations

from dataclasses import dataclass, field

from ara import temper_ingest, bulwark


@dataclass
class _Item:
    destination: str
    control: dict = field(default_factory=dict)
    title: str = "t"
    proposed_text: str = ""
    citations: list = field(default_factory=list)


def _fires(rule_text: str, event: dict) -> str:
    # strip trailing cite comment, wrap in a scope heading, evaluate
    bw = bulwark.parse("## global\n" + rule_text)
    assert bw.rules, f"no rule parsed from {rule_text!r}"
    return bulwark.evaluate(bw, event).action


def test_bulwark_entry_routes_to_bulwark_and_enforces() -> None:
    plan = temper_ingest.plan_item(_Item(
        "bulwark",
        {"action": "deny", "layer": "exec",
         "match": "jtag|uart|esptool", "exception_path": "op"},
        citations=[{"manifest_element_id": "elem_hw"}],
    ))
    assert plan.home == temper_ingest.BULWARK_HOME
    assert plan.enforced is True
    assert plan.rule_text.startswith("deny exec regex:jtag|uart|esptool")
    assert "# cite: elem_hw" in plan.rule_text
    # actually fires
    assert _fires(plan.rule_text, {"layer": "exec", "path": "esptool", "argv": "x"}) == "deny"


def test_canary_routes_to_reply_deny_and_fires() -> None:
    plan = temper_ingest.plan_item(_Item(
        "canary",
        {"signal": "cred", "pattern": "AKIA[0-9A-Z]{16}", "severity": "high",
         "trip_when": "secret in output", "response": "redact + flag"},
    ))
    assert plan.home == temper_ingest.BULWARK_HOME
    assert plan.layer == "reply" and plan.action == "deny"
    assert plan.enforced is True
    # a response body carrying the secret pattern is blocked
    assert _fires(plan.rule_text,
                  {"layer": "reply", "content": ("key AKIA" + "ABCDEFGHIJKLMNOP leaked")}) == "deny"


def test_absolute_routes_to_absolute_as_advisory() -> None:
    plan = temper_ingest.plan_item(_Item(
        "absolute", proposed_text="Never disclose raw credentials."))
    assert plan.home == temper_ingest.ABSOLUTE_HOME
    assert plan.enforced is False
    assert "Never disclose" in plan.rule_text


def test_rhema_reply_rewrite_is_flagged_not_enforced() -> None:
    # reply rewrite is a real intent but the proxy doesn't apply it to
    # responses — must be flagged so the operator isn't misled.
    plan = temper_ingest.plan_item(_Item(
        "rhema",
        {"action": "rewrite", "layer": "reply", "pattern": "SECRET",
         "rationale": "redact"}))
    assert plan.home == temper_ingest.BULWARK_HOME
    assert plan.enforced is False
    assert "not applied to responses" in plan.note


def test_approval_gate_without_layer_falls_back_to_advisory() -> None:
    plan = temper_ingest.plan_item(_Item(
        "approval_gate",
        {"surface": "hardware", "trigger": "flash write",
         "required_context": "op", "approval_prompt": "Approve flash?"}))
    assert plan.home == temper_ingest.ABSOLUTE_HOME
    assert plan.enforced is False
    assert "layer" in plan.note


def test_approval_gate_with_layer_enforces() -> None:
    plan = temper_ingest.plan_item(_Item(
        "approval_gate",
        {"layer": "fs", "match": "/etc/**", "surface": "fs",
         "trigger": "x", "required_context": "op", "approval_prompt": "?"}))
    assert plan.home == temper_ingest.BULWARK_HOME
    assert plan.enforced is True
    assert _fires(plan.rule_text, {"layer": "fs", "path": "/etc/shadow"}) == "approval"


def test_approval_gate_surface_maps_to_layer_and_enforces() -> None:
    plan = temper_ingest.plan_item(_Item(
        "approval_gate",
        {"surface": "api", "trigger": "/api/system/adb|/system/adb",
         "required_context": "op", "approval_prompt": "Approve?"}))
    assert plan.home == temper_ingest.BULWARK_HOME
    assert plan.layer == "url" and plan.action == "approval"
    assert plan.enforced is True


def test_approval_gate_unmappable_surface_stays_advisory() -> None:
    plan = temper_ingest.plan_item(_Item(
        "approval_gate",
        {"surface": "business logic", "trigger": "x",
         "required_context": "op", "approval_prompt": "?"}))
    assert plan.home == temper_ingest.ABSOLUTE_HOME
    assert plan.enforced is False
