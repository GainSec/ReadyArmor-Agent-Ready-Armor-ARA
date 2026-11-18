"""Heartbeat watchdog: a guard that stops beating gets SIGTERM'd."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from ara.supervisor import Supervisor


@pytest.mark.asyncio
async def test_watchdog_kills_silent_pid(short_tmp: Path, monkeypatch) -> None:
    tmp_path = short_tmp
    monkeypatch.setenv("ARA_HEARTBEAT_TIMEOUT_S", "1")
    sup = await Supervisor.start(state_root=tmp_path / ".ara")
    try:
        # Fork a child that just sleeps — stand-in for a guard pid.
        child = os.fork()
        if child == 0:
            try:
                time.sleep(30)
            finally:
                os._exit(0)
        # Register the child as a known guard with a stale heartbeat.
        sup.guards[child] = type("G", (), {
            "pid": child, "ppid": os.getpid(),
            "runtime": "test", "argv": [],
        })()
        sup._heartbeat_at[child] = time.time() - 5.0
        # Watchdog polls once per second; timeout is 1s. Wait a bit.
        for _ in range(40):
            await asyncio.sleep(0.1)
            try:
                pid, _ = os.waitpid(child, os.WNOHANG)
                if pid == child:
                    break
            except ChildProcessError:
                break
        else:
            os.kill(child, signal.SIGKILL)
            pytest.fail("watchdog did not kill silent guard within 4s")
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_watchdog_silently_drops_already_dead_pid(
    short_tmp: Path, monkeypatch,
) -> None:
    """Short-lived helpers (``ip neigh show`` and similar) inherit the
    libc shim, register as guards, then exit in <500 ms. When the
    watchdog finally notices at the timeout window the pid is already
    gone. No ``heartbeat_miss`` event should be emitted in that case —
    clean exits are not compromise events.
    """
    monkeypatch.setenv("ARA_HEARTBEAT_TIMEOUT_S", "1")
    sup = await Supervisor.start(state_root=short_tmp / ".ara")
    try:
        child = os.fork()
        if child == 0:
            os._exit(0)
        # Reap so the pid isn't a zombie — once reaped, kill(pid, 0)
        # returns ESRCH, which is what the watchdog probes for.
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass

        events_path = sup.events.path
        before = events_path.read_text() if events_path.exists() else ""

        sup.guards[child] = type("G", (), {
            "pid": child, "ppid": os.getpid(),
            "runtime": "test", "argv": [],
        })()
        sup._heartbeat_at[child] = time.time() - 5.0
        # Give the watchdog a couple of polls to notice + drop it.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if child not in sup.guards:
                break
        else:
            pytest.fail("watchdog did not drop dead pid from tracking")

        after = events_path.read_text() if events_path.exists() else ""
        delta = after[len(before):]
        assert "heartbeat_miss" not in delta, (
            f"unexpected heartbeat_miss for already-dead pid: {delta!r}"
        )
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_watchdog_does_not_kill_fresh_guard(short_tmp: Path, monkeypatch) -> None:
    tmp_path = short_tmp
    monkeypatch.setenv("ARA_HEARTBEAT_TIMEOUT_S", "10")
    sup = await Supervisor.start(state_root=tmp_path / ".ara")
    try:
        child = os.fork()
        if child == 0:
            try:
                time.sleep(3)
            finally:
                os._exit(0)
        sup.guards[child] = type("G", (), {
            "pid": child, "ppid": os.getpid(),
            "runtime": "test", "argv": [],
        })()
        sup._heartbeat_at[child] = time.time()
        # Sleep past one poll cycle; child should still be alive.
        await asyncio.sleep(1.5)
        try:
            os.kill(child, 0)
        except OSError:
            pytest.fail("watchdog killed a fresh-beating guard")
        # Clean up.
        os.kill(child, signal.SIGKILL)
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass
    finally:
        await sup.stop()
