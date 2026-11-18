"""Migration tests: walls.md + overrides.json + rhema.json → bulwark.md."""

from __future__ import annotations

import json
from pathlib import Path

from ara import bulwark as bw
from ara import bulwark_migrate as mig


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_empty_state_root_no_op(tmp_path: Path) -> None:
    r = mig.migrate(tmp_path)
    assert r.skipped
    assert not (tmp_path / "bulwark.md").exists()


def test_walls_migration(tmp_path: Path) -> None:
    walls = """\
## deny_path
/etc/**
## deny_command
curl
## deny_net
*.evil.com:443
## deny_env
*_TOKEN
## approval_path
~/.ssh/**
## allow_exec
ls
"""
    _write(tmp_path / "armors" / "orion" / "WALLS.md", walls)
    r = mig.migrate(tmp_path)
    assert r.wrote_bulwark
    text = (tmp_path / "bulwark.md").read_text()
    assert "## armor: orion" in text
    assert "deny" in text and "/etc/**" in text
    assert "deny     env" in text  # env layer row
    # Re-parse to confirm the synthesised file is valid.
    b = bw.parse(text)
    assert not b.parse_errors, b.parse_errors
    assert len(b.rules) >= 6
    # Legacy file renamed
    assert not (tmp_path / "armors" / "orion" / "WALLS.md").exists()
    assert (tmp_path / "armors" / "orion" / "WALLS.md.bulwark-migrated.bak").exists()


def test_overrides_migration(tmp_path: Path) -> None:
    data = {
        "overrides": [
            {"id": "a1", "kind": "path", "pattern": "/tmp/build/**", "added_at": 0},
        ],
        "blackholes": [
            {"id": "b1", "kind": "command", "pattern": "rm", "added_at": 0},
        ],
    }
    _write(tmp_path / "overrides.json", json.dumps(data))
    r = mig.migrate(tmp_path)
    assert r.wrote_bulwark
    text = (tmp_path / "bulwark.md").read_text()
    assert "## global" in text
    assert "allow    fs   /tmp/build/**" in text
    assert "deny     exec rm" in text


def test_rhema_migration(tmp_path: Path) -> None:
    data = {
        "rules": [
            {"id": "r1", "scope": "request", "pattern": "secret_key",
             "pattern_kind": "substring", "action": "deny"},
            {"id": "r2", "scope": "any", "pattern": r"rm\s+-rf",
             "pattern_kind": "regex", "action": "approval"},
            {"id": "r3", "scope": "request", "pattern": "rewrite me",
             "pattern_kind": "substring", "action": "rewrite",
             "rewrite_with": {"op": "replace", "field": "instructions", "text": "x"}},
        ]
    }
    _write(tmp_path / "rhema.json", json.dumps(data))
    r = mig.migrate(tmp_path)
    assert r.wrote_bulwark
    text = (tmp_path / "bulwark.md").read_text()
    # request → ask layer
    assert "deny     ask  secret_key" in text
    # any → ask + reply + oc
    assert "approval ask  regex:rm\\s+-rf" in text
    assert "approval reply regex:rm\\s+-rf" in text
    assert "approval oc   regex:rm\\s+-rf" in text
    # rewrite action preserved as comment + deny
    assert "# MIGRATION: former rewrite rule" in text
    assert any("rewrite_with" in w or "rewrite" in w for w in r.warnings)


def test_combined_migration(tmp_path: Path) -> None:
    """Walls + per-armor overrides + global overrides + rhema all coalesce."""
    _write(
        tmp_path / "armors" / "orion" / "WALLS.md",
        "## deny_path\n/etc/**\n",
    )
    _write(
        tmp_path / "armors" / "orion" / "overrides.json",
        json.dumps({
            "overrides": [
                {"id": "x", "kind": "path", "pattern": "/etc/orion.conf", "added_at": 0},
            ],
            "blackholes": [],
        }),
    )
    _write(
        tmp_path / "overrides.json",
        json.dumps({
            "overrides": [],
            "blackholes": [
                {"id": "y", "kind": "command", "pattern": "nc", "added_at": 0},
            ],
        }),
    )
    _write(
        tmp_path / "rhema.json",
        json.dumps({"rules": [
            {"id": "z", "scope": "response", "pattern": "classified",
             "pattern_kind": "substring", "action": "deny"},
        ]}),
    )
    r = mig.migrate(tmp_path)
    assert r.wrote_bulwark
    text = (tmp_path / "bulwark.md").read_text()
    # Parse the synthesized output and check rule scopes look right.
    b = bw.parse(text)
    assert not b.parse_errors, b.parse_errors
    global_rules = [r for r in b.rules if r.scope.include_global]
    orion_rules = [r for r in b.rules if "orion" in r.scope.include_armors]
    assert any(r.layer == "reply" for r in global_rules)   # rhema response
    assert any(r.layer == "exec" for r in global_rules)    # global blackhole
    assert any(r.layer == "fs" and r.action == "deny" for r in orion_rules)
    assert any(r.layer == "fs" and r.action == "allow" for r in orion_rules)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    _write(tmp_path / "armors" / "orion" / "WALLS.md", "## deny_path\n/x\n")
    r = mig.migrate(tmp_path, dry_run=True)
    assert not r.wrote_bulwark
    assert (tmp_path / "armors" / "orion" / "WALLS.md").exists()
    assert not (tmp_path / "bulwark.md").exists()
    assert "## armor: orion" in r.bulwark_text


def test_idempotent_when_bulwark_exists(tmp_path: Path) -> None:
    (tmp_path / "bulwark.md").write_text("## global\nallow net *\n")
    _write(tmp_path / "armors" / "a" / "WALLS.md", "## deny_path\n/x\n")
    r = mig.migrate(tmp_path)
    assert r.skipped
    assert (tmp_path / "armors" / "a" / "WALLS.md").exists()


def test_force_overwrites(tmp_path: Path) -> None:
    (tmp_path / "bulwark.md").write_text("## global\nallow net *\n")
    _write(tmp_path / "armors" / "a" / "WALLS.md", "## deny_path\n/x\n")
    r = mig.migrate(tmp_path, force=True)
    assert r.wrote_bulwark
    assert "/x" in (tmp_path / "bulwark.md").read_text()
