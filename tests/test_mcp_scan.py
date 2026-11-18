"""MCP-Scan sidecar framework — parsing + ingest unit coverage.

Doesn't exercise the real mcp-scan binary (external dependency);
focuses on the parser + event-routing contract ARA owns.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ara import mcp_scan


def test_is_available_honors_env_override(monkeypatch, tmp_path: Path) -> None:
    fake = tmp_path / "mcp-scan"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(0o755)
    monkeypatch.setenv("ARA_MCP_SCAN", str(fake))
    assert mcp_scan.binary_path() == str(fake)
    assert mcp_scan.is_available() is True


def test_is_available_env_override_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARA_MCP_SCAN", str(tmp_path / "nope"))
    assert mcp_scan.binary_path() is None


def test_parse_jsonl_findings() -> None:
    stdout = (
        '{"severity":"high","category":"tool_poisoning","server":"s1",'
        '"detail":"description field smuggles an instruction override"}\n'
        '{"level":"medium","type":"pii_leak","message":"email leaked"}\n'
        'not-json\n'
        '{"severity":"low","rule":"tool_shadowing","source":"s2"}\n'
    )
    findings = mcp_scan.parse_findings(stdout, source="default")
    assert len(findings) == 3
    assert findings[0].severity == "high"
    assert findings[0].category == "tool_poisoning"
    assert findings[1].category == "pii_leak"
    assert findings[1].severity == "medium"
    assert findings[2].source == "s2"


def test_parse_json_array() -> None:
    stdout = """[
      {"severity":"critical","type":"tool_poisoning","server":"x","message":"bad"},
      {"severity":"info","type":"benign","message":"ok"}
    ]"""
    findings = mcp_scan.parse_findings(stdout, source="default")
    assert len(findings) == 2
    assert findings[0].severity == "critical"


def test_parse_empty_stdout() -> None:
    assert mcp_scan.parse_findings("", source="x") == []
    assert mcp_scan.parse_findings("   \n\n  ", source="x") == []


def test_ingest_findings_routes_events() -> None:
    sink_calls: list[dict] = []
    def sink(ev: dict) -> None:
        sink_calls.append(ev)
    findings = [
        mcp_scan.Finding(severity="high", category="tp",
                         source="s", detail="ouch"),
        mcp_scan.Finding(severity="low",  category="pii",
                         source="t", detail="minor"),
    ]
    count = mcp_scan.ingest_findings(sink, findings)
    assert count == 2
    assert sink_calls[0]["kind"] == "mcp_scan_finding"
    assert sink_calls[0]["severity"] == "high"


def test_run_static_scan_noop_when_not_installed(monkeypatch) -> None:
    monkeypatch.delenv("ARA_MCP_SCAN", raising=False)
    monkeypatch.setattr(mcp_scan, "binary_path", lambda: None)
    assert mcp_scan.run_static_scan(Path("/nonexistent/config.json")) == []


def test_finding_default_source_used_when_not_in_obj() -> None:
    stdout = '{"severity":"low","type":"misc","message":"x"}'
    findings = mcp_scan.parse_findings(stdout, source="fallback-src")
    assert findings[0].source == "fallback-src"
