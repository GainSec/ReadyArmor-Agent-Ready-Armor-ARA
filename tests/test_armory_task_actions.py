from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from ara import armors as armors_mod
from ara import temper as temper_mod
from ara import trims as trims_mod
from ara import weapons as weapons_mod
from ara.armory import app
from ara.armory import forge_v1_views


class _FakeProc:
    pid = 4242


class _WaitableFakeProc:
    pid = 4343

    def __init__(self, done: threading.Event) -> None:
        self.done = done

    def wait(self) -> int:
        self.done.set()
        return 0


def _seal_task_weapon(state_root, weapon_id="w-task"):
    weapon_dir = state_root / "weapons" / weapon_id
    (weapon_dir / "chunks").mkdir(parents=True)
    (weapon_dir / "weapon.json").write_text(json.dumps({
        "weapon_target_id": weapon_id,
        "crumbs": [],
        "shapes": [],
    }))
    (weapon_dir / "seal.json").write_text(json.dumps({
        "weapon_id": weapon_id,
        "weapon_hash": "0" * 64,
    }))
    (weapon_dir / "chunks" / "enum.json").write_text(json.dumps({
        "id": "enum",
        "title": "Enum",
        "prompt_fragment": "",
        "tool_surface": ["fact.assert", "lead.file", "chunks.emit_tag"],
        "exits": {},
        "is_entry": True,
    }))
    weapons_mod.set_equipped_weapon(state_root, weapon_id)
    return weapon_dir


def _setup_run_task_loadout(state_root):
    armors_mod.create_armor(state_root, "blacksmith")
    armors_mod.set_active_armor(state_root, "blacksmith")
    _seal_task_weapon(state_root)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    meta = trims_mod.new_trim(
        state_root, "blacksmith", "active-task", target="https://example.test/",
    )
    meta.active_testing = True
    meta.test_window_end = future
    meta.expires_at = future
    trims_mod.save_meta(state_root, meta)
    trims_mod.apply_trim(state_root, "blacksmith", "active-task")
    proposal = temper_mod.create_proposal(
        state_root, "blacksmith", source_trims=["active-task"],
    )
    sealed = temper_mod.seal_selected_proposal(
        state_root, "blacksmith", proposal.proposal_id, selected_indexes=[0],
    )
    temper_mod.activate_temper(state_root, "blacksmith", sealed.proposal_id)
    return {"window_end": future, "temper_id": sealed.proposal_id}


def test_start_task_session_reaps_launcher_and_updates_marker(monkeypatch, tmp_path):
    done = threading.Event()

    def fake_popen(cmd, **kwargs):
        return _WaitableFakeProc(done)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _setup_run_task_loadout(tmp_path)

    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "contain": "on", "single_turn": "on"},
    )

    assert ok is True
    assert "4343" in msg
    assert done.wait(1.0) is True
    marker = next((tmp_path / "task-launches").glob("task-*.json"))
    deadline = time.time() + 1.0
    data = json.loads(marker.read_text())
    while data.get("status") != "exited" and time.time() < deadline:
        time.sleep(0.01)
        data = json.loads(marker.read_text())
    assert data["status"] == "exited"
    assert data["exit_code"] == 0


def test_start_task_session_builds_hermes_launch(monkeypatch, tmp_path):
    calls = []
    home = tmp_path / "home"
    hermes_bin = home / ".local" / "bin" / "hermes"
    hermes_bin.parent.mkdir(parents=True)
    hermes_bin.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(home))

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _setup_run_task_loadout(tmp_path)

    ok, msg = app._start_task_session(
        tmp_path,
        {
            "worker_cmd": "hermes",
            "prompt": "hello task",
            "contain": "on",
            "single_turn": "on",
        },
    )

    assert ok is True
    assert "4242" in msg
    cmd, kwargs = calls[0]
    assert cmd[:4] == [app.sys.executable, "-m", "ara.cli", "launch"]
    assert "--state-root" in cmd
    assert str(tmp_path) in cmd
    assert "--contain" in cmd
    assert cmd[-3:] == [str(hermes_bin), "-z", "hello task"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    marker = next((tmp_path / "task-launches").glob("task-*.json"))
    data = json.loads(marker.read_text())
    assert data["pid"] == 4242
    assert data["worker_command"] == [str(hermes_bin), "-z", "hello task"]


def test_start_task_session_wraps_default_hermes_in_durable_loop(monkeypatch, tmp_path):
    calls = []
    home = tmp_path / "home"
    hermes_bin = home / ".local" / "bin" / "hermes"
    hermes_bin.parent.mkdir(parents=True)
    hermes_bin.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(home))

    loadout = _setup_run_task_loadout(tmp_path)
    future = loadout["window_end"]

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "prompt": "operator goal", "contain": "on"},
    )

    assert ok is True
    assert "4242" in msg
    cmd, kwargs = calls[0]
    assert cmd[-3:-1] == ["bash", "-lc"]
    loop_script = cmd[-1]
    assert str(hermes_bin) in loop_script
    assert future in loop_script
    assert "while" in loop_script
    assert "timeout" in loop_script
    assert "--toolsets browser,terminal,file,code_execution" not in loop_script
    assert "--toolsets web" not in loop_script
    assert "operator goal" in loop_script
    assert "fact.assert" in loop_script
    assert "lead.file" in loop_script
    assert "finding.file" in loop_script
    assert "chunks.emit_tag" in loop_script
    assert "mcp_ara_forge_fact_assert" in loop_script
    assert "mcp_ara_forge_lead_file" in loop_script
    assert "mcp_ara_forge_evidence_search" in loop_script
    assert "_ara_current_chunk" in loop_script
    assert "_ara_current_chunk_context" in loop_script
    assert "CHUNK_CONTEXT=$(_ara_current_chunk_context)" in loop_script
    assert "Valid exits:" in loop_script
    assert "TURN_PROMPT" in loop_script
    assert "Task depth:" not in loop_script
    assert "passive_probe" not in loop_script
    assert "operator_review" in loop_script
    assert "operator review requested" in loop_script
    assert "Use contained terminal/process tooling for target URLs" in loop_script
    assert "Use contained browser and terminal tooling" not in loop_script
    assert "Do not end a cycle with only prose or terminal output" in loop_script
    assert "Do not use web_extract or web_search for target URLs" in loop_script
    assert kwargs["env"]["ARA_TASK_SUPERVISOR"] == "armory-hermes-loop"
    assert kwargs["env"]["ARA_TASK_RUN_ID"]
    task_run_id = kwargs["env"]["ARA_TASK_RUN_ID"]
    assert (tmp_path / "task-runs" / task_run_id / "session.json").is_file()
    task_session = json.loads((tmp_path / "task-runs" / task_run_id / "session.json").read_text())
    assert task_session["action_budget"] == 1
    assert task_session["branch_action_budget"] == 3
    marker = next((tmp_path / "task-launches").glob("task-*.json"))
    data = json.loads(marker.read_text())
    assert data["task_run_id"] == task_run_id
    assert data["worker_command"] == ["bash", "-lc", loop_script]
    assert data["supervisor_mode"] == "armory-hermes-loop"
    assert data["hermes_profile_mode"] == "ara-task"
    assert data["task_depth"] == "full"
    assert data["mcp_required"] is True
    assert data["shim_required"] is True
    assert data["weapon_id"] == "w-task"
    assert data["temper_id"] == loadout["temper_id"]
    assert (tmp_path / "task-runs" / task_run_id / "absolute.md").is_file()
    assert data["temper_projection"]["absolute_path"].endswith(
        f"task-runs/{task_run_id}/absolute.md",
    )
    assert data["channel_contract"]["canonical_tools"] == [
        "fact.assert",
        "lead.file",
        "finding.file",
        "evidence.search",
        "evidence.get",
        "chunks.emit_tag",
    ]
    assert "required_channels" not in data["channel_contract"]
    assert data["channel_contract"]["bad_aliases"]["mcp_ara_forge_fact_assert"] == "fact.assert"
    assert "REMAINING=$((END - $(date -u +%s)))" in loop_script
    assert "CYCLE_TIMEOUT=$((ARA_TASK_CYCLE_TIMEOUT < REMAINING ? ARA_TASK_CYCLE_TIMEOUT : REMAINING))" in loop_script
    assert "timeout \"$CYCLE_TIMEOUT\"" in loop_script
    assert "ARA_TASK_CYCLE_TIMEOUT=${ARA_TASK_CYCLE_TIMEOUT:-90}" in loop_script
    assert "ARA_FACTS_BEFORE=$(_ara_structured_count)" in loop_script
    assert "ARA_FACTS_AFTER=$(_ara_structured_count)" in loop_script
    assert "ARA_TASK_MAX_EMPTY_CYCLES=${ARA_TASK_MAX_EMPTY_CYCLES:-6}" in loop_script
    assert "ARA_TASK_MAX_COMMIT_RETRIES=${ARA_TASK_MAX_COMMIT_RETRIES:-1}" in loop_script
    assert "ARA_TASK_MAX_CHUNK_TRANSITION_REPAIRS=${ARA_TASK_MAX_CHUNK_TRANSITION_REPAIRS:-2}" in loop_script
    assert "PHASE=$(_ara_task_phase); if [ \"$PHASE\" = \"commit_required\" ]; then" in loop_script
    assert 'if [ "$PHASE" = "branch_required" ]; then' in loop_script
    assert "ARA_TASK_MAX_BRANCH_RETRIES=${ARA_TASK_MAX_BRANCH_RETRIES:-1}" in loop_script
    assert "BRANCH_REQUIRED. The investigation ledger needs a branch update" in loop_script
    assert "_ara_task_chunk_gated" in loop_script
    assert "BRANCH_REQUIRED_CHUNK_TRANSITION" in loop_script
    chunk_prompt_segment = loop_script.split("BRANCH_REQUIRED_CHUNK_TRANSITION", 1)[1]
    assert "Do not call terminal, browser, web, file, mcp_ara_forge_lead_file" in chunk_prompt_segment
    assert "Call exactly one MCP tool now: mcp_ara_forge_chunks_emit_tag" in chunk_prompt_segment
    assert '-z "$BRANCH_PROMPT"' in loop_script
    assert "_ara_branch_count" in loop_script
    assert "_ara_chunk_transition_count" in loop_script
    assert "ARA_CHUNK_BEFORE=$(_ara_chunk_transition_count)" in loop_script
    assert "ARA_CHUNK_AFTER=$(_ara_chunk_transition_count)" in loop_script
    assert "PHASE_AFTER=$(_ara_task_phase)" in loop_script
    assert '[ "$PHASE_AFTER" = "work" ]' in loop_script
    assert '[ "${ARA_CHUNK_AFTER:-0}" -gt "${ARA_CHUNK_BEFORE:-0}" ]' in loop_script
    assert "branch update required before next action; running branch-only turn" in loop_script
    assert "EMPTY_CYCLES=$((EMPTY_CYCLES+1))" in loop_script
    assert "COMMIT_RETRIES=$((COMMIT_RETRIES+1))" in loop_script
    assert "task-repair-commit" in loop_script
    assert "task-repair-chunk-transition" in loop_script
    assert "chunk transition missing; running system chunk transition repair" in loop_script
    assert "CHUNK_TRANSITION_REPAIR_LIMIT_OR_NO_EXIT" in loop_script
    assert "--accept-hooks --continue ara-task-" in loop_script
    assert "commit required before next action; running commit-only turn" in loop_script
    assert "REPAIR_LIMIT_EXCEEDED" in loop_script
    assert "Evidence lookup channels do not satisfy task progress" in loop_script
    assert "evidence.search" in loop_script
    assert "evidence.get" in loop_script
    assert "chunks.emit_tag" in loop_script
    structured_count_segment = loop_script.split("_ara_task_phase()", 1)[0]
    assert "chunks.emit_tag" in structured_count_segment
    assert "evidence.search" not in structured_count_segment
    assert "evidence.get" not in structured_count_segment
    assert "structured channel output missing" in loop_script
    assert "break" in loop_script


def test_current_chunk_context_helper_returns_active_chunk_exits(tmp_path):
    weapon_dir = tmp_path / "weapons" / "w-task" / "chunks"
    weapon_dir.mkdir(parents=True)
    (weapon_dir / "probe.json").write_text(json.dumps({
        "id": "probe",
        "title": "Probe",
        "exits": {
            "<readied_for_writeup>": {
                "next_chunk_id": "intel",
                "description": "Enough evidence; advance to intel.",
            },
            "<investigation_done>": {
                "next_chunk_id": "enum",
                "description": "Wrap up; return to enum.",
            },
        },
    }))
    session_dir = tmp_path / "sessions" / "sid-a"
    session_dir.mkdir(parents=True)
    (session_dir / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "initialize",
        "to_chunk": "probe",
    }) + "\n")

    env = {
        **os.environ,
        "ARA_STATE_ROOT": str(tmp_path),
        "ARA_SESSION_ID": "sid-a",
        "ARA_TASK_WEAPON_ID": "w-task",
    }
    res = subprocess.run(
        [sys.executable, "-c", app._build_current_chunk_context_python()],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    assert "Active chunk: probe - Probe" in res.stdout
    assert "<readied_for_writeup> -> intel" in res.stdout
    assert "<investigation_done> -> enum" in res.stdout
    assert "mcp_ara_forge_chunks_emit_tag" in res.stdout


def test_current_chunk_context_helper_is_empty_without_chunk_history(tmp_path):
    session_dir = tmp_path / "sessions" / "sid-a"
    session_dir.mkdir(parents=True)

    env = {
        **os.environ,
        "ARA_STATE_ROOT": str(tmp_path),
        "ARA_SESSION_ID": "sid-a",
        "ARA_TASK_WEAPON_ID": "w-task",
    }
    res = subprocess.run(
        [sys.executable, "-c", app._build_current_chunk_context_python()],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    assert res.stdout == ""
    assert res.stderr == ""


def test_durable_loop_stops_cleanly_on_operator_review(tmp_path):
    session_dir = tmp_path / "sessions" / "sid-a"
    session_dir.mkdir(parents=True)
    (session_dir / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "transition",
        "to_chunk": "operator_review",
    }) + "\n")
    cmd = app._build_durable_hermes_worker(
        ["/bin/false"],
        task_depth="full",
        operator_prompt="",
        context={
            "armor_id": "hermes-default",
            "weapon_id": "w-task",
            "trim_id": "trim-a",
            "temper_id": "temper-a",
            "target": "https://example.test/",
        },
        stamp="20300101-000000",
    )
    loop_script = cmd[-1].replace("python3.9", shlex.quote(sys.executable))

    res = subprocess.run(
        ["bash", "-lc", loop_script],
        text=True,
        capture_output=True,
        timeout=5,
        env={
            **os.environ,
            "ARA_STATE_ROOT": str(tmp_path),
            "ARA_SESSION_ID": "sid-a",
            "ARA_TASK_WEAPON_ID": "w-task",
            "ARA_TASK_CYCLE_SLEEP": "0",
            "ARA_TASK_MAX_SECONDS": "30",
        },
    )

    assert res.returncode == 0
    assert "operator review requested" in res.stderr


def test_start_task_session_records_task_depth(monkeypatch, tmp_path):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _setup_run_task_loadout(tmp_path)

    ok, msg = app._start_task_session(
        tmp_path,
        {
            "worker_cmd": "hermes",
            "task_depth": "safe_mutation",
            "contain": "on",
        },
    )

    assert ok is True
    assert "4242" in msg
    marker = next((tmp_path / "task-launches").glob("task-*.json"))
    data = json.loads(marker.read_text())
    assert data["task_depth"] == "safe_mutation"
    assert calls[0][1]["env"]["ARA_TASK_DEPTH"] == "safe_mutation"


def test_start_task_session_rejects_bad_command(tmp_path):
    _setup_run_task_loadout(tmp_path)

    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes 'unterminated"},
    )

    assert ok is False
    assert "Invalid worker command" in msg


def test_start_task_session_requires_complete_run_task_loadout(monkeypatch, tmp_path):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "contain": "on"},
    )
    assert ok is False
    assert "active armor required" in msg
    assert calls == []

    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "contain": "on"},
    )
    assert ok is False
    assert "sealed equipped weapon required" in msg

    _seal_task_weapon(tmp_path)
    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "contain": "on"},
    )
    assert ok is False
    assert "active trim required" in msg

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    meta = trims_mod.new_trim(tmp_path, "blacksmith", "active-task")
    meta.active_testing = True
    meta.test_window_end = future
    meta.expires_at = future
    trims_mod.save_meta(tmp_path, meta)
    trims_mod.apply_trim(tmp_path, "blacksmith", "active-task")
    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "contain": "on"},
    )
    assert ok is False
    assert "active temper required" in msg


def test_start_task_session_blocks_expired_active_trim(monkeypatch, tmp_path):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    _seal_task_weapon(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    meta = trims_mod.new_trim(tmp_path, "blacksmith", "expired")
    meta.active_testing = True
    meta.test_window_end = past
    meta.expires_at = past
    trims_mod.save_meta(tmp_path, meta)
    trims_mod.apply_trim(tmp_path, "blacksmith", "expired")

    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "contain": "on"},
    )

    assert ok is False
    assert "trim expired" in msg
    assert calls == []


def test_start_task_session_blocks_inactive_active_trim(monkeypatch, tmp_path):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    _seal_task_weapon(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    meta = trims_mod.new_trim(tmp_path, "blacksmith", "passive")
    meta.active_testing = False
    meta.test_window_end = future
    meta.expires_at = future
    trims_mod.save_meta(tmp_path, meta)
    trims_mod.apply_trim(tmp_path, "blacksmith", "passive")

    ok, msg = app._start_task_session(
        tmp_path,
        {"worker_cmd": "hermes", "contain": "on"},
    )

    assert ok is False
    assert "does not authorize active testing" in msg
    assert calls == []


def test_task_page_shows_trim_and_temper_slots(tmp_path):
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    trims_mod.new_trim(tmp_path, "blacksmith", "task-a", target="Acme")
    trims_mod.apply_trim(tmp_path, "blacksmith", "task-a")
    proposal = temper_mod.create_proposal(
        tmp_path, "blacksmith", source_trims=["task-a"],
    )
    sealed = temper_mod.seal_selected_proposal(
        tmp_path, "blacksmith", proposal.proposal_id, selected_indexes=[0],
    )
    temper_mod.activate_temper(tmp_path, "blacksmith", sealed.proposal_id)

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "<span>Trim</span><strong>task-a</strong>" in html
    assert f"<span>Temper</span><strong>{sealed.proposal_id}</strong>" in html


def test_task_page_exposes_task_depth_selector(tmp_path):
    _setup_run_task_loadout(tmp_path)

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert 'name="task_depth"' in html
    assert "Safe mutation" in html


def test_task_page_shows_blocked_state_for_expired_trim(tmp_path):
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    _seal_task_weapon(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    meta = trims_mod.new_trim(tmp_path, "blacksmith", "expired")
    meta.active_testing = True
    meta.test_window_end = past
    meta.expires_at = past
    trims_mod.save_meta(tmp_path, meta)
    trims_mod.apply_trim(tmp_path, "blacksmith", "expired")

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Task blocked" in html
    assert "trim expired" in html
    assert "Start task</button>" not in html


def test_task_page_shows_latest_launch_marker(tmp_path):
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    log_path = launch_dir / "task-20260617-010203.log"
    log_path.write_text("launch output\n")
    (launch_dir / "task-20260617-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["hermes", "-z", "hello"],
        "log": str(log_path),
        "status": "requested",
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Latest launch" in html
    assert "4242" in html
    assert "hermes" in html
    assert "task-20260617-010203.log" in html


def test_task_page_latest_launch_shows_task_controller_loadout(tmp_path):
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260618-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["bash", "-lc", "loop"],
        "log": str(launch_dir / "task-20260618-010203.log"),
        "status": "requested",
        "task_run_id": "task-abc",
        "armor_id": "hermes-default",
        "weapon_id": "flocker-v04",
        "trim_id": "trim-live",
        "temper_id": "temper-live",
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "task-abc" in html
    assert "hermes-default" in html
    assert "flocker-v04" in html
    assert "trim-live" in html
    assert "temper-live" in html


def test_task_page_marks_dead_requested_launch_stale(tmp_path):
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260617-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 999999,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["hermes"],
        "log": str(launch_dir / "task-20260617-010203.log"),
        "status": "requested",
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Latest launch" in html
    assert '<span class="forge-phase-pill">stale</span>' in html
    assert '<span class="forge-phase-pill">running</span>' not in html


def test_stop_task_session_marks_latest_launcher_stopped(monkeypatch, tmp_path):
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    marker = launch_dir / "task-20260617-010203.json"
    marker.write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["hermes"],
        "log": str(launch_dir / "task-20260617-010203.log"),
        "status": "requested",
    }))
    monkeypatch.setattr(app, "_signal_task_pid", lambda pid: pid == 4242)

    assert app._stop_task_session(tmp_path) is True

    data = json.loads(marker.read_text())
    assert data["status"] == "stopped"
    assert data["stopped_at"] > 0


def test_descendant_pids_reads_process_tree(monkeypatch):
    class Result:
        returncode = 0
        stdout = "10 1\n11 10\n12 11\n13 10\n20 1\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())

    assert app._descendant_pids(10) == [11, 12, 13]


def test_signal_task_pid_signals_descendants(monkeypatch):
    calls = []
    live = {100, 101, 102}

    monkeypatch.setattr(app, "_descendant_pids", lambda pid: [101, 102])
    monkeypatch.setattr(app.process_state_mod, "is_pid_live", lambda pid: pid in live)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def fake_killpg(pid, sig):
        calls.append(("killpg", pid, sig))

    def fake_kill(pid, sig):
        calls.append(("kill", pid, sig))
        if pid in live:
            live.remove(pid)

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)

    assert app._signal_task_pid(100) is True

    killed_pids = [pid for kind, pid, _sig in calls if kind == "kill"]
    assert 101 in killed_pids
    assert 102 in killed_pids


def test_task_page_flags_missing_required_channel_output(tmp_path):
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"pid": 999999}))
    (session_dir / "events.jsonl").write_text("")
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260618-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["bash", "-lc", "loop"],
        "log": str(launch_dir / "task-20260618-010203.log"),
        "status": "requested",
        "channel_required": True,
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Structured output required" in html
    assert "missing" in html
    assert "No fact, lead, finding, or evidence channel calls" in html


def test_task_page_flags_prefixed_mcp_channel_aliases(tmp_path):
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"pid": 999999}))
    (session_dir / "events.jsonl").write_text("")
    (session_dir / "transcript.jsonl").write_text(json.dumps({
        "role": "assistant",
        "content": "calling mcp_ara_forge_fact_assert now",
    }) + "\n")
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260618-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["bash", "-lc", "loop"],
        "log": str(launch_dir / "task-20260618-010203.log"),
        "status": "requested",
        "channel_required": True,
        "task_depth": "enum_only",
        "channel_contract": {"required_channels": ["fact.assert"]},
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Structured output required" in html
    assert "blocked" in html
    assert "channel_tool_mismatch" in html
    assert "mcp_ara_forge_fact_assert" in html
    assert "fact.assert" in html


def test_task_page_blocks_missing_output_when_contract_required_list_empty(tmp_path):
    weapons_mod.set_equipped_weapon(tmp_path, "w-task")
    weapon_dir = tmp_path / "weapons" / "w-task"
    (weapon_dir / "chunks").mkdir(parents=True)
    (weapon_dir / "seal.json").write_text("{}")
    (weapon_dir / "chunks" / "probe.json").write_text(json.dumps({
        "id": "probe",
        "title": "Probe",
        "prompt_fragment": "",
        "tool_surface": ["fact.assert", "lead.file", "chunks.emit_tag"],
        "exits": {},
        "is_entry": True,
    }))
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"pid": 999999}))
    (session_dir / "chunk_history.jsonl").write_text(json.dumps({
        "ts": 1,
        "kind": "initialize",
        "to_chunk": "probe",
    }) + "\n")
    (session_dir / "events.jsonl").write_text(json.dumps({
        "ts": 2,
        "kind": "channel.chunks.emit_tag",
        "body": {"channel": "chunks.emit_tag", "status": "ok"},
    }) + "\n")
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260618-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["bash", "-lc", "loop"],
        "log": str(launch_dir / "task-20260618-010203.log"),
        "status": "requested",
        "channel_required": True,
        "task_depth": "safe_mutation",
        "weapon_id": "w-task",
        "channel_contract": {"required_channels": []},
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Structured output required" in html
    assert "structured_channel_output_missing" in html
    assert "fact.assert" in html
    assert "lead.file" in html


def test_task_page_accepts_required_real_channel_output_for_enum(tmp_path):
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"pid": 999999}))
    (session_dir / "events.jsonl").write_text(json.dumps({
        "ts": 1,
        "kind": "channel.fact.assert",
        "body": {"channel": "fact.assert", "status": "ok"},
    }) + "\n")
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260618-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["bash", "-lc", "loop"],
        "log": str(launch_dir / "task-20260618-010203.log"),
        "status": "requested",
        "channel_required": True,
        "task_depth": "enum_only",
        "channel_contract": {"required_channels": ["fact.assert"]},
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Structured output required" in html
    assert "ok" in html
    assert "structured_channel_output_missing" not in html


def test_task_page_blocks_passive_task_without_chunk_transition(tmp_path):
    weapons_mod.set_equipped_weapon(tmp_path, "w-task")
    weapon_dir = tmp_path / "weapons" / "w-task"
    (weapon_dir / "chunks").mkdir(parents=True)
    (weapon_dir / "seal.json").write_text("{}")
    (weapon_dir / "chunks" / "enum.json").write_text(json.dumps({
        "id": "enum",
        "title": "Enum",
        "prompt_fragment": "",
        "tool_surface": ["fact.assert", "chunks.emit_tag"],
        "exits": {"<go>": {"tag": "<go>", "next_chunk_id": "probe"}},
        "is_entry": True,
    }))
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"pid": 999999}))
    (session_dir / "chunk_history.jsonl").write_text(json.dumps({
        "ts": 1,
        "kind": "initialize",
        "to_chunk": "enum",
    }) + "\n")
    (session_dir / "events.jsonl").write_text(json.dumps({
        "ts": 2,
        "kind": "channel.fact.assert",
        "body": {"channel": "fact.assert", "status": "ok"},
    }) + "\n")
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260618-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["bash", "-lc", "loop"],
        "log": str(launch_dir / "task-20260618-010203.log"),
        "status": "requested",
        "channel_required": True,
        "task_depth": "passive_probe",
        "weapon_id": "w-task",
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Structured output required" in html
    assert "blocked" in html
    assert "chunk_transition_missing" in html
    assert "current chunk" in html
    assert "enum" in html


def test_task_page_treats_missing_task_depth_as_full_task(tmp_path):
    weapons_mod.set_equipped_weapon(tmp_path, "w-task")
    weapon_dir = tmp_path / "weapons" / "w-task"
    (weapon_dir / "chunks").mkdir(parents=True)
    (weapon_dir / "seal.json").write_text("{}")
    (weapon_dir / "chunks" / "enum.json").write_text(json.dumps({
        "id": "enum",
        "title": "Enum",
        "prompt_fragment": "",
        "tool_surface": ["fact.assert", "chunks.emit_tag"],
        "exits": {"<surface_mapped>": {"tag": "<surface_mapped>", "next_chunk_id": "probe"}},
        "is_entry": True,
    }))
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"pid": 999999}))
    (session_dir / "chunk_history.jsonl").write_text(json.dumps({
        "ts": 1,
        "kind": "initialize",
        "to_chunk": "enum",
    }) + "\n")
    (session_dir / "events.jsonl").write_text(json.dumps({
        "ts": 2,
        "kind": "channel.fact.assert",
        "body": {"channel": "fact.assert", "status": "ok"},
    }) + "\n")
    launch_dir = tmp_path / "task-launches"
    launch_dir.mkdir()
    (launch_dir / "task-20260618-010203.json").write_text(json.dumps({
        "ts": 1_788_888_000.0,
        "pid": 4242,
        "command": ["python", "-m", "ara.cli", "launch"],
        "worker_command": ["bash", "-lc", "loop"],
        "log": str(launch_dir / "task-20260618-010203.log"),
        "status": "requested",
        "channel_required": True,
        "weapon_id": "w-task",
    }))

    html = forge_v1_views.render_engagement(tmp_path, "tok")

    assert "Structured output required" in html
    assert "blocked" in html
    assert "chunk_transition_missing" in html
    assert "unspecified" in html
