"""End-to-end test for the ForgeChannelClient SDK over a real unix socket.

Spins up the supervisor's IPC server with a minimal handler, connects
a client, makes channel calls, asserts responses match the
``channel_call_response`` shape.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ara import weapons as weapons_mod
from ara.clients.forge_channel_sdk import (
    ChannelCallError,
    ForgeChannelClient,
    call_once,
)
from ara.runtime import audit as raudit
from ara.runtime.channels import registry as channel_registry


@pytest.fixture(autouse=True)
def _isolate(short_tmp: Path):
    raudit.reset_registry_for_tests()
    channel_registry.unregister_for_tests()
    yield
    raudit.reset_registry_for_tests()
    channel_registry.unregister_for_tests()


def _seal_minimal_weapon(state_root: Path, weapon_id: str = "w-test") -> None:
    from ara.forge.chunks.chunkdef import ChunkDef, write_chunkdef
    wdir = state_root / "weapons" / weapon_id
    (wdir / "chunks").mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({
        "weapon_target_id": weapon_id, "crumbs": [], "shapes": [],
    }))
    (wdir / "seal.json").write_text(json.dumps({
        "weapon_id": weapon_id, "weapon_hash": "0" * 64,
    }))
    write_chunkdef(wdir / "chunks" / "enum.json", ChunkDef(
        id="enum", title="Enum", prompt_fragment="x",
        tool_surface=["fact.assert", "lead.file", "facts.query"],
        is_entry=True, is_terminal=True,
    ))


async def _start_supervisor(state_root: Path):
    """Start a real supervisor with its IPC server bound to a socket."""
    from ara.supervisor import Supervisor
    sup = await Supervisor.start(state_root=state_root)
    return sup


async def _stop_supervisor(sup) -> None:
    if sup._reload_task is not None:
        sup._reload_task.cancel()
    if sup._watchdog_task is not None:
        sup._watchdog_task.cancel()
    if sup._bulwark_task is not None:
        sup._bulwark_task.cancel()
    await sup.server.stop()
    if sup.engagement is not None and sup.engagement.autofirer is not None:
        # No explicit shutdown needed; just clear the binder hooks
        pass


# ---------- live socket integration ----------

@pytest.mark.asyncio
async def test_sdk_call_returns_result(short_tmp: Path) -> None:
    _seal_minimal_weapon(short_tmp)
    weapons_mod.set_equipped_weapon(short_tmp, "w-test")
    sup = await _start_supervisor(short_tmp)
    try:
        # Drop a piece of evidence in the active session so fact.assert can cite it
        from ara.runtime.binder.api import EvidenceBinder
        eng_dir = short_tmp / "sessions" / sup.session.id
        binder = EvidenceBinder(eng_dir)
        rec = binder.put(
            b"x", source="tool:curl", source_metadata={}, summary="s",
            activation_id=sup.engagement.activation_manager.current(),
        )
        client = ForgeChannelClient(sup.session.socket_path)
        await client.connect()
        try:
            result = await client.call("fact.assert", {
                "predicate": "target.host", "value": "example.com",
                "asserted_by_crumb": "c1", "evidence_refs": [rec.id],
            })
            assert result["fact"]["predicate"] == "target.host"
        finally:
            await client.close()
    finally:
        await _stop_supervisor(sup)


@pytest.mark.asyncio
async def test_sdk_call_raises_on_no_weapon(short_tmp: Path) -> None:
    sup = await _start_supervisor(short_tmp)
    try:
        client = ForgeChannelClient(sup.session.socket_path)
        await client.connect()
        try:
            with pytest.raises(ChannelCallError) as exc:
                await client.call("fact.assert", {})
            assert exc.value.error_kind == "no_weapon_equipped"
        finally:
            await client.close()
    finally:
        await _stop_supervisor(sup)


@pytest.mark.asyncio
async def test_sdk_call_raises_on_tool_surface_violation(short_tmp: Path) -> None:
    _seal_minimal_weapon(short_tmp)
    weapons_mod.set_equipped_weapon(short_tmp, "w-test")
    sup = await _start_supervisor(short_tmp)
    try:
        client = ForgeChannelClient(sup.session.socket_path)
        await client.connect()
        try:
            with pytest.raises(ChannelCallError) as exc:
                # finding.file is NOT in entry chunk's tool_surface
                await client.call("finding.file", {})
            assert exc.value.error_kind == "tool_not_in_surface"
        finally:
            await client.close()
    finally:
        await _stop_supervisor(sup)


@pytest.mark.asyncio
async def test_sdk_concurrent_calls_match_by_seq(short_tmp: Path) -> None:
    """Multiple in-flight calls on the same socket are matched by seq."""
    _seal_minimal_weapon(short_tmp)
    weapons_mod.set_equipped_weapon(short_tmp, "w-test")
    sup = await _start_supervisor(short_tmp)
    try:
        from ara.runtime.binder.api import EvidenceBinder
        eng_dir = short_tmp / "sessions" / sup.session.id
        binder = EvidenceBinder(eng_dir)
        recs = [
            binder.put(
                f"x{i}".encode(), source="tool:curl",
                source_metadata={}, summary=f"s{i}",
                activation_id=sup.engagement.activation_manager.current(),
            )
            for i in range(5)
        ]
        client = ForgeChannelClient(sup.session.socket_path)
        await client.connect()
        try:
            tasks = [
                asyncio.create_task(client.call("fact.assert", {
                    "predicate": f"target.{i}", "value": i,
                    "asserted_by_crumb": "c1",
                    "evidence_refs": [recs[i].id],
                }))
                for i in range(5)
            ]
            results = await asyncio.gather(*tasks)
            assert {r["fact"]["predicate"] for r in results} == {
                f"target.{i}" for i in range(5)
            }
        finally:
            await client.close()
    finally:
        await _stop_supervisor(sup)


@pytest.mark.asyncio
async def test_sdk_call_once_helper(short_tmp: Path) -> None:
    _seal_minimal_weapon(short_tmp)
    weapons_mod.set_equipped_weapon(short_tmp, "w-test")
    sup = await _start_supervisor(short_tmp)
    try:
        # facts.query is in entry chunk's tool_surface; safe to call without setup
        result = await call_once(
            sup.session.socket_path, "facts.query", {"predicate": "x"},
        )
        assert result["facts"] == []
    finally:
        await _stop_supervisor(sup)


@pytest.mark.asyncio
async def test_sdk_call_raises_on_channel_rejection(short_tmp: Path) -> None:
    _seal_minimal_weapon(short_tmp)
    weapons_mod.set_equipped_weapon(short_tmp, "w-test")
    sup = await _start_supervisor(short_tmp)
    try:
        client = ForgeChannelClient(sup.session.socket_path)
        await client.connect()
        try:
            # fact.assert with neither evidence_refs nor depends_on
            with pytest.raises(ChannelCallError) as exc:
                await client.call("fact.assert", {
                    "predicate": "p", "value": 1,
                    "asserted_by_crumb": "c",
                })
            assert exc.value.error_kind == "channel_rejection"
        finally:
            await client.close()
    finally:
        await _stop_supervisor(sup)


@pytest.mark.asyncio
async def test_sdk_close_fails_in_flight_calls(short_tmp: Path) -> None:
    _seal_minimal_weapon(short_tmp)
    weapons_mod.set_equipped_weapon(short_tmp, "w-test")
    sup = await _start_supervisor(short_tmp)
    try:
        client = ForgeChannelClient(sup.session.socket_path)
        await client.connect()
        # Don't await: queue a request and close immediately
        task = asyncio.create_task(client.call("facts.query", {"predicate": "x"}))
        await asyncio.sleep(0)  # let the task register the pending future
        await client.close()
        # The in-flight call should resolve to a transport-closed error
        # (or succeed if it raced — both are acceptable; we just want
        # no hang).
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except ChannelCallError as e:
            assert e.error_kind in ("transport_closed", "timeout")
        except asyncio.TimeoutError:
            pytest.fail("close() did not unblock pending call")
    finally:
        await _stop_supervisor(sup)
