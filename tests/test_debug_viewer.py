import json
from pathlib import Path

from ara import debug_viewer


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_load_records_filters_component_event_and_grep(tmp_path: Path) -> None:
    log_path = tmp_path / "ara-debug.jsonl"
    _write(log_path, [
        {
            "iso": "2026-06-23T00:00:00+00:00",
            "level": "debug",
            "component": "launch",
            "event": "start",
            "fields": {"command": ["hermes"]},
        },
        {
            "iso": "2026-06-23T00:00:01+00:00",
            "level": "debug",
            "component": "mitm",
            "event": "task_action_deny",
            "fields": {"reason": "TASK_COMMIT_REQUIRED"},
        },
    ])

    rows = debug_viewer.load_records(
        log_path,
        component="mitm",
        grep="commit_required",
    )

    assert len(rows) == 1
    assert rows[0]["component"] == "mitm"
    assert rows[0]["event"] == "task_action_deny"


def test_format_record_shows_origin_and_summary() -> None:
    line = debug_viewer.format_record({
        "iso": "2026-06-23T00:00:01+00:00",
        "level": "debug",
        "component": "task-gate",
        "event": "decision",
        "session_id": "sess-123456",
        "task_id": "task-abcdef",
        "caller": "task_action_gate.py:40 evaluate_query",
        "fields": {"verdict": "deny", "reason": "TASK_COMMIT_REQUIRED"},
    }, width=140)

    assert "task-gate" in line
    assert "decision" in line
    assert "sess-1234" in line
    assert "task-abcd" in line
    assert "task_action_gate.py:40" in line
    assert "TASK_COMMIT_REQUIRED" in line


def test_print_records_honors_limit(tmp_path: Path, capsys) -> None:
    log_path = tmp_path / "ara-debug.jsonl"
    _write(log_path, [
        {"component": "a", "event": "one", "fields": {}},
        {"component": "b", "event": "two", "fields": {}},
    ])

    rc = debug_viewer.print_records(log_path, limit=1, width=120)

    captured = capsys.readouterr().out
    assert rc == 0
    assert "two" in captured
    assert "one" not in captured
