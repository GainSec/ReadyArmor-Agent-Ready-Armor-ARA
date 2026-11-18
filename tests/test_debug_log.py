import json
from pathlib import Path

from ara import debug_log


def test_emit_writes_jsonl_with_caller_and_redaction(tmp_path: Path) -> None:
    state_root = tmp_path / ".ara"

    debug_log.emit(
        "unit",
        "secret.check",
        state_root=state_root,
        session_id="sess-1",
        task_id="task-1",
        api_key="sk-test-secret",
        nested={"password": "hunter2", "safe": "visible"},
    )

    log_path = state_root / "logs" / "ara-debug.jsonl"
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["component"] == "unit"
    assert row["event"] == "secret.check"
    assert row["session_id"] == "sess-1"
    assert row["task_id"] == "task-1"
    assert row["caller"]
    assert row["fields"]["api_key"] != "sk-test-secret"
    assert row["fields"]["nested"]["password"] != "hunter2"
    assert row["fields"]["nested"]["safe"] == "visible"


def test_emit_uses_explicit_env_log_path(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "custom.jsonl"
    monkeypatch.setenv("ARA_DEBUG_LOG", str(log_path))

    debug_log.emit("unit", "env.path", value=1)

    row = json.loads(log_path.read_text())
    assert row["component"] == "unit"
    assert row["event"] == "env.path"
    assert row["fields"]["value"] == 1


def test_emit_without_state_or_env_is_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ARA_DEBUG_LOG", raising=False)
    monkeypatch.delenv("ARA_DEBUG", raising=False)
    monkeypatch.chdir(tmp_path)

    debug_log.emit("unit", "noop")

    assert not (tmp_path / ".ara" / "logs" / "ara-debug.jsonl").exists()


def test_emit_exception_records_error_fields(tmp_path: Path) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        debug_log.emit_exception(
            "unit",
            "failed",
            exc,
            state_root=tmp_path / ".ara",
            where="test",
        )

    row = json.loads((tmp_path / ".ara" / "logs" / "ara-debug.jsonl").read_text())
    assert row["level"] == "error"
    assert row["fields"]["exception_type"] == "RuntimeError"
    assert row["fields"]["exception"] == "boom"
    assert row["fields"]["where"] == "test"
