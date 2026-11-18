"""F — supervisor watches the equipped armor's WALLS, not just the
file passed at launch. Equipping a new armor (or editing its WALLS)
triggers a reload + broadcast without restarting the supervisor.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from ara import armors as armors_mod
from ara.supervisor import Supervisor
from ara.walls import parse_file as parse_walls_file


_BASE_WALLS = "## deny_path\n/launch/forbidden\n"
_ARMOR_WALLS = "## deny_path\n/armor/forbidden\n"


@pytest.mark.asyncio
async def test_supervisor_switches_to_armor_walls_when_equipped(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    launch_walls = short_tmp / "launch_walls.md"
    launch_walls.write_text(_BASE_WALLS)

    armors_mod.create_armor(state, "alpha", description="t")
    armor = armors_mod.load_armor(state, "alpha")
    assert armor is not None
    armor.walls_path.write_text(_ARMOR_WALLS)

    sup = await Supervisor.start(state_root=state, walls_file=launch_walls)
    try:
        # Watcher polls every 1s; give it a couple cycles to discover
        # the launch file (no armor yet equipped → falls back to launch).
        for _ in range(40):
            await asyncio.sleep(0.1)
            if any(
                r.pattern == "/launch/forbidden" for r in sup.walls.path_rules
            ):
                break
        assert any(
            r.pattern == "/launch/forbidden" for r in sup.walls.path_rules
        ), "launch WALLS should be loaded initially"

        # Equip the armor — watcher should swap to armor's WALLS within
        # a couple of poll cycles.
        armors_mod.set_active_armor(state, "alpha")
        for _ in range(40):
            await asyncio.sleep(0.1)
            if any(
                r.pattern == "/armor/forbidden" for r in sup.walls.path_rules
            ):
                break
        else:
            pytest.fail("supervisor did not pick up the armor's WALLS")

        # Edit the armor's WALLS in place — watcher should re-parse.
        armor.walls_path.write_text(
            "## deny_path\n/armor/forbidden\n/armor/also-forbidden\n"
        )
        for _ in range(40):
            await asyncio.sleep(0.1)
            if any(
                r.pattern == "/armor/also-forbidden" for r in sup.walls.path_rules
            ):
                break
        else:
            pytest.fail("supervisor did not pick up edited armor WALLS")
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_supervisor_falls_back_to_launch_walls_when_no_armor_equipped(
    short_tmp: Path,
):
    state = short_tmp / ".ara"
    state.mkdir()
    launch_walls = short_tmp / "launch_walls.md"
    launch_walls.write_text(_BASE_WALLS)

    sup = await Supervisor.start(state_root=state, walls_file=launch_walls)
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            if any(
                r.pattern == "/launch/forbidden" for r in sup.walls.path_rules
            ):
                break
        assert any(
            r.pattern == "/launch/forbidden" for r in sup.walls.path_rules
        )
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_sync_rules_include_active_bulwark_fs_denies(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    launch_walls = short_tmp / "launch_walls.md"
    launch_walls.write_text(_BASE_WALLS)

    armors_mod.create_armor(state, "alpha", description="t")
    armors_mod.set_active_armor(state, "alpha")
    bulwark_path = state / "bulwark.md"
    bulwark_path.write_text(
        "## global\n"
        "deny fs /global-secret\n"
        "## armor: alpha\n"
        "deny fs /alpha-secret\n"
        "## armor: beta\n"
        "deny fs /beta-secret\n"
    )

    sup = await Supervisor.start(state_root=state, walls_file=launch_walls)
    try:
        sup.walls = parse_walls_file(launch_walls)
        payload = sup._sync_rules_payload()
        patterns = {r["pattern"] for r in payload["deny_path"]}
        assert "/launch/forbidden" in patterns
        assert "/global-secret" in patterns
        assert "/alpha-secret" in patterns
        assert "/beta-secret" not in patterns
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_sync_rules_include_active_bulwark_net_allowlist_and_denies(
    short_tmp: Path,
):
    state = short_tmp / ".ara"
    state.mkdir()
    launch_walls = short_tmp / "launch_walls.md"
    launch_walls.write_text(_BASE_WALLS)

    armors_mod.create_armor(state, "blacksmith", description="t")
    armors_mod.set_active_armor(state, "blacksmith")
    bulwark_path = state / "bulwark.md"
    bulwark_path.write_text(
        "## armor: blacksmith\n"
        "allow net 192.168.1.2:443\n"
        "allow net 127.0.0.1:*\n"
        "deny net *:*\n"
        "## armor: other\n"
        "deny net 10.0.0.1:443\n"
    )

    sup = await Supervisor.start(state_root=state, walls_file=launch_walls)
    try:
        payload = sup._sync_rules_payload()
        deny_patterns = {r["pattern"] for r in payload["deny_net"]}
        allow_patterns = {r["pattern"] for r in payload["allow_net"]}
        assert "*:*" in deny_patterns
        assert "10.0.0.1:443" not in deny_patterns
        assert "192.168.1.2:443" in allow_patterns
        assert "127.0.0.1:*" in allow_patterns
    finally:
        await sup.stop()
