"""End-to-end test for the Tier-A transcript channel.

Spawns a real supervisor + launches a child python that uses
``ara.clients.transcript.intercept`` to send a couple of turns. Verifies
the supervisor wrote the transcript file with the expected records.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ara.clients.sitecustomize import _TranscriptStream, _safe_dump
from ara.clients import transcript

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_intercept_roundtrip(short_tmp: Path):
    state = short_tmp / ".ara"
    state.mkdir()
    walls = short_tmp / "WALLS.md"
    walls.write_text("## deny_path\n")  # empty rules, just need a file

    # Child program: import the ARA client, send request + response turns,
    # then print the decisions so the test can verify the response shape.
    child_src = (
        "import json, sys\n"
        "from ara.clients.transcript import intercept\n"
        "d1 = intercept('request', {'system':'be safe','messages':[{'role':'user','content':'hi'}]}, model='gpt-5-codex')\n"
        "d2 = intercept('response', {'content':[{'type':'text','text':'hello'}], 'usage':{'input_tokens':5,'output_tokens':3}})\n"
        "print(json.dumps({'d1':d1, 'd2':d2}))\n"
    )

    env = {
        # Replace os.environ so the child's PYTHONPATH is set deterministically.
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    cmd = [
        sys.executable, "-m", "ara.cli",
        "launch", "--state-root", str(state), "--walls", str(walls),
        "--", sys.executable, "-c", child_src,
    ]
    result = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=20,
    )
    # Parse the child's printed decision dict from stdout.
    out = result.stdout.strip().splitlines()
    decision_line = next((l for l in out if l.startswith("{")), None)
    assert decision_line, f"no decision json in stdout; stderr=\n{result.stderr}"
    decisions = json.loads(decision_line)
    assert decisions["d1"]["action"] == "allow"
    assert decisions["d2"]["action"] == "allow"

    # Locate the session and verify the transcript was written.
    sessions = list((state / "sessions").iterdir())
    assert len(sessions) == 1
    transcript = sessions[0] / "transcript.jsonl"
    assert transcript.exists(), "supervisor never wrote transcript.jsonl"
    lines = transcript.read_text().splitlines()
    assert len(lines) == 2, lines
    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])
    assert rec1["role"] == "request"
    assert rec1["payload"]["system"] == "be safe"
    assert rec1["model"] == "gpt-5-codex"
    assert rec2["role"] == "response"
    # Hash chain advances.
    assert rec2["prev_hash"] != rec1["prev_hash"]


def test_transcript_stream_records_consumed_chunks():
    calls = []

    def intercept(role, payload, *, model=None):
        calls.append({"role": role, "payload": payload, "model": model})

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "hidden-shape"}}]},
        {"choices": [{"delta": {"content": "visible"}}]},
    ]
    wrapped = _TranscriptStream(iter(chunks), intercept, "qwen-test")

    assert list(wrapped) == chunks
    assert len(calls) == 1
    assert calls[0]["role"] == "response"
    assert calls[0]["model"] == "qwen-test"
    assert calls[0]["payload"]["stream"] is True
    assert calls[0]["payload"]["complete"] is True
    assert calls[0]["payload"]["reason"] == "exhausted"
    assert calls[0]["payload"]["chunks"] == chunks


def test_safe_dump_prefers_model_dump():
    class FakeChunk:
        def model_dump(self, mode="python"):
            assert mode == "json"
            return {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "hidden-shape",
                            "content": "visible",
                        }
                    }
                ]
            }

    assert _safe_dump(FakeChunk()) == {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "hidden-shape",
                    "content": "visible",
                }
            }
        ]
    }


def test_query_sends_supervisor_query(monkeypatch):
    sent = []
    monkeypatch.setenv("ARA_TRANSCRIPT_PROTOCOL", "v1")

    class FakeSocket:
        def sendall(self, data):
            sent.append(data.decode("utf-8"))

    monkeypatch.setattr(transcript, "_connect", lambda: FakeSocket())
    monkeypatch.setattr(
        transcript,
        "_readline",
        lambda _sock: json.dumps({"seq": 1001, "verdict": "allow"}),
    )
    monkeypatch.setattr(transcript, "_next_seq", lambda: 1001)

    decision = transcript.query(
        op="exec.spawn",
        command="python3",
        args=["probe.py"],
        cwd="/tmp",
    )

    assert decision == {"seq": 1001, "verdict": "allow"}
    payload = json.loads(sent[0])
    assert payload["type"] == "query"
    assert payload["op"] == "exec.spawn"
    assert payload["command"] == "python3"
    assert payload["args"] == ["probe.py"]


def test_intercept_fails_closed_during_run_task(monkeypatch):
    monkeypatch.setenv("ARA_TRANSCRIPT_PROTOCOL", "v1")
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")
    monkeypatch.setattr(transcript, "_connect", lambda: None)

    decision = transcript.intercept("request", {"messages": []}, model="qwen")

    assert decision["action"] == "deny"
    assert decision["reason"] == "ARA_INTERCEPT_UNAVAILABLE"
