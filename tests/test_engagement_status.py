"""Tests for ``ara engagement status``."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from ara import weapons as weapons_mod


def _seed_session(state_root: Path, *, session_id: str = "sess-test") -> Path:
    sd = state_root / "sessions" / session_id
    sd.mkdir(parents=True)
    return sd


def _seed_chunk_history(sd: Path, *transitions: dict) -> None:
    p = sd / "chunk_history.jsonl"
    p.write_text("\n".join(json.dumps(t) for t in transitions) + "\n")


def _seed_activations(sd: Path, *acts: dict) -> None:
    p = sd / "activations.jsonl"
    p.write_text("\n".join(json.dumps(a) for a in acts) + "\n")


def _seed_events(sd: Path, *events: dict) -> None:
    p = sd / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _seed_jsonl(sd: Path, name: str, *rows: dict) -> None:
    (sd / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _seed_evidence_db(sd: Path, n: int = 0) -> None:
    db_path = sd / "evidence" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE evidence (id TEXT PRIMARY KEY, sha256 TEXT)"
        )
        for i in range(n):
            conn.execute(
                "INSERT INTO evidence (id, sha256) VALUES (?, ?)",
                (f"e:{i:010d}", f"{i:064d}"),
            )
        conn.commit()
    finally:
        conn.close()


# ---------- empty / no-session ----------

def test_status_with_empty_state_root(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["equipped_weapon"] is None
    assert out["session_id"] is None
    # No engagement-specific keys when there's no session
    assert "chunk" not in out


def test_status_picks_most_recent_session(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    older = _seed_session(tmp_path, session_id="old")
    newer = _seed_session(tmp_path, session_id="new")
    # Force "new" to have a strictly later mtime
    (newer / "stamp").write_text("x")
    time.sleep(0.01)
    older_stat = older.stat().st_mtime
    newer_stat = newer.stat().st_mtime
    assert newer_stat >= older_stat
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "new"


def test_status_explicit_session_override(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    _seed_session(tmp_path, session_id="A")
    _seed_session(tmp_path, session_id="B")
    rc = main([
        "engagement", "--state-root", str(tmp_path), "status",
        "--session", "A",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "A"


def test_status_unknown_session_falls_back_to_no_session(
    tmp_path: Path, capsys,
) -> None:
    from ara.cli import main
    _seed_session(tmp_path, session_id="real")
    rc = main([
        "engagement", "--state-root", str(tmp_path), "status",
        "--session", "ghost",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] is None


# ---------- equipped-weapon surface ----------

def test_status_shows_equipped_weapon(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["equipped_weapon"] == "wstg-v1"


# ---------- chunk + activation summary ----------

def test_status_summarizes_chunk_history(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_chunk_history(
        sd,
        {"ts": 1.0, "kind": "initialize", "to_chunk": "enum",
         "activation_id": "a1"},
        {"ts": 2.0, "kind": "swap", "from_chunk": "enum", "to_chunk": "probe",
         "tag": "<go>", "caller": "agent", "activation_id": "a2"},
        {"ts": 3.0, "kind": "swap", "from_chunk": "probe", "to_chunk": "writeup",
         "tag": "<readied>", "caller": "agent", "activation_id": "a3"},
    )
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["chunk"]["current_chunk"] == "writeup"
    assert len(out["chunk"]["transitions"]) == 3
    assert out["chunk"]["transitions"][-1]["to"] == "writeup"


def test_status_recent_flag_caps_transitions(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_chunk_history(
        sd,
        *[
            {"ts": float(i), "kind": "swap", "from_chunk": f"c{i-1}",
             "to_chunk": f"c{i}", "tag": "<go>", "caller": "agent",
             "activation_id": f"a{i}"}
            for i in range(20)
        ],
    )
    rc = main([
        "engagement", "--state-root", str(tmp_path), "status",
        "--recent", "5",
    ])
    out = json.loads(capsys.readouterr().out)
    assert len(out["chunk"]["transitions"]) == 5


def test_status_summarizes_activations(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_activations(
        sd,
        {"ts": 1.0, "activation_id": "a1", "reason": "initial",
         "chunk_id": "enum"},
        {"ts": 2.0, "activation_id": "a2", "reason": "chunk_swap",
         "chunk_id": "probe"},
    )
    (sd / "activation.txt").write_text("a2")
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    out = json.loads(capsys.readouterr().out)
    assert out["activations"]["count"] == 2
    assert out["activations"]["current"] == "a2"


# ---------- channel call counts ----------

def test_status_counts_channel_calls(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_events(
        sd,
        {"ts": 1, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok"}, "prev_hash": "0"*64},
        {"ts": 2, "kind": "channel.fact.assert",
         "body": {"channel": "fact.assert", "status": "ok"}, "prev_hash": "0"*64},
        {"ts": 3, "kind": "channel.lead.file",
         "body": {"channel": "lead.file", "status": "ok"}, "prev_hash": "0"*64},
        {"ts": 4, "kind": "channel.lead.file",
         "body": {"channel": "lead.file", "status": "rejected",
                  "reason": "x"}, "prev_hash": "0"*64},
        {"ts": 5, "kind": "heartbeat", "body": {}, "prev_hash": "0"*64},
    )
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    out = json.loads(capsys.readouterr().out)
    chs = out["channels"]
    assert chs["total"] == 4
    assert chs["by_channel"]["fact.assert"] == 2
    assert chs["by_channel"]["lead.file"] == 2
    assert chs["rejections_by_channel"].get("lead.file") == 1


# ---------- finding / lead / anomaly counts ----------

def test_status_counts_findings_with_latest_wins(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd, "findings.jsonl",
        {"id": "F:1", "status": "draft", "severity_name": "high"},
        {"id": "F:1", "status": "signed_off", "severity_name": "high"},
        {"id": "F:2", "status": "draft", "severity_name": "low"},
    )
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    out = json.loads(capsys.readouterr().out)
    f = out["findings"]
    assert f["total"] == 2  # latest-wins per id
    assert f["by_status"]["signed_off"] == 1
    assert f["by_status"]["draft"] == 1


def test_status_counts_leads(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd, "leads.jsonl",
        {"id": "L:1", "status": "open", "priority_computed": "high"},
        {"id": "L:1", "status": "investigating", "priority_computed": "high"},
        {"id": "L:2", "status": "dismissed_operator", "priority_computed": "low"},
    )
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    out = json.loads(capsys.readouterr().out)
    leads = out["leads"]
    assert leads["total"] == 2
    assert leads["by_status"]["investigating"] == 1
    assert leads["by_priority"]["high"] == 1


def test_status_counts_anomalies(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd, "anomalies.jsonl",
        {"id": "A:1", "status": "pending"},
        {"id": "A:2", "status": "promoted_to_lead"},
        {"id": "A:3", "status": "pending"},
    )
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    out = json.loads(capsys.readouterr().out)
    a = out["anomalies"]
    assert a["total"] == 3
    assert a["pending"] == 2


def test_status_counts_facts(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_jsonl(
        sd, "facts.jsonl",
        {"id": "f:1"}, {"id": "f:2"}, {"id": "f:3"},
    )
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    out = json.loads(capsys.readouterr().out)
    assert out["facts"]["total"] == 3


def test_status_counts_evidence_via_sqlite(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    sd = _seed_session(tmp_path)
    _seed_evidence_db(sd, n=4)
    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    out = json.loads(capsys.readouterr().out)
    assert out["evidence"]["total"] == 4


# ---------- end-to-end shape ----------

def test_status_full_shape(tmp_path: Path, capsys) -> None:
    """One round-trip with everything seeded; assert top-level keys
    are stable so downstream tooling can rely on the schema."""
    from ara.cli import main
    weapons_mod.set_equipped_weapon(tmp_path, "wstg-v1")
    sd = _seed_session(tmp_path)
    _seed_chunk_history(
        sd,
        {"ts": 1.0, "kind": "initialize", "to_chunk": "enum",
         "activation_id": "a1"},
    )
    _seed_activations(
        sd,
        {"ts": 1.0, "activation_id": "a1", "reason": "initial",
         "chunk_id": "enum"},
    )
    _seed_events(sd, {"ts": 1, "kind": "heartbeat", "body": {}})
    _seed_jsonl(sd, "facts.jsonl", {"id": "f:1"})
    _seed_jsonl(sd, "leads.jsonl", {"id": "L:1", "status": "open"})
    _seed_jsonl(sd, "findings.jsonl",
                 {"id": "F:1", "status": "draft", "severity_name": "low"})
    _seed_jsonl(sd, "anomalies.jsonl", {"id": "A:1", "status": "pending"})
    _seed_evidence_db(sd, n=1)

    rc = main(["engagement", "--state-root", str(tmp_path), "status"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    expected_keys = {
        "state_root", "equipped_weapon", "session_dir", "session_id",
        "chunk", "activations", "channels",
        "findings", "leads", "anomalies", "facts", "evidence",
    }
    assert expected_keys.issubset(out.keys())
    assert out["equipped_weapon"] == "wstg-v1"
    assert out["chunk"]["current_chunk"] == "enum"
    assert out["facts"]["total"] == 1
    assert out["leads"]["total"] == 1
    assert out["findings"]["total"] == 1
    assert out["anomalies"]["total"] == 1
    assert out["evidence"]["total"] == 1
