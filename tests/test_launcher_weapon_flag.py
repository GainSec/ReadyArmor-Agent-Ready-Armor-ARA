"""Tests for the --weapon flag on ara openclaw / ara claude launchers.

The launcher wraps its launch_cli call with equip → run → unequip.
We don't actually launch a real subprocess here — the tests stub
``launch_cli`` (and the worker ``build_command`` helpers) so we can
inspect what equip/unequip did before and after the (mocked) run.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ara import weapons as weapons_mod


def _seal_weapon(state_root: Path, weapon_id: str = "w-launch") -> None:
    wdir = weapons_mod.weapon_dir(state_root, weapon_id)
    pdir = wdir / "policy"
    pdir.mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({"weapon_target_id": weapon_id}))
    (wdir / "seal.json").write_text(json.dumps({"weapon_id": weapon_id}))
    (pdir / "WALLS.md").write_text("## deny_path\n# from weapon\n")


def _stub_openclaw_build(*_a, **_kw):
    return ["/bin/echo", "stubbed"], {"OPENCLAW_STATE_DIR": "/tmp/x"}


def _stub_claude_build(*_a, **_kw):
    return ["/bin/echo", "stubbed"], {"CLAUDE_HOME": "/tmp/y"}


def test_openclaw_with_weapon_flag_equips_then_unequips(
    tmp_path: Path, monkeypatch
) -> None:
    from ara import cli

    _seal_weapon(tmp_path, "w-launch")
    operator_walls = "# operator\n## deny_path\n*.env\n"
    (tmp_path / "WALLS.md").write_text(operator_walls)

    captured: dict = {}

    def fake_launch(*args, **kwargs):
        # Mid-launch: weapon is equipped, overlay is in WALLS.md
        captured["mid_walls"] = (tmp_path / "WALLS.md").read_text()
        captured["mid_equipped"] = weapons_mod.equipped_weapon_id(tmp_path)
        return 0

    monkeypatch.setattr(cli, "launch_cli", fake_launch)
    monkeypatch.setattr(
        "ara.workers.openclaw.build_command", _stub_openclaw_build,
    )

    rc = cli.main([
        "openclaw",
        "--state-root", str(tmp_path),
        "--weapon", "w-launch",
        "--", "noop",
    ])
    assert rc == 0
    # Mid-launch: weapon was equipped, overlay was in WALLS.md
    assert captured["mid_equipped"] == "w-launch"
    assert "from weapon" in captured["mid_walls"]
    assert "*.env" in captured["mid_walls"]  # operator preserved
    # Post-launch: overlay stripped, operator content byte-identical,
    # equipped marker cleared (since none was set before)
    final = (tmp_path / "WALLS.md").read_text()
    assert "from weapon" not in final
    assert final.rstrip("\n") == operator_walls.rstrip("\n")
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_openclaw_without_weapon_flag_runs_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    from ara import cli

    captured = {"call_count": 0}

    def fake_launch(*args, **kwargs):
        captured["call_count"] += 1
        # No weapon equipped at launch time
        captured["equipped"] = weapons_mod.equipped_weapon_id(tmp_path)
        return 0

    monkeypatch.setattr(cli, "launch_cli", fake_launch)
    monkeypatch.setattr(
        "ara.workers.openclaw.build_command", _stub_openclaw_build,
    )

    rc = cli.main([
        "openclaw",
        "--state-root", str(tmp_path),
        "--", "noop",
    ])
    assert rc == 0
    assert captured["call_count"] == 1
    assert captured["equipped"] is None


def test_openclaw_unsealed_weapon_rejected(tmp_path: Path, monkeypatch) -> None:
    from ara import cli

    monkeypatch.setattr(cli, "launch_cli", lambda *a, **kw: 0)
    monkeypatch.setattr(
        "ara.workers.openclaw.build_command", _stub_openclaw_build,
    )

    rc = cli.main([
        "openclaw",
        "--state-root", str(tmp_path),
        "--weapon", "ghost",  # not sealed
        "--", "noop",
    ])
    assert rc == 2
    # No equipped marker should have been set
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_claude_with_weapon_flag_equips_then_unequips(
    tmp_path: Path, monkeypatch
) -> None:
    from ara import cli

    _seal_weapon(tmp_path, "w-claude")
    captured: dict = {}

    def fake_launch(*args, **kwargs):
        captured["mid_equipped"] = weapons_mod.equipped_weapon_id(tmp_path)
        captured["mid_walls"] = (tmp_path / "WALLS.md").read_text()
        return 0

    monkeypatch.setattr(cli, "launch_cli", fake_launch)
    monkeypatch.setattr(
        "ara.workers.claude_code.build_command", _stub_claude_build,
    )

    rc = cli.main([
        "claude",
        "--state-root", str(tmp_path),
        "--weapon", "w-claude",
        "--", "noop",
    ])
    assert rc == 0
    assert captured["mid_equipped"] == "w-claude"
    assert "from weapon" in captured["mid_walls"]
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_unequip_runs_even_when_launch_returns_nonzero(
    tmp_path: Path, monkeypatch
) -> None:
    """If the launched process exits non-zero (or even raises), the
    finally block must still unequip + clear the equipped marker."""
    from ara import cli

    _seal_weapon(tmp_path, "w-fail")

    def fake_launch(*args, **kwargs):
        return 7  # non-zero exit

    monkeypatch.setattr(cli, "launch_cli", fake_launch)
    monkeypatch.setattr(
        "ara.workers.openclaw.build_command", _stub_openclaw_build,
    )

    rc = cli.main([
        "openclaw",
        "--state-root", str(tmp_path),
        "--weapon", "w-fail",
        "--", "noop",
    ])
    assert rc == 7
    # Cleanup happened
    assert weapons_mod.equipped_weapon_id(tmp_path) is None
    final = (tmp_path / "WALLS.md").read_text() if (tmp_path / "WALLS.md").exists() else ""
    assert "from weapon" not in final


def test_unequip_runs_when_launch_raises(tmp_path: Path, monkeypatch) -> None:
    from ara import cli

    _seal_weapon(tmp_path, "w-raise")

    def fake_launch(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "launch_cli", fake_launch)
    monkeypatch.setattr(
        "ara.workers.openclaw.build_command", _stub_openclaw_build,
    )

    with pytest.raises(RuntimeError, match="boom"):
        cli.main([
            "openclaw",
            "--state-root", str(tmp_path),
            "--weapon", "w-raise",
            "--", "noop",
        ])
    # Cleanup still happened despite the raise
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_prior_equipped_weapon_restored_on_unequip(
    tmp_path: Path, monkeypatch
) -> None:
    """If the operator already had weapon 'wA' equipped and launches
    with --weapon wB, post-launch we should be back on wA."""
    from ara import cli

    _seal_weapon(tmp_path, "wA")
    _seal_weapon(tmp_path, "wB")
    # Operator already has wA equipped
    weapons_mod.set_equipped_weapon(tmp_path, "wA")
    weapons_mod.equip_policy(tmp_path, "wA")
    pre_walls = (tmp_path / "WALLS.md").read_text()
    assert "BEGIN wA" in pre_walls

    captured = {}

    def fake_launch(*args, **kwargs):
        captured["mid_equipped"] = weapons_mod.equipped_weapon_id(tmp_path)
        captured["mid_walls"] = (tmp_path / "WALLS.md").read_text()
        return 0

    monkeypatch.setattr(cli, "launch_cli", fake_launch)
    monkeypatch.setattr(
        "ara.workers.openclaw.build_command", _stub_openclaw_build,
    )

    rc = cli.main([
        "openclaw",
        "--state-root", str(tmp_path),
        "--weapon", "wB",
        "--", "noop",
    ])
    assert rc == 0
    # Mid-launch: wB equipped (overlay swapped from wA → wB)
    assert captured["mid_equipped"] == "wB"
    assert "BEGIN wB" in captured["mid_walls"]
    assert "BEGIN wA" not in captured["mid_walls"]
    # Post-launch: equipped marker restored to wA, but the WALLS overlay
    # was unequipped — so file no longer carries wB; wA has to be
    # re-equipped explicitly if the operator wants it back. (The marker
    # restore is for state file pointer consistency; policy overlay
    # restoration mid-flight would be surprising behaviour.)
    assert weapons_mod.equipped_weapon_id(tmp_path) == "wA"
