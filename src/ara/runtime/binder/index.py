"""SQLite metadata index for evidence binder.

Schema mirrors orchestrator skeleton §7.2. FTS5 virtual table powers
3.1 evidence.search.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    sha256: str
    captured_at: float
    captured_during_activation_id: str | None
    source: str
    source_metadata: dict
    summary: str
    size_bytes: int
    content_type: str | None
    masked: bool


@dataclass(frozen=True)
class SearchResult:
    record: EvidenceRecord
    score: float
    excerpt: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    captured_at REAL NOT NULL,
    captured_during_activation_id TEXT,
    source TEXT NOT NULL,
    source_metadata_json TEXT,
    summary TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_type TEXT,
    masked INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS evidence_by_activation
    ON evidence(captured_during_activation_id);
CREATE INDEX IF NOT EXISTS evidence_by_source ON evidence(source);
CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
    id UNINDEXED,
    summary,
    source,
    content_excerpt,
    tokenize = 'porter unicode61'
);
"""


class EvidenceIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def upsert(self, rec: EvidenceRecord, content_excerpt: str) -> None:
        with self._tx() as c:
            existing = c.execute(
                "SELECT id FROM evidence WHERE id = ?", (rec.id,)
            ).fetchone()
            if existing is None:
                c.execute(
                    """INSERT INTO evidence
                       (id, sha256, captured_at, captured_during_activation_id,
                        source, source_metadata_json, summary, size_bytes,
                        content_type, masked)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rec.id, rec.sha256, rec.captured_at,
                        rec.captured_during_activation_id, rec.source,
                        json.dumps(rec.source_metadata, sort_keys=True),
                        rec.summary, rec.size_bytes, rec.content_type,
                        1 if rec.masked else 0,
                    ),
                )
                c.execute(
                    """INSERT INTO evidence_fts (id, summary, source, content_excerpt)
                       VALUES (?, ?, ?, ?)""",
                    (rec.id, rec.summary, rec.source, content_excerpt),
                )

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        row = self._conn.execute(
            "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def by_activation(self, activation_id: str) -> list[EvidenceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE captured_during_activation_id = ?",
            (activation_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def search(self, query: str, *, source: str | None = None,
               since: float | None = None, k: int = 10) -> list[SearchResult]:
        params_query = str(query)
        try:
            rows = self._search_rows(
                params_query, source=source, since=since, k=k,
            )
        except sqlite3.OperationalError as exc:
            if not _is_fts_query_error(exc):
                raise
            rows = self._search_rows(
                _literal_fts_query(params_query),
                source=source,
                since=since,
                k=k,
            )
        return [
            SearchResult(
                record=self._row_to_record(r),
                score=float(r["score"]) if r["score"] is not None else 0.0,
                excerpt=r["excerpt"] or "",
            )
            for r in rows
        ]

    def _search_rows(
        self,
        query: str,
        *,
        source: str | None,
        since: float | None,
        k: int,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT e.*, snippet(evidence_fts, 3, '[', ']', '...', 32) AS excerpt, "
            "rank AS score "
            "FROM evidence_fts "
            "JOIN evidence e ON e.id = evidence_fts.id "
            "WHERE evidence_fts MATCH ? "
        )
        params: list = [query]
        if source is not None:
            sql += "AND e.source = ? "
            params.append(source)
        if since is not None:
            sql += "AND e.captured_at >= ? "
            params.append(since)
        sql += "ORDER BY rank LIMIT ?"
        params.append(k)
        return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            id=row["id"],
            sha256=row["sha256"],
            captured_at=row["captured_at"],
            captured_during_activation_id=row["captured_during_activation_id"],
            source=row["source"],
            source_metadata=json.loads(row["source_metadata_json"] or "{}"),
            summary=row["summary"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            masked=bool(row["masked"]),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _literal_fts_query(query: str) -> str:
    return '"' + str(query).replace('"', '""') + '"'


def _is_fts_query_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "fts5" in text or "no such column:" in text
