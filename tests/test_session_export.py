"""Audit #17 — bundle a session's JSONL streams + containment manifest
into a single gzipped tarball."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from ara.armory.app import (
    build_session_export, build_session_export_signature,
)


def _mk_session(state: Path, sid: str) -> Path:
    sdir = state / "sessions" / sid
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps({"session_id": sid}))
    (sdir / "events.jsonl").write_text('{"kind":"hi"}\n')
    (sdir / "policy_hits.jsonl").write_text('{"kind":"hit"}\n')
    (sdir / "transcript.jsonl").write_text('{"role":"request"}\n')
    (sdir / "contain").mkdir()
    (sdir / "contain" / "README").write_text("contained\n")
    (sdir / "contain" / "sub").mkdir()
    (sdir / "contain" / "sub" / "nested.txt").write_text("nested\n")
    return sdir


def test_export_returns_none_for_unknown_session(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    assert build_session_export(state, "nope") is None


def test_export_bundles_all_known_files(short_tmp: Path):
    state = short_tmp / "state"
    sid = "e" * 32
    _mk_session(state, sid)
    blob = build_session_export(state, sid)
    assert blob is not None
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        names = set(tf.getnames())
    assert f"{sid}/session.json" in names
    assert f"{sid}/events.jsonl" in names
    assert f"{sid}/policy_hits.jsonl" in names
    assert f"{sid}/transcript.jsonl" in names
    assert f"{sid}/contain/README" in names
    assert f"{sid}/contain/sub/nested.txt" in names


def test_export_includes_attestation(short_tmp: Path):
    state = short_tmp / "state"
    sid = "g" * 32
    _mk_session(state, sid)
    blob = build_session_export(state, sid)
    assert blob is not None
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        member = tf.getmember(f"{sid}/ATTESTATION.json")
        raw = tf.extractfile(member).read()
    att = json.loads(raw)
    assert att["session_id"] == sid
    assert att["audit_chain"]["present"] is True


def test_signature_is_deterministic_and_secret_bound(short_tmp: Path):
    blob = b"hello world"
    s1 = build_session_export_signature(blob, "secret-a")
    s2 = build_session_export_signature(blob, "secret-a")
    s3 = build_session_export_signature(blob, "secret-b")
    # Same blob + same secret → same signature.
    assert s1 == s2
    # Different secret → different signature.
    assert s1 != s3
    # Hex representation: 64 hex chars + trailing newline.
    assert len(s1.strip()) == 64


def test_export_skips_missing_optional_streams(short_tmp: Path):
    state = short_tmp / "state"
    sid = "f" * 32
    sdir = state / "sessions" / sid
    sdir.mkdir(parents=True)
    (sdir / "events.jsonl").write_text('{"kind":"hi"}\n')

    blob = build_session_export(state, sid)
    assert blob is not None
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        names = set(tf.getnames())
    # Attestation is always emitted; beyond that only events.jsonl.
    assert names == {f"{sid}/events.jsonl", f"{sid}/ATTESTATION.json"}
