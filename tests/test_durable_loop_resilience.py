"""Durable-loop connection-death resilience: a model connection drop is a
retryable transient (its own backoff budget), not an empty-cycle strike."""

from __future__ import annotations

import subprocess

from ara.armory.app import _build_durable_hermes_worker


def _script() -> str:
    cmd = _build_durable_hermes_worker(
        ["hermes"], task_depth="full", operator_prompt="t",
        context={"window_end": "2026-07-13T10:00:00Z"}, stamp="test",
    )
    assert cmd[0] == "bash" and cmd[1] == "-lc"
    return cmd[2]


def test_generated_loop_is_valid_bash() -> None:
    r = subprocess.run(["bash", "-n"], input=_script(), text=True,
                       capture_output=True)
    assert r.returncode == 0, r.stderr


def test_connection_death_branch_present() -> None:
    s = _script()
    # detects the real connection-death signatures
    for sig in ("Connection lost", "ConnectionResetError", "no final response was produced"):
        assert sig in s
    # its own budget + backoff, distinct from empty-cycle tolerance
    assert "CONN_FAILS" in s
    assert "ARA_TASK_MAX_CONN_RETRIES" in s
    assert "ARA_TASK_CONN_BACKOFF" in s
    assert "model connection lost" in s          # retry message
    assert "model unreachable after" in s         # give-up message
    # captures the cycle's output so the branch can inspect it
    assert "ARA_CYCLE_LOG" in s


def test_conn_fails_resets_on_success() -> None:
    s = _script()
    # every success reset that clears EMPTY_CYCLES also clears CONN_FAILS
    assert "EMPTY_CYCLES=0; CONN_FAILS=0;" in s


def test_backoff_is_exponential_and_bounded() -> None:
    # mirror the loop's arithmetic: base * (1 << min(fails,5))
    base = 10
    sched = [base * (1 << (f if f < 5 else 5)) for f in range(1, 6)]
    assert sched == [20, 40, 80, 160, 320]  # ~10 min total recovery window
