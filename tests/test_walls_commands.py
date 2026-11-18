"""DEPRECATED: covers the legacy walls+overrides+policy evaluator, retired in the Bulwark rollout. Tests preserved as documentation; Bulwark equivalents in test_bulwark.py / test_bulwark_supervisor.py."""

import pytest
pytestmark = pytest.mark.skip(reason='legacy policy evaluator retired — see test_bulwark*.py')

from pathlib import Path

from ara.policy import evaluate
from ara.walls import find_command_match, parse


def test_parses_deny_command_section(tmp_path: Path):
    walls = parse(
        "## deny_command\nls\nnode\n", home=tmp_path
    )
    assert [r.pattern for r in walls.command_rules] == ["ls", "node"]
    assert [r.rule_id for r in walls.command_rules] == [
        "deny_command:1",
        "deny_command:2",
    ]


def test_find_command_match_direct_exec(tmp_path: Path):
    walls = parse("## deny_command\nls\n", home=tmp_path)
    assert find_command_match(walls, "ls", []) is not None
    assert find_command_match(walls, "ls", ["-la"]).rule_id == "deny_command:1"
    assert find_command_match(walls, "/usr/bin/ls", []).rule_id == "deny_command:1"


def test_find_command_match_no_false_positive_on_prompt_text(tmp_path: Path):
    """Tokens in user-supplied args (e.g., prompts) must NOT match command rules."""
    walls = parse("## deny_command\nls\n", home=tmp_path)
    # argv[0] is node; message happens to contain 'ls'
    result = find_command_match(
        walls, "/usr/bin/node", ["--message", "Run ls ~/ for me"]
    )
    assert result is None


def test_find_command_match_no_hit(tmp_path: Path):
    walls = parse("## deny_command\nls\n", home=tmp_path)
    assert find_command_match(walls, "cat", ["/tmp/x"]) is None


def test_evaluate_exec_spawn_deny(tmp_path: Path):
    walls = parse("## deny_command\nls\n", home=tmp_path)
    d = evaluate(walls, {"op": "exec.spawn", "command": "ls", "args": ["~/"]})
    assert d.verdict == "deny"
    assert d.reason == "DENY_COMMAND_BLOCKED"
    assert d.rule_id == "deny_command:1"


def test_evaluate_exec_spawn_allow(tmp_path: Path):
    walls = parse("## deny_command\nls\n", home=tmp_path)
    d = evaluate(walls, {"op": "exec.spawn", "command": "cat", "args": ["/tmp/x"]})
    assert d.verdict == "allow"


def test_evaluate_exec_spawn_prompt_text_safe(tmp_path: Path):
    walls = parse("## deny_command\nls\n", home=tmp_path)
    d = evaluate(
        walls,
        {
            "op": "exec.spawn",
            "command": "/usr/bin/node",
            "args": ["--message", "Run ls ~/ for me"],
        },
    )
    assert d.verdict == "allow"


def test_bash_c_wrapper_is_introspected(tmp_path: Path):
    walls = parse("## deny_command\nls\n", home=tmp_path)
    d = evaluate(
        walls,
        {"op": "exec.spawn", "command": "/bin/bash", "args": ["-c", "ls ~/"]},
    )
    assert d.verdict == "deny"
    assert d.rule_id == "deny_command:1"


def test_sh_c_wrapper_is_introspected(tmp_path: Path):
    walls = parse("## deny_command\nls\n", home=tmp_path)
    d = evaluate(walls, {"op": "exec.spawn", "command": "sh", "args": ["-c", "ls"]})
    assert d.verdict == "deny"
