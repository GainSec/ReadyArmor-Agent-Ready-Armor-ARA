"""Tests for runtime.binder (store + index + api)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.binder import sources
from ara.runtime.binder.store import ContentStore, evidence_id_for, sha256_hex


def test_store_put_and_get_roundtrip(tmp_path: Path) -> None:
    s = ContentStore(tmp_path)
    sha, path = s.put(b"hello world")
    assert sha == hashlib.sha256(b"hello world").hexdigest()
    assert path.exists()
    assert s.get(sha) == b"hello world"


def test_store_put_idempotent(tmp_path: Path) -> None:
    s = ContentStore(tmp_path)
    sha1, p1 = s.put(b"x" * 100)
    mtime1 = p1.stat().st_mtime_ns
    sha2, p2 = s.put(b"x" * 100)
    assert sha1 == sha2
    assert p1 == p2
    assert p2.stat().st_mtime_ns == mtime1  # not rewritten


def test_store_two_level_split(tmp_path: Path) -> None:
    s = ContentStore(tmp_path)
    sha, path = s.put(b"abc")
    rel = path.relative_to(tmp_path).parts
    # Expected layout: <ab>/<cd>/<rest>.bin
    assert len(rel[0]) == 2 and len(rel[1]) == 2
    assert rel[2].startswith(sha[4:])


def test_store_range_and_head(tmp_path: Path) -> None:
    s = ContentStore(tmp_path)
    payload = b"0123456789" * 1000  # 10 KB
    sha, _ = s.put(payload)
    assert s.get(sha, range=(10, 5)) == b"01234"
    head = s.head(sha, kb=1)
    assert len(head) == 1024
    assert head == payload[:1024]


def test_evidence_id_format() -> None:
    sha = "a" * 64
    assert evidence_id_for(sha) == "e:" + "a" * 10


def test_binder_put_and_get(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    rec = b.put(
        b'{"hits": [1,2,3]}',
        source="tool:curl",
        source_metadata={"args": "https://example.com"},
        summary="curl example.com",
        content_type="application/json",
        activation_id="a1",
    )
    assert rec.id.startswith("e:")
    assert rec.size_bytes == len(b'{"hits": [1,2,3]}')
    assert rec.captured_during_activation_id == "a1"
    assert b.get(rec.id) == b'{"hits": [1,2,3]}'


def test_binder_put_idempotent_returns_existing(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    rec1 = b.put(b"abc", source="tool:curl", summary="s",
                 source_metadata={}, activation_id="a1")
    rec2 = b.put(b"abc", source="tool:curl", summary="s2",
                 source_metadata={}, activation_id="a2")
    # Same content → same record (first-write-wins on metadata)
    assert rec1.id == rec2.id
    assert rec2.summary == rec1.summary
    assert rec2.captured_during_activation_id == "a1"


def test_binder_summary_truncated(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    long = "x" * 200
    rec = b.put(b"data", source="tool:curl", summary=long,
                source_metadata={})
    assert len(rec.summary) == 120
    assert rec.summary.endswith("...")


def test_binder_by_activation(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    b.put(b"1", source="tool:curl", summary="a", source_metadata={},
          activation_id="A")
    b.put(b"2", source="tool:curl", summary="b", source_metadata={},
          activation_id="A")
    b.put(b"3", source="tool:curl", summary="c", source_metadata={},
          activation_id="B")
    a_recs = b.by_activation("A")
    assert {r.summary for r in a_recs} == {"a", "b"}


def test_binder_fts_search(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    b.put(b"some text about SQL injection", source="tool:curl",
          source_metadata={}, summary="sqli probe response")
    b.put(b"unrelated XSS reflection", source="tool:curl",
          source_metadata={}, summary="xss probe response")
    results = b.search("sqli")
    assert len(results) == 1
    assert "sqli" in results[0].record.summary

    # Excerpt-content match too
    res2 = b.search("injection")
    assert len(res2) >= 1


def test_binder_search_literalizes_dotted_agent_query(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path / "s")
    rec = b.put(
        b"TLSv1.3 target.tls_version Cloudflare target.waf",
        source="tool:python",
        source_metadata={},
        summary="TLSv1.3 target.tls_version Cloudflare target.waf",
    )

    results = b.search("TLSv1.3 target.tls_version", k=5)

    assert results
    assert results[0].record.id == rec.id


def test_binder_search_literalizes_hyphenated_agent_query(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path / "s")
    rec = b.put(
        b"Strict-Transport-Security max-age includeSubDomains",
        source="tool:python",
        source_metadata={},
        summary="strict-origin-when-cross-origin max-age security header",
    )

    results = b.search("strict-origin-when-cross-origin max-age", k=5)

    assert results
    assert results[0].record.id == rec.id


def test_binder_metadata_validation_strict_for_known_source(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    with pytest.raises(ValueError, match="missing required"):
        b.put(b"x", source="mitm:request", summary="s",
              source_metadata={"url": "x"})  # missing 'method'


def test_binder_metadata_validation_unknown_source_permissive(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    b.put(b"x", source="exotic:thing", summary="s",
          source_metadata={"any": "key"})


def test_binder_get_unknown_id_raises(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    with pytest.raises(KeyError):
        b.get("e:deadbeef00")


def test_register_tool_source_idempotent() -> None:
    a = sources.register_tool_source("nmap")
    b = sources.register_tool_source("nmap")
    assert a is b
    assert sources.get("tool:nmap") is a


def test_hash_helper(tmp_path: Path) -> None:
    b = EvidenceBinder(tmp_path)
    assert b.hash(b"hello") == sha256_hex(b"hello")
