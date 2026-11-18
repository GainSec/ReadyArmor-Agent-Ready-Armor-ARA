"""net:connection evidence materialized lazily from the seccomp-egress ledger."""

from __future__ import annotations

import json
from pathlib import Path

from ara.runtime.binder.net_capture import read_connections, materialize_net_connections
from ara.runtime.binder.api import EvidenceBinder


def _ledger(state_root: Path, records: list[dict]) -> None:
    (state_root / "logs").mkdir(parents=True, exist_ok=True)
    from ara import debug_log
    p = state_root / "logs" / debug_log.DEFAULT_LOG_NAME
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _decision(sid: str, host: str, port: int, verdict: str, ts: float, pid: int = 100) -> dict:
    return {
        "component": "seccomp-egress", "event": "decision", "session_id": sid, "ts": ts,
        "fields": {"enforcer": "seccomp", "op": "connect", "family": 2, "host": host,
                   "port": port, "verdict": verdict, "reason": f"R:{verdict}", "pid": pid},
    }


def test_read_connections_filters_by_session_and_host(tmp_path: Path) -> None:
    _ledger(tmp_path, [
        _decision("s1", "192.168.18.9", 80, "allow", 10.0),
        _decision("s1", "192.168.18.1", 80, "deny", 11.0),
        _decision("s2", "192.168.18.9", 80, "allow", 12.0),   # other session
        {"component": "other", "event": "x"},                  # noise
    ])
    got = read_connections(tmp_path, "s1")
    assert len(got) == 2
    only = read_connections(tmp_path, "s1", host="192.168.18.9")
    assert len(only) == 1 and only[0]["verdict"] == "allow"


def test_read_connections_time_window(tmp_path: Path) -> None:
    _ledger(tmp_path, [
        _decision("s1", "h", 1, "allow", 5.0),
        _decision("s1", "h", 2, "allow", 15.0),
        _decision("s1", "h", 3, "allow", 25.0),
    ])
    got = read_connections(tmp_path, "s1", since=10.0, until=20.0)
    assert [c["port"] for c in got] == [2]


def test_materialize_binds_net_connection_evidence(tmp_path: Path) -> None:
    state = tmp_path
    sd = tmp_path / "sessions" / "s1"
    sd.mkdir(parents=True)
    _ledger(state, [
        _decision("s1", "192.168.18.9", 80, "allow", 10.0),
        _decision("s1", "192.168.18.9", 3000, "allow", 11.0),
        _decision("s1", "192.168.18.1", 80, "deny", 12.0),
    ])
    ids = materialize_net_connections(sd, state, "s1", host="192.168.18.9", activation_id="act-1")
    assert len(ids) == 2

    binder = EvidenceBinder(sd)
    r = binder.get_record(ids[0])
    assert r is not None and r.source == "net:connection"
    assert r.source_metadata["host"] == "192.168.18.9"
    assert r.captured_during_activation_id == "act-1"
    binder.close()


def test_materialize_idempotent(tmp_path: Path) -> None:
    state = tmp_path
    sd = tmp_path / "sessions" / "s1"
    sd.mkdir(parents=True)
    _ledger(state, [_decision("s1", "h", 80, "allow", 10.0)])
    a = materialize_net_connections(sd, state, "s1")
    b = materialize_net_connections(sd, state, "s1")
    assert a == b and len(a) == 1  # same content hash → same id, no dup


def test_materialize_no_ledger_no_raise(tmp_path: Path) -> None:
    sd = tmp_path / "sessions" / "s1"
    sd.mkdir(parents=True)
    assert materialize_net_connections(sd, tmp_path, "s1") == []
