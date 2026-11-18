from __future__ import annotations

from pathlib import Path
import asyncio

from ara import task_action_gate
from ara import task_session
from ara.audit import JsonlWriter
from ara.session import create_session
from ara.supervisor import Supervisor
from ara.walls import Walls


def _write_loadout(state: Path) -> None:
    (state / "armors" / "blacksmith" / "absolutes").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "absolutes" / "default.md").write_text("armor\n")
    (state / "weapons" / "w-task" / "chunks").mkdir(parents=True)
    (state / "weapons" / "w-task" / "weapon.json").write_text("{}")
    (state / "weapons" / "w-task" / "seal.json").write_text("{}")
    (state / "armors" / "blacksmith" / "trims" / "trim-a").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state / "armors" / "blacksmith" / "temper" / "proposals").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "temper" / "proposals" / "temp-a.json").write_text("{}")


def _run(state: Path) -> task_session.TaskRunSession:
    _write_loadout(state)
    return task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )


def test_gate_allows_first_target_action_then_denies_until_commit(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    first = task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    )
    assert first is None

    second = task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    )
    assert second is not None
    assert second.verdict == "deny"
    assert second.reason == "TASK_COMMIT_REQUIRED"

    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    loaded.record_commit("fact.assert")

    third = task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    )
    assert third is None


def test_evidence_lookup_does_not_clear_task_debt(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    ) is None

    assert task_action_gate.record_commit(
        tmp_path, "evidence.search", environ=env,
    ) is False
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED
    assert loaded.actions_since_commit == 1
    assert loaded.accepted_commit_count == 0

    assert task_action_gate.record_commit(
        tmp_path, "fact.assert", environ=env,
    ) is True
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.actions_since_commit == 0
    assert loaded.accepted_commit_count == 1


def test_gate_denies_target_action_when_branch_update_required(tmp_path: Path) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        task_id="task-test",
    )
    env = {"ARA_TASK_RUN_ID": run.task_id}

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    ) is None

    assert task_action_gate.record_commit(
        tmp_path, "fact.assert", environ=env,
    ) is True
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.BRANCH_REQUIRED

    decision = task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    )
    assert decision is not None
    assert decision.verdict == "deny"
    assert decision.reason == "TASK_BRANCH_REQUIRED"

    assert task_action_gate.record_commit(
        tmp_path, "lead.file", environ=env,
    ) is True
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK


def test_task_debt_mcp_tools_require_chunk_transition_when_chunk_gated(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    env = {
        "ARA_TASK_RUN_ID": run.task_id,
        "ARA_TASK_SUPERVISOR": "armory-hermes-loop",
    }
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})

    assert task_action_gate.task_debt_mcp_tool_names(
        tmp_path, environ=env,
    ) == ("mcp_ara_forge_chunks_emit_tag",)


def test_chunk_gated_lead_file_commit_is_rejected(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    env = {
        "ARA_TASK_RUN_ID": run.task_id,
        "ARA_TASK_SUPERVISOR": "armory-hermes-loop",
    }
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})

    assert task_action_gate.record_commit(
        tmp_path, "lead.file", environ=env,
    ) is False

    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.BRANCH_REQUIRED
    assert loaded.accepted_branch_update_count == 1
    assert loaded.branch_updates_since_chunk_transition == 1


def test_chunk_gated_chunks_emit_tag_commit_is_accepted(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    env = {
        "ARA_TASK_RUN_ID": run.task_id,
        "ARA_TASK_SUPERVISOR": "armory-hermes-loop",
    }
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})

    assert task_action_gate.record_commit(
        tmp_path, "chunks.emit_tag", environ=env,
    ) is True

    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.accepted_branch_update_count == 2
    assert loaded.branch_updates_since_chunk_transition == 0


def test_gate_ignores_internal_ara_launcher(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    for _ in range(3):
        decision = task_action_gate.evaluate_query(
            tmp_path,
            {
                "op": "exec.spawn",
                "command": "timeout",
                "args": ["90", "/usr/local/bin/hermes", "--continue", "ara-task-123"],
            },
            environ=env,
        )
        assert decision is None

    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 0


def test_gate_ignores_task_control_plane_bootstrap(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    bootstrap = [
        {
            "op": "exec.spawn",
            "command": "python3.9",
            "args": ["-m", "ara.cli", "forge-mcp"],
        },
        {
            "op": "exec.spawn",
            "command": "python3.9",
            "args": ["-m", "ara.cli", "task-repair-commit"],
        },
        {
            "op": "exec.spawn",
            "command": str(tmp_path / ".local/bin/hermes"),
            "args": [
                "--accept-hooks",
                "-z",
                "COMMIT_REQUIRED. You have taken a target action without filing a valid structured ARA channel.",
            ],
        },
        {
            "op": "exec.spawn",
            "command": str(tmp_path / ".local/bin/hermes"),
            "args": [
                "--accept-hooks",
                "-z",
                "BRANCH_REQUIRED. The investigation ledger needs a branch update before any more target action.",
            ],
        },
        {
            "op": "exec.spawn",
            "command": "file",
            "args": ["-b", str(tmp_path / ".local/share/uv/python/cpython-3.11.14-linux-x86_64-gnu/bin/python3.11")],
        },
        {
            "op": "exec.spawn",
            "command": "python3",
            "args": ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
        },
        {
            "op": "exec.spawn",
            "command": "python3",
            "args": ["-m", "pip", "--version"],
        },
        {
            "op": "exec.spawn",
            "command": "python3",
            "args": ["-c", "import sys, os;stdlib = os.path.dirname(os.__file__);marker = os.path.join(stdlib, 'EXTERNALLY-MANAGED');print('yes' if os.path.exists(marker) else 'no')"],
        },
        {
            "op": "exec.spawn",
            "command": "python3.9",
            "args": [
                "-c",
                "import json,os,pathlib;root=os.environ.get('ARA_STATE_ROOT');"
                "sid=os.environ.get('ARA_SESSION_ID');"
                "p=pathlib.Path(root or '.')/'sessions'/str(sid or '')/'events.jsonl';"
                "n=0;\nif p.is_file():\n"
                "    f=p.open()\n"
                "    for line in f:\n"
                "        r=json.loads(line); b=r.get('body') or {}\n"
                "        if str(r.get('kind') or '').startswith('channel.') "
                "and b.get('status') != 'rejected': n += 1\n"
                "print(n)",
            ],
        },
        {
            "op": "exec.spawn",
            "command": "python3.9",
            "args": [
                "-c",
                "import json,os,pathlib;root=os.environ.get('ARA_STATE_ROOT');"
                "tid=os.environ.get('ARA_TASK_RUN_ID');"
                "p=pathlib.Path(root or '.')/'task-runs'/str(tid or '')/'session.json';"
                "\nif root and tid and p.is_file():\n"
                "    print(json.loads(p.read_text()).get('phase','work'))\n"
                "else:\n"
                "    print('work')",
            ],
        },
        {
            "op": "exec.spawn",
            "command": "pip",
            "args": ["--version"],
        },
        {
            "op": "exec.spawn",
            "command": "uname",
            "args": ["-p"],
        },
        {"op": "net.connect", "host": "openrouter.ai", "port": 443},
        {"op": "net.connect", "host": "api.openai.com", "port": 443},
        {"op": "net.connect", "host": "api.anthropic.com", "port": 443},
        {
            "op": "exec.spawn",
            "command": "/usr/bin/bash",
            "args": [
                "-l",
                "-c",
                "export -p > /tmp/hermes-snap-abc.sh\npwd -P > /tmp/hermes-cwd-abc.txt\n",
            ],
        },
    ]
    for msg in bootstrap:
        assert task_action_gate.evaluate_query(tmp_path, msg, environ=env) is None
        assert task_action_gate.is_task_control_plane_action(msg) is True

    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 0
    assert loaded.phase == task_session.TaskPhase.WORK


def test_gate_still_counts_exec_after_non_budgeted_net_connect(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "net.connect", "host": "example.test", "port": 443},
        environ=env,
    ) is None

    decision = task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    )
    assert decision is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED


def test_net_connect_does_not_spend_task_budget_while_working(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "net.connect", "host": "example.test", "port": 443},
        environ=env,
    ) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.actions_since_commit == 0


def test_gate_counts_hermes_shell_target_action(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    first = task_action_gate.evaluate_query(
        tmp_path,
        {
            "op": "exec.spawn",
            "command": "/usr/bin/bash",
            "args": ["-l", "-c", "python3 -c 'fetch target'"],
        },
        environ=env,
    )
    assert first is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED
    assert loaded.actions_since_commit == 1

    second = task_action_gate.evaluate_query(
        tmp_path,
        {
            "op": "exec.spawn",
            "command": "/usr/bin/bash",
            "args": ["-l", "-c", "python3 -c 'fetch target again'"],
        },
        environ=env,
    )
    assert second is not None
    assert second.reason == "TASK_COMMIT_REQUIRED"


def test_net_connect_after_exec_spawn_does_not_spend_task_budget(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "/usr/bin/bash", "args": ["-l", "-c", "python3 -c 'fetch'"]},
        environ=env,
    ) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "net.connect", "host": "example.test", "port": 443},
        environ=env,
    ) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED


def test_http_request_after_exec_spawn_does_not_spend_task_budget(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "/usr/bin/bash", "args": ["-l", "-c", "python3 -c 'fetch'"]},
        environ=env,
    ) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "http.request", "url": "https://example.test/", "method": "GET"},
        environ=env,
    ) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED


def test_task_shell_transport_is_target_action(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}
    msg = {
        "op": "exec.spawn",
        "command": "/usr/bin/bash",
        "args": ["-l", "-c", "python3 -c 'fetch target'"],
    }
    assert task_action_gate.is_task_shell_transport_action(msg) is True
    assert task_action_gate.is_task_control_plane_action(msg) is False
    assert task_action_gate.evaluate_query(tmp_path, msg, environ=env) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED


def test_hermes_shell_transport_counts_as_target_action(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}
    shell_transport = {
        "op": "exec.spawn",
        "command": "/usr/bin/bash",
        "args": [
            "-c",
            "builtin cd -- /work || exit 126\n"
            "eval 'python3 -c \"fetch target\"'\n"
        ],
    }

    assert task_action_gate.evaluate_query(
        tmp_path, shell_transport, environ=env,
    ) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "net.connect", "host": "example.test", "port": 443},
        environ=env,
    ) is None
    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.actions_since_commit == 1
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED


def test_browser_nav_still_denied_when_commit_required(tmp_path: Path) -> None:
    run = _run(tmp_path)
    env = {"ARA_TASK_RUN_ID": run.task_id}

    assert task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ=env,
    ) is None

    decision = task_action_gate.evaluate_query(
        tmp_path,
        {"op": "browser.nav", "url": "https://example.test/"},
        environ=env,
    )
    assert decision is not None
    assert decision.verdict == "deny"
    assert decision.reason == "TASK_COMMIT_REQUIRED"


def test_gate_denies_missing_task_session_in_run_task(tmp_path: Path) -> None:
    decision = task_action_gate.evaluate_query(
        tmp_path,
        {"op": "exec.spawn", "command": "python3", "args": ["probe.py"]},
        environ={"ARA_TASK_RUN_ID": "missing"},
    )

    assert decision is not None
    assert decision.verdict == "deny"
    assert decision.reason == "TASK_SESSION_MISSING"


def test_supervisor_query_uses_task_action_gate(monkeypatch, tmp_path: Path) -> None:
    run = _run(tmp_path)
    monkeypatch.setenv("ARA_TASK_RUN_ID", run.task_id)
    session = create_session(state_root=tmp_path)
    sup = Supervisor(
        session=session,
        walls=Walls(path_rules=()),
        server=None,  # type: ignore[arg-type]
        events=JsonlWriter(session.events_path),
        policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
        state_root=tmp_path,
    )

    first = asyncio.run(sup._on_query({
        "type": "query",
        "seq": 1,
        "op": "exec.spawn",
        "command": "python3",
        "args": ["probe.py"],
    }))
    assert first["verdict"] == "allow"

    second = asyncio.run(sup._on_query({
        "type": "query",
        "seq": 2,
        "op": "exec.spawn",
        "command": "python3",
        "args": ["probe.py"],
    }))
    assert second["verdict"] == "deny"
    assert second["reason"] == "TASK_COMMIT_REQUIRED"
