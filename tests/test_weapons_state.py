"""Tests for ara.weapons (equipped weapon state) + CLI."""
from __future__ import annotations

import json
from pathlib import Path

from ara import weapons as weapons_mod


def test_no_weapon_equipped_by_default(tmp_path: Path) -> None:
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_set_equipped_weapon_round_trip(tmp_path: Path) -> None:
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    assert weapons_mod.equipped_weapon_id(tmp_path) == "wstg-v1"
    weapons_mod.set_equipped_weapon(tmp_path, None)
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_set_equipped_strips_whitespace(tmp_path: Path) -> None:
    weapons_mod.set_equipped_weapon(tmp_path, "  wstg-v1  ")
    assert weapons_mod.equipped_weapon_id(tmp_path) == "wstg-v1"


def test_weapon_exists_requires_seal_json(tmp_path: Path) -> None:
    assert not weapons_mod.weapon_exists(tmp_path, "x")
    weapon_dir = weapons_mod.weapon_dir(tmp_path, "x")
    weapon_dir.mkdir(parents=True)
    assert not weapons_mod.weapon_exists(tmp_path, "x")
    (weapon_dir / "seal.json").write_text("{}")
    assert weapons_mod.weapon_exists(tmp_path, "x")


def test_list_sealed_weapons_filters_unsealed(tmp_path: Path) -> None:
    base = tmp_path / "weapons"
    base.mkdir()
    (base / "sealed-a").mkdir()
    (base / "sealed-a" / "seal.json").write_text("{}")
    (base / "sealed-b").mkdir()
    (base / "sealed-b" / "seal.json").write_text("{}")
    (base / "draft-c").mkdir()  # no seal.json
    assert weapons_mod.list_sealed_weapons(tmp_path) == ["sealed-a", "sealed-b"]


def test_cli_equip_unequip_current(tmp_path: Path, capsys) -> None:
    from ara.cli import main

    # Seed a sealed weapon
    weapon_dir = weapons_mod.weapon_dir(tmp_path, "wstg-v1")
    weapon_dir.mkdir(parents=True)
    (weapon_dir / "seal.json").write_text("{}")

    # current — none
    main(["weapon", "--state-root", str(tmp_path), "current"])
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"equipped_weapon": None}

    # equip
    rc = main(["weapon", "--state-root", str(tmp_path), "equip", "wstg-v1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["equipped_weapon"] == "wstg-v1"
    assert "policy_overlaid" in out and "policy_kept" in out

    # current — now equipped
    main(["weapon", "--state-root", str(tmp_path), "current"])
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"equipped_weapon": "wstg-v1"}

    # unequip
    main(["weapon", "--state-root", str(tmp_path), "unequip"])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["equipped_weapon"] is None
    assert "policy_stripped" in out and "policy_untouched" in out


def test_cli_equip_rejects_unsealed_weapon(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    rc = main(["weapon", "--state-root", str(tmp_path), "equip", "ghost"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert "not sealed" in out["error"]
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_cli_list(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    weapon_dir = weapons_mod.weapon_dir(tmp_path, "w-a")
    weapon_dir.mkdir(parents=True)
    (weapon_dir / "seal.json").write_text("{}")
    main(["weapon", "--state-root", str(tmp_path), "list"])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["weapons"] == ["w-a"]
    assert out["equipped"] is None


def _seal_weapon_with_index(tmp_path: Path, wid: str) -> Path:
    """Seed a sealed weapon with a tiny FTS index."""
    from ara.forge.index.builder import FTSIndex, IndexEntry
    wdir = weapons_mod.weapon_dir(tmp_path, wid)
    wdir.mkdir(parents=True)
    (wdir / "seal.json").write_text(json.dumps({
        "weapon_id": wid, "weapon_target_id": wid,
        "weapon_hash": "h" * 64,
    }))
    (wdir / "search").mkdir()
    idx = FTSIndex(wdir / "search" / "fts.db")
    idx.add_many([
        IndexEntry("c:sqli", "crumb", "probe sqli", "sql injection check"),
        IndexEntry("c:xss", "crumb", "probe xss", "reflected cross-site"),
    ])
    return wdir


def test_cli_calibrate_passes_and_writes_report(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    wdir = _seal_weapon_with_index(tmp_path, "w-cal")
    (wdir / "calibration_queries.json").write_text(json.dumps({
        "queries": [
            {"query": "sqli", "expected_top_ids": ["c:sqli"],
             "kind": "crumb", "min_precision_at_5": 0.2},
        ],
    }))
    rc = main([
        "weapon", "--state-root", str(tmp_path),
        "calibrate", "w-cal", "--min-precision", "0.2",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["queries_total"] == 1
    assert out["queries_passed"] == 1
    assert out["seal_blocked"] is False
    assert (wdir / "calibration_report.json").is_file()
    rep = json.loads((wdir / "calibration_report.json").read_text())
    assert rep["weapon_id"] == "w-cal"
    assert rep["queries_passed"] == 1


def test_cli_calibrate_blocks_on_low_precision(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    wdir = _seal_weapon_with_index(tmp_path, "w-block")
    (wdir / "calibration_queries.json").write_text(json.dumps({
        "queries": [
            {"query": "ghost", "expected_top_ids": ["c:does_not_exist"],
             "min_precision_at_5": 0.6},
        ],
    }))
    rc = main([
        "weapon", "--state-root", str(tmp_path),
        "calibrate", "w-block",
    ])
    assert rc == 3
    out = json.loads(capsys.readouterr().out.strip())
    assert out["seal_blocked"] is True
    assert out["queries_failed"] == 1


def test_cli_calibrate_unsealed(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    rc = main([
        "weapon", "--state-root", str(tmp_path),
        "calibrate", "ghost",
    ])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert "not sealed" in out["error"]


def test_cli_calibrate_missing_queries(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    _seal_weapon_with_index(tmp_path, "w-noq")
    rc = main([
        "weapon", "--state-root", str(tmp_path),
        "calibrate", "w-noq",
    ])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert "calibration queries" in out["error"]


def test_cli_calibrate_missing_index(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    wdir = weapons_mod.weapon_dir(tmp_path, "w-noidx")
    wdir.mkdir(parents=True)
    (wdir / "seal.json").write_text("{}")
    (wdir / "calibration_queries.json").write_text(json.dumps({
        "queries": [{"query": "x", "expected_top_ids": []}],
    }))
    rc = main([
        "weapon", "--state-root", str(tmp_path),
        "calibrate", "w-noidx",
    ])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert "search index" in out["error"]
