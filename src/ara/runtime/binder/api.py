"""High-level evidence binder API. Combines ``store`` + ``index`` +
``sources``. Single API for every capture path (mitm, shim, agent
tools, agent stream).
"""
from __future__ import annotations

import time
from pathlib import Path

from ara.runtime.binder.index import (
    EvidenceIndex,
    EvidenceRecord,
    SearchResult,
)
from ara.runtime.binder import sources as _sources
from ara.runtime.binder.store import (
    ContentStore,
    evidence_id_for,
    sha256_hex,
)


_EXCERPT_KB = 2


def _make_excerpt(content: bytes) -> str:
    head = content[: _EXCERPT_KB * 1024]
    try:
        return head.decode("utf-8", errors="replace")
    except Exception:
        return ""


class EvidenceBinder:
    """Per-session binder. Idempotent by content hash; multiple put() of
    the same bytes return the same EvidenceRecord without rewriting."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        evidence_root = self.session_dir / "evidence"
        self._store = ContentStore(evidence_root / "sha256")
        self._index = EvidenceIndex(evidence_root / "index.db")

    def put(
        self,
        content: bytes,
        *,
        source: str,
        source_metadata: dict | None = None,
        summary: str,
        content_type: str | None = None,
        activation_id: str | None = None,
        masked: bool = False,
    ) -> EvidenceRecord:
        metadata = source_metadata or {}
        errors = _sources.validate_metadata(source, metadata)
        if errors:
            raise ValueError(f"binder.put metadata invalid: {errors}")
        if len(summary) > 120:
            summary = summary[:117] + "..."
        sha, _path = self._store.put(content)
        ev_id = evidence_id_for(sha)
        existing = self._index.get(ev_id)
        if existing is not None:
            return existing
        rec = EvidenceRecord(
            id=ev_id,
            sha256=sha,
            captured_at=time.time(),
            captured_during_activation_id=activation_id,
            source=source,
            source_metadata=metadata,
            summary=summary,
            size_bytes=len(content),
            content_type=content_type,
            masked=masked,
        )
        self._index.upsert(rec, content_excerpt=_make_excerpt(content))
        return rec

    def get_record(self, evidence_id: str) -> EvidenceRecord | None:
        return self._index.get(evidence_id)

    def get(self, evidence_id: str,
            range: tuple[int, int] | None = None) -> bytes:
        rec = self._index.get(evidence_id)
        if rec is None:
            raise KeyError(f"unknown evidence id: {evidence_id}")
        return self._store.get(rec.sha256, range=range)

    def head(self, evidence_id: str, kb: int = 2) -> bytes:
        rec = self._index.get(evidence_id)
        if rec is None:
            raise KeyError(f"unknown evidence id: {evidence_id}")
        return self._store.head(rec.sha256, kb=kb)

    def by_activation(self, activation_id: str) -> list[EvidenceRecord]:
        return self._index.by_activation(activation_id)

    def search(self, query: str, *, source: str | None = None,
               since: float | None = None, k: int = 10) -> list[SearchResult]:
        return self._index.search(query, source=source, since=since, k=k)

    def hash(self, content: bytes) -> str:
        return sha256_hex(content)

    def close(self) -> None:
        self._index.close()
