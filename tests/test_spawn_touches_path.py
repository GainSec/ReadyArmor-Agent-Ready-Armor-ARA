"""DEPRECATED: covers the legacy walls+overrides+policy evaluator, retired in the Bulwark rollout. Tests preserved as documentation; Bulwark equivalents in test_bulwark.py / test_bulwark_supervisor.py."""

import pytest
pytestmark = pytest.mark.skip(reason='legacy policy evaluator retired — see test_bulwark*.py')

"""Path-centric spawn enforcement: block any spawn whose argv resolves to a
walled path, regardless of which command is doing the touching.

This is the control-in-depth complement to deny_command. WALLS protects the
resource; whatever tool tries to interact with it hits the same wall.
"""

from pathlib import Path

from ara.policy import evaluate
from ara.walls import extract_path_candidates, parse


def test_extract_direct_path_arg(tmp_path: Path):
    c = extract_path_candidates("ls", ["~/"], home=tmp_path)
    assert str(tmp_path) in c


def test_extract_find_flags(tmp_path: Path):
    c = extract_path_candidates(
        "find", ["~/", "-maxdepth", "1", "-not", "-path", "*/.*"], home=tmp_path
    )
    assert str(tmp_path) in c


def test_extract_tree_with_flags(tmp_path: Path):
    c = extract_path_candidates("tree", ["-L", "1", "~/"], home=tmp_path)
    assert str(tmp_path) in c


def test_extract_equals_flag(tmp_path: Path):
    c = extract_path_candidates("ssh", ["--identity-file=~/.ssh/id_rsa"], home=tmp_path)
    assert str(tmp_path / ".ssh/id_rsa") in c


def test_extract_shell_c(tmp_path: Path):
    c = extract_path_candidates("bash", ["-c", "ls ~/"], home=tmp_path)
    assert str(tmp_path) in c


def test_extract_shell_c_with_quotes(tmp_path: Path):
    c = extract_path_candidates(
        "bash", ["-c", "cat '~/.ssh/id_rsa'"], home=tmp_path
    )
    assert str(tmp_path / ".ssh/id_rsa") in c


def test_extract_no_false_positive_for_flags(tmp_path: Path):
    c = extract_path_candidates("node", ["--max-old-space-size=4096"], home=tmp_path)
    assert c == []


def test_extract_no_false_positive_for_prompt_text(tmp_path: Path):
    c = extract_path_candidates(
        "/usr/bin/node",
        ["--message", "Run ls ~/ for me"],
        home=tmp_path,
    )
    # "Run", "ls", "~/", "for", "me" are individual words in the message arg,
    # but the whole arg value "Run ls ~/ for me" doesn't look path-shaped
    # (has spaces, doesn't start with / ~ . -). So no match.
    assert c == []


# Policy-level: spawn whose argv touches a walled path is denied.


def test_ls_home_denied_by_path_wall_only(tmp_path: Path):
    walls = parse(f"## deny_path\n{tmp_path}\n", home=tmp_path)
    d = evaluate(walls, {"op": "exec.spawn", "command": "ls", "args": [str(tmp_path)]})
    assert d.verdict == "deny"
    assert d.reason == "DENY_SPAWN_TOUCHES_BLOCKED_PATH"
    assert d.touched == str(tmp_path)


def test_find_home_denied_by_path_wall(tmp_path: Path):
    walls = parse(f"## deny_path\n{tmp_path}\n", home=tmp_path)
    d = evaluate(
        walls,
        {
            "op": "exec.spawn",
            "command": "find",
            "args": [str(tmp_path), "-maxdepth", "1"],
        },
    )
    assert d.verdict == "deny"
    assert d.reason == "DENY_SPAWN_TOUCHES_BLOCKED_PATH"


def test_tree_home_denied(tmp_path: Path):
    walls = parse(f"## deny_path\n{tmp_path}\n", home=tmp_path)
    d = evaluate(
        walls, {"op": "exec.spawn", "command": "tree", "args": ["-L", "1", str(tmp_path)]}
    )
    assert d.verdict == "deny"


def test_cat_ssh_key_denied(tmp_path: Path):
    walls = parse(f"## deny_path\n{tmp_path}/.ssh/**\n", home=tmp_path)
    d = evaluate(
        walls,
        {
            "op": "exec.spawn",
            "command": "cat",
            "args": [str(tmp_path / ".ssh/id_rsa")],
        },
    )
    assert d.verdict == "deny"
    assert d.reason == "DENY_SPAWN_TOUCHES_BLOCKED_PATH"


def test_bash_c_cat_ssh_key_denied(tmp_path: Path):
    walls = parse(f"## deny_path\n{tmp_path}/.ssh/**\n", home=tmp_path)
    d = evaluate(
        walls,
        {
            "op": "exec.spawn",
            "command": "/bin/bash",
            "args": ["-c", f"cat {tmp_path}/.ssh/id_rsa"],
        },
    )
    assert d.verdict == "deny"


def test_unwalled_spawn_allowed(tmp_path: Path):
    walls = parse(f"## deny_path\n{tmp_path}/.ssh/**\n", home=tmp_path)
    d = evaluate(walls, {"op": "exec.spawn", "command": "echo", "args": ["hi"]})
    assert d.verdict == "allow"


def test_both_layers_still_work(tmp_path: Path):
    """Command wall still fires when no path match but command is banned."""
    walls = parse("## deny_command\ncurl\n", home=tmp_path)
    d = evaluate(
        walls,
        {"op": "exec.spawn", "command": "curl", "args": ["https://example.com"]},
    )
    assert d.verdict == "deny"
    assert d.reason == "DENY_COMMAND_BLOCKED"


def test_path_wall_takes_priority(tmp_path: Path):
    """If both walls would match, path wall wins (it's the more precise one)."""
    walls = parse(
        f"## deny_path\n{tmp_path}/.ssh/**\n## deny_command\ncat\n", home=tmp_path
    )
    d = evaluate(
        walls,
        {
            "op": "exec.spawn",
            "command": "cat",
            "args": [str(tmp_path / ".ssh/id_rsa")],
        },
    )
    assert d.verdict == "deny"
    assert d.reason == "DENY_SPAWN_TOUCHES_BLOCKED_PATH"
