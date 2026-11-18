from __future__ import annotations

import errno
import os
import subprocess

from ara import process_state


def test_is_pid_live_false_for_invalid_pid() -> None:
    assert process_state.is_pid_live(None) is False
    assert process_state.is_pid_live(0) is False
    assert process_state.is_pid_live("abc") is False


def test_is_pid_live_false_when_kill_reports_missing(monkeypatch) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        raise OSError(errno.ESRCH, "no such process")

    monkeypatch.setattr(os, "kill", fake_kill)

    assert process_state.is_pid_live(1234) is False


def test_is_pid_live_false_for_zombie_status(monkeypatch) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        return None

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="Z+\n", stderr="")

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert process_state.is_pid_live(1234) is False


def test_is_pid_live_true_for_running_status(monkeypatch) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        return None

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="Ss\n", stderr="")

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert process_state.is_pid_live(1234) is True
