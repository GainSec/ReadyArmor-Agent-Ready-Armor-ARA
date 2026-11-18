"""Tests for the ``exec: allowlist`` posture + allow_exec rules."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="legacy policy evaluator retired — see test_bulwark*.py")


from ara.policy import evaluate
from ara.walls import parse, find_allow_exec_match


def _query(command: str, args=None):
    return {"op": "exec.spawn", "command": command, "args": args or []}


def test_default_posture_allows_unmatched_exec() -> None:
    walls = parse("")
    d = evaluate(walls, _query("/bin/ls"))
    assert d.verdict == "allow"


def test_allowlist_posture_denies_unmatched() -> None:
    walls = parse(
        "## posture\n"
        "exec: allowlist\n"
        "## allow_exec\n"
        "git\n"
    )
    d = evaluate(walls, _query("/usr/bin/curl"))
    assert d.verdict == "deny"
    assert d.reason == "DENY_ALLOWLIST_EXEC"
    assert d.touched == "/usr/bin/curl"


def test_allowlist_posture_allows_matched_basename() -> None:
    walls = parse(
        "## posture\n"
        "exec: allowlist\n"
        "## allow_exec\n"
        "git\n"
        "node\n"
    )
    d = evaluate(walls, _query("/usr/bin/git", ["status"]))
    assert d.verdict == "allow"


def test_allowlist_regex_entry() -> None:
    walls = parse(
        "## posture\n"
        "exec: allowlist\n"
        "## allow_exec\n"
        "regex:^(git|gh|node)$\n"
    )
    assert evaluate(walls, _query("/usr/bin/gh")).verdict == "allow"
    assert evaluate(walls, _query("/usr/bin/rm")).verdict == "deny"


def test_deny_command_still_reports_specific_rule_in_allowlist() -> None:
    """A command on the allow_exec list that also has a deny_command rule
    should surface the explicit deny reason, not the posture fallback."""
    walls = parse(
        "## posture\n"
        "exec: allowlist\n"
        "## deny_command\n"
        "curl\n"
        "## allow_exec\n"
        "curl\n"
    )
    d = evaluate(walls, _query("/usr/bin/curl"))
    assert d.verdict == "deny"
    assert d.reason == "DENY_COMMAND_BLOCKED"
    assert d.rule_id == "deny_command:1"


def test_find_allow_exec_match_helper() -> None:
    walls = parse(
        "## posture\n"
        "exec: allowlist\n"
        "## allow_exec\n"
        "git\n"
    )
    assert find_allow_exec_match(walls, "/usr/bin/git") is not None
    assert find_allow_exec_match(walls, "/usr/bin/curl") is None


def test_posture_parses_whitespace_tolerant() -> None:
    walls = parse(
        "## posture\n"
        "  exec : AllowList  \n"
    )
    assert walls.exec_posture == "allowlist"
