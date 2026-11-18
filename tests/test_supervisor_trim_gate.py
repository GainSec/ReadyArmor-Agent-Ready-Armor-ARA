"""Tests for the v0.1 supervisor patch — `_extract_tool_for_classification`
helper and `Supervisor._evaluate_trim_gate` method.

The supervisor wires `_evaluate_trim_gate` into `_on_query` *between*
bulwark and the approval flow. These tests exercise the helper +
method shape without needing a full supervisor IPC harness.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ara import armors as armors_mod
from ara import trims as trims_mod
from ara import tool_registry as tr
from ara.policy import Decision
from ara.runtime import trim_gate as tg
from ara.supervisor import _extract_tool_for_classification


# ---------------------------------------------------------------------------
# _extract_tool_for_classification (pure helper)
# ---------------------------------------------------------------------------

def test_extract_oc_bash_inner_tool() -> None:
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "nmap -sV -p 80,443 portal.acme.com"}
    tool, args = _extract_tool_for_classification(msg)
    assert tool == "nmap"
    assert args == ["-sV", "-p", "80,443", "portal.acme.com"]


def test_extract_oc_bash_strips_path_and_exe() -> None:
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "/usr/bin/nmap.exe -sV"}
    tool, _ = _extract_tool_for_classification(msg)
    assert tool == "nmap"


def test_extract_oc_bash_with_quoting() -> None:
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": 'curl -A "test agent" https://x'}
    tool, args = _extract_tool_for_classification(msg)
    assert tool == "curl"
    assert "test agent" in args  # shlex preserves quoted phrase


def test_extract_oc_bash_unclosed_quote_falls_back() -> None:
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "sh -c 'unclosed"}
    tool, args = _extract_tool_for_classification(msg)
    # Falls through to bash with empty args rather than crashing.
    assert tool == "bash"
    assert args == []


def test_extract_oc_other_uses_op_as_tool() -> None:
    msg = {"op": "oc.tool_call", "tool": "write",
           "args": ["/tmp/foo", "content"]}
    tool, args = _extract_tool_for_classification(msg)
    assert tool == "write"
    assert args == ["/tmp/foo", "content"]


def test_extract_skips_fs_net_layers() -> None:
    for op in ["fs.read", "fs.write", "net.connect", "perm.exec"]:
        tool, args = _extract_tool_for_classification({"op": op})
        assert tool == "" and args == []


# ---------------------------------------------------------------------------
# exec.spawn — the ground-truth path that fires for every agent shell call
# ---------------------------------------------------------------------------

def test_extract_exec_spawn_bash_dash_c_recurses_to_inner_tool() -> None:
    """openclaw 2026.4.x bash-tool runtime spawns `/bin/bash -c "<cmd>"`;
    classification needs to see the inner tool, not bash."""
    msg = {
        "op": "exec.spawn", "command": "/bin/bash",
        "args": ["-c", "nmap -sV portal.acme.com"],
    }
    tool, args = _extract_tool_for_classification(msg)
    assert tool == "nmap"
    assert args == ["-sV", "portal.acme.com"]


def test_extract_exec_spawn_other_shells_also_recurse() -> None:
    for shell in ("/bin/sh", "/usr/bin/dash", "/usr/bin/zsh"):
        msg = {"op": "exec.spawn", "command": shell,
               "args": ["-c", "ls -la /tmp"]}
        tool, _ = _extract_tool_for_classification(msg)
        assert tool == "ls", f"shell={shell} failed"


def test_extract_exec_spawn_direct_command_NOT_classified() -> None:
    """Infrastructure spawns (codex, node, watchdog helpers, ARA's own
    subprocesses) must NOT trip the classification gate — only bash -c
    invocations from the agent's bash-tool runtime do. Otherwise the
    very first node startup hits the conservative-wiring approval gate
    and openclaw never boots."""
    for cmd, args in [
        ("/usr/bin/node", ["index.js"]),
        ("codex", ["app-server", "--listen", "stdio://"]),
        ("/usr/bin/sqlmap", ["-u", "https://x", "--batch"]),  # direct, not via bash
    ]:
        msg = {"op": "exec.spawn", "command": cmd, "args": args}
        tool, parsed_args = _extract_tool_for_classification(msg)
        assert tool == "" and parsed_args == [], \
            f"direct exec of {cmd!r} should skip classification"


def test_extract_exec_spawn_handles_unclosed_quote_in_inner() -> None:
    """Unclosed-quote inner falls back to the shell name in
    `_parse_bash_command`, which equals `base` — and exec.spawn skips
    classification when the inner == base (treats it as plain shell
    infra). End result: no classification triggered, tool="".
    """
    msg = {"op": "exec.spawn", "command": "/bin/bash",
           "args": ["-c", "echo 'unclosed"]}
    tool, _ = _extract_tool_for_classification(msg)
    assert tool == ""


def test_extract_exec_spawn_empty_command() -> None:
    msg = {"op": "exec.spawn", "command": "", "args": []}
    tool, _ = _extract_tool_for_classification(msg)
    assert tool == ""


def test_extract_exec_spawn_bash_minus_c_with_pipeline() -> None:
    """The first command in a pipeline is the classification subject."""
    msg = {"op": "exec.spawn", "command": "/bin/bash",
           "args": ["-c", "ls -la /tmp | head -5"]}
    tool, args = _extract_tool_for_classification(msg)
    assert tool == "ls"
    # shlex preserves the rest including the pipe
    assert "-la" in args


def test_extract_handles_missing_command() -> None:
    msg = {"op": "oc.tool_call", "tool": "bash"}
    tool, args = _extract_tool_for_classification(msg)
    assert tool == "bash" and args == []


# ---------------------------------------------------------------------------
# _evaluate_trim_gate (method on Supervisor) — exercise via fake supervisor
# ---------------------------------------------------------------------------

class _FakeSup:
    """Minimum surface to call `_evaluate_trim_gate` without the IPC stack."""
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root

    # Bind the real method
    from ara.supervisor import Supervisor as _Sup
    _evaluate_trim_gate = _Sup._evaluate_trim_gate


def _allow_decision() -> Decision:
    return Decision(verdict="allow", reason="bulwark allow",
                    rule_id=None, touched=None)


def test_gate_passes_through_when_no_armor(tmp_path: Path) -> None:
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash", "command": "ls /tmp"}
    out = sup._evaluate_trim_gate(msg, _allow_decision())
    assert out is None


def test_gate_passes_through_when_no_trim_applied(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash", "command": "ls /tmp"}
    out = sup._evaluate_trim_gate(msg, _allow_decision())
    assert out is None


def _setup_engagement(state: Path, *, env: str, active: bool) -> None:
    armors_mod.create_armor(state, "blacksmith")
    armors_mod.set_active_armor(state, "blacksmith")
    meta = trims_mod.derive_trim(state, "blacksmith", engagement={
        "engagement_id": "eng1",
        "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": env},
        "posture": "blackbox",
        "active_testing": active,
    })
    trims_mod.apply_trim(state, "blacksmith", meta.trim_id)


def test_gate_first_use_promotes_to_approval(tmp_path: Path) -> None:
    _setup_engagement(tmp_path, env="nonprod", active=True)
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "nmap -sV portal.acme.com"}
    out = sup._evaluate_trim_gate(msg, _allow_decision())
    assert out is not None
    assert out.verdict == "approval"
    assert "classify" in out.reason.lower()
    assert out.rule_id == "tool_registry:first_use"


def test_gate_classified_active_on_passive_engagement_denies(
    tmp_path: Path,
) -> None:
    _setup_engagement(tmp_path, env="nonprod", active=False)  # passive
    # Pre-register nmap as active.
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="active scan",
    )
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "nmap -sV portal.acme.com"}
    out = sup._evaluate_trim_gate(msg, _allow_decision())
    assert out is not None
    assert out.verdict == "deny"
    assert "TRIM_GATE" in out.reason


def test_gate_destructive_on_prod_promotes_to_token_approval(
    tmp_path: Path,
) -> None:
    _setup_engagement(tmp_path, env="prod", active=True)
    tr.record_classification(
        tmp_path, key="rm | -rf*",
        classification=tr.Classification(active=False, destructive=True),
        confidence=3, reasoning="recursive deletion",
    )
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "rm -rf /tmp/foo"}
    out = sup._evaluate_trim_gate(msg, _allow_decision())
    assert out is not None
    assert out.verdict == "approval"
    assert "PROD-DESTRUCTIVE-APPROVAL" in (out.reason + (out.approval_pattern or ""))


def test_gate_passes_when_classification_within_contract(
    tmp_path: Path,
) -> None:
    _setup_engagement(tmp_path, env="nonprod", active=True)
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True, intrusive=True),
        confidence=3, reasoning="active scan",
    )
    sup = _FakeSup(tmp_path)
    msg = {"op": "oc.tool_call", "tool": "bash",
           "command": "nmap -sV portal.acme.com"}
    out = sup._evaluate_trim_gate(msg, _allow_decision())
    # Contract permits → trim gate concurs → returns None (keep prior allow).
    assert out is None


def test_gate_never_softens_a_bulwark_deny(tmp_path: Path) -> None:
    """Even if the trim gate would otherwise allow, a bulwark deny stands."""
    _setup_engagement(tmp_path, env="nonprod", active=True)
    sup = _FakeSup(tmp_path)
    deny = Decision(verdict="deny", reason="bulwark walls deny",
                    rule_id="walls.fs.deny", touched="/etc/passwd")
    msg = {"op": "oc.tool_call", "tool": "bash", "command": "cat /etc/passwd"}
    out = sup._evaluate_trim_gate(msg, deny)
    assert out is None  # gate refuses to override deny


def test_gate_skips_fs_layers(tmp_path: Path) -> None:
    """fs.read / net.connect / etc. don't go through tool-classification."""
    _setup_engagement(tmp_path, env="prod", active=True)
    sup = _FakeSup(tmp_path)
    msg = {"op": "fs.read", "path": "/etc/hostname"}
    out = sup._evaluate_trim_gate(msg, _allow_decision())
    # No tool extracted → gate passes through (no requires_classification trigger).
    # Bulwark already covers fs allows/denies.
    assert out is None
