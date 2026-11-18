"""Evidence provenance index: record + load + verify round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara import evidence


def _session(tmp: Path) -> Path:
    s = tmp / "sessions" / "sess123"
    s.mkdir(parents=True)
    return s


def test_record_and_load_round_trip(tmp_path: Path) -> None:
    session = _session(tmp_path)
    artifact = session / "out.txt"
    artifact.write_text("hello\n")
    rec = evidence.record_artifact(
        session, tool_use_id="t1", tool_name="oc:write",
        action="write", artifact_path=str(artifact), hint="wrote report.txt",
    )
    assert rec.artifact_sha256
    assert rec.size_bytes == 6
    assert rec.hint == "wrote report.txt"
    loaded = evidence.load(session)
    assert len(loaded) == 1
    assert loaded[0].capture_id == rec.capture_id


def test_verify_untampered(tmp_path: Path) -> None:
    session = _session(tmp_path)
    artifact = session / "a.bin"
    artifact.write_bytes(b"\x00\x01\x02" * 1024)
    rec = evidence.record_artifact(
        session, tool_use_id="t2", tool_name="oc:bash",
        action="capture", artifact_path=str(artifact),
    )
    assert evidence.verify(rec) is True


def test_verify_detects_tamper(tmp_path: Path) -> None:
    session = _session(tmp_path)
    artifact = session / "mutable.txt"
    artifact.write_text("original")
    rec = evidence.record_artifact(
        session, tool_use_id="t3", tool_name="oc:write",
        action="write", artifact_path=str(artifact),
    )
    artifact.write_text("tampered")
    assert evidence.verify(rec) is False


def test_verify_detects_missing(tmp_path: Path) -> None:
    session = _session(tmp_path)
    artifact = session / "gone.txt"
    artifact.write_text("x")
    rec = evidence.record_artifact(
        session, tool_use_id="t4", tool_name="oc:write",
        action="write", artifact_path=str(artifact),
    )
    artifact.unlink()
    assert evidence.verify(rec) is False


def test_verify_session_summary(tmp_path: Path) -> None:
    session = _session(tmp_path)
    good = session / "good.txt"
    good.write_text("g")
    bad = session / "bad.txt"
    bad.write_text("original")
    missing = session / "missing.txt"
    missing.write_text("m")

    r_good = evidence.record_artifact(
        session, tool_use_id="a", tool_name="oc:write",
        action="write", artifact_path=str(good),
    )
    r_bad = evidence.record_artifact(
        session, tool_use_id="b", tool_name="oc:write",
        action="write", artifact_path=str(bad),
    )
    r_missing = evidence.record_artifact(
        session, tool_use_id="c", tool_name="oc:write",
        action="write", artifact_path=str(missing),
    )
    bad.write_text("tampered")
    missing.unlink()

    rep = evidence.verify_session(session)
    assert rep["total"] == 3
    assert r_good.capture_id in rep["ok"]
    assert r_bad.capture_id in rep["tampered"]
    assert r_missing.capture_id in rep["missing"]


def test_jsonl_format_is_append_only(tmp_path: Path) -> None:
    session = _session(tmp_path)
    f1 = session / "f1.txt"
    f1.write_text("1")
    f2 = session / "f2.txt"
    f2.write_text("2")
    evidence.record_artifact(
        session, tool_use_id="x", tool_name="oc:write",
        action="write", artifact_path=str(f1),
    )
    evidence.record_artifact(
        session, tool_use_id="y", tool_name="oc:write",
        action="write", artifact_path=str(f2),
    )
    lines = (session / "evidence_index.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert all(json.loads(l)["tool_name"] == "oc:write" for l in lines)


def test_find_by_tool_use(tmp_path: Path) -> None:
    session = _session(tmp_path)
    a = session / "a"
    a.write_text("a")
    b = session / "b"
    b.write_text("b")
    evidence.record_artifact(session, tool_use_id="t1",
                             tool_name="oc:write", action="write",
                             artifact_path=str(a))
    evidence.record_artifact(session, tool_use_id="t1",
                             tool_name="oc:write", action="write",
                             artifact_path=str(b))
    matches = list(evidence.find_by_tool_use(session, "t1"))
    assert len(matches) == 2


def test_missing_file_raises(tmp_path: Path) -> None:
    session = _session(tmp_path)
    with pytest.raises(FileNotFoundError):
        evidence.record_artifact(
            session, tool_use_id="x", tool_name="oc:write",
            action="write", artifact_path=str(session / "nope"),
        )
