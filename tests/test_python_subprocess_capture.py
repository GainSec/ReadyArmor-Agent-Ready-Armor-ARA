from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from ara.clients import sitecustomize


REPO_ROOT = Path(__file__).resolve().parents[1]
PYBOOTSTRAP = REPO_ROOT / "src" / "ara" / "clients" / "_pybootstrap"


def test_subprocess_run_event_captures_target_url_output() -> None:
    sent: list[dict] = []

    result = subprocess.CompletedProcess(
        args=["curl", "-sI", "https://example.test/"],
        returncode=0,
        stdout="HTTP/2 200\r\nserver: cloudflare\r\n",
        stderr="",
    )

    sitecustomize._emit_subprocess_output_event(
        result.args,
        result.stdout,
        result.stderr,
        returncode=result.returncode,
        sender=sent.append,
    )

    assert sent
    event = sent[0]
    assert event["type"] == "event"
    assert event["kind"] == "spawn_output"
    assert event["source"] == "python-subprocess"
    assert event["command"] == "curl"
    assert event["url"] == "https://example.test/"
    assert event["stdout_text"].startswith("HTTP/2 200")
    assert event["exit_code"] == 0


def test_subprocess_output_event_ignores_non_target_commands() -> None:
    sent: list[dict] = []

    sitecustomize._emit_subprocess_output_event(
        ["python3", "-c", "print('hello')"],
        "hello\n",
        "",
        returncode=0,
        sender=sent.append,
    )

    assert sent == []


def test_pyintercept_disable_does_not_disable_subprocess_capture(monkeypatch) -> None:
    monkeypatch.setenv("ARA_TRANSCRIPT_PROTOCOL", "v1")
    monkeypatch.setenv("ARA_PYINTERCEPT_DISABLE", "1")

    assert sitecustomize._sdk_intercept_enabled() is False
    assert sitecustomize._subprocess_capture_enabled() is True


def test_task_run_subprocess_enforcement_allows_supervisor_allow(monkeypatch) -> None:
    monkeypatch.setenv("ARA_TRANSCRIPT_PROTOCOL", "v1")
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")

    calls = []

    def fake_query(command, args):
        calls.append((command, args))
        return {"verdict": "allow"}

    sitecustomize._enforce_subprocess_decision(
        ["python3", "probe.py"],
        query=fake_query,
    )

    assert calls == [("python3", ["probe.py"])]


def test_task_run_subprocess_enforcement_denies_supervisor_deny(monkeypatch) -> None:
    monkeypatch.setenv("ARA_TRANSCRIPT_PROTOCOL", "v1")
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")

    def fake_query(command, args):
        return {"verdict": "deny", "reason": "TASK_COMMIT_REQUIRED"}

    try:
        sitecustomize._enforce_subprocess_decision(
            ["python3", "probe.py"],
            query=fake_query,
        )
    except PermissionError as exc:
        assert "TASK_COMMIT_REQUIRED" in str(exc)
    else:
        raise AssertionError("expected PermissionError")


def test_task_run_subprocess_enforcement_fails_closed_on_ipc_error(monkeypatch) -> None:
    monkeypatch.setenv("ARA_TRANSCRIPT_PROTOCOL", "v1")
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")

    def fake_query(command, args):
        raise OSError("socket down")

    try:
        sitecustomize._enforce_subprocess_decision(
            ["python3", "probe.py"],
            query=fake_query,
        )
    except PermissionError as exc:
        assert "ARA_SUBPROCESS_QUERY_FAILED" in str(exc)
    else:
        raise AssertionError("expected PermissionError")


def test_bootstrap_wraps_subprocess_when_pyintercept_disabled() -> None:
    env = {
        **os.environ,
        "ARA_TRANSCRIPT_PROTOCOL": "v1",
        "ARA_PYINTERCEPT_DISABLE": "1",
        "PYTHONPATH": os.pathsep.join([str(PYBOOTSTRAP), str(REPO_ROOT / "src")]),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, subprocess, sys; "
                "print(json.dumps({"
                "'loaded': 'ara.clients.sitecustomize' in sys.modules, "
                "'wrapped': bool(getattr(subprocess, '_ara_wrapped', False)), "
                "'run_wrapped': bool(getattr(subprocess.run, '_ara_wrapped', False))"
                "}))"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    data = json.loads(proc.stdout)
    assert data == {"loaded": True, "wrapped": True, "run_wrapped": True}
