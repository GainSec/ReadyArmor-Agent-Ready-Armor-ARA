"""Audit #24 — second-runtime adapter for Claude Code. Smoke-tests the
shape of the adapter; mirrors tests/test_workers_openclaw.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from ara.workers import claude_code as cc


def test_state_dir_default_profile():
    assert cc.state_dir_for("").name == ".claude-ara-default"
    assert cc.state_dir_for("demo").name == ".claude-ara-demo"


def test_env_contains_expected_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = cc.env_for("poc")
    assert "CLAUDE_HOME" in env
    assert "CLAUDE_CONFIG_PATH" in env
    assert env["CLAUDE_HOME"].endswith(".claude-ara-poc")
    assert env["CLAUDE_CONFIG_PATH"].endswith(".claude-ara-poc/claude.json")


def test_contained_env_skips_state_mkdir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = cc.env_for("contained-poc", contained=True)
    assert not (tmp_path / ".claude-ara-contained-poc").exists()
    assert env["CLAUDE_HOME"].endswith(".claude-ara-contained-poc")


def test_non_contained_env_creates_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cc.env_for("non-contained-poc")
    assert (tmp_path / ".claude-ara-non-contained-poc").is_dir()


def test_resolve_binary_raises_when_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(RuntimeError):
        cc.resolve_binary()


def test_resolve_binary_respects_override():
    assert cc.resolve_binary("/opt/claude/bin/claude") == "/opt/claude/bin/claude"
