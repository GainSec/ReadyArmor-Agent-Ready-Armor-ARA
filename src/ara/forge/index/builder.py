"""FTS5 index builder. Reads the sealed weapon's components + the
session's facts/leads/findings/evidence + the forge's manifest, and
produces ``search/fts.db`` with one big virtual table.

Indexable kinds (v1):
- ``crumb``    (weapon) — name, requires/emits, action description
- ``shape``    (weapon) — schema fields, descriptions
- ``store``    (weapon) — resource entries
- ``line``     (weapon) — out-of-scope rules + counterexamples
- ``fact``     (session)
- ``evidence`` (session)
- ``manifest`` (forge)  — manifest element extracted_text
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_FTS_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS index_fts USING fts5(
    artifact_id UNINDEXED,
    kind UNINDEXED,
    summary,
    body,
    tags,
    tokenize = 'porter unicode61'
);
"""


@dataclass(frozen=True)
class IndexEntry:
    artifact_id: str
    kind: str
    summary: str
    body: str
    tags: str = ""


class FTSIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._set_meta("schema_version", str(_FTS_SCHEMA_VERSION))
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _set_meta(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def add(self, entry: IndexEntry) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO index_fts (artifact_id, kind, summary, body, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (entry.artifact_id, entry.kind, entry.summary, entry.body, entry.tags),
            )

    def add_many(self, entries: list[IndexEntry]) -> None:
        if not entries:
            return
        with self._tx() as c:
            c.executemany(
                "INSERT INTO index_fts (artifact_id, kind, summary, body, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                [(e.artifact_id, e.kind, e.summary, e.body, e.tags) for e in entries],
            )

    def clear(self) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM index_fts")

    def count(self, kind: str | None = None) -> int:
        if kind is None:
            row = self._conn.execute(
                "SELECT count(*) AS n FROM index_fts"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT count(*) AS n FROM index_fts WHERE kind=?", (kind,)
            ).fetchone()
        return int(row["n"])

    def search(
        self,
        match_expr: str,
        *,
        kind: str | None = None,
        k: int = 10,
    ) -> list[dict]:
        sql = (
            "SELECT artifact_id, kind, summary, "
            "snippet(index_fts, 3, '[', ']', '...', 32) AS excerpt, "
            "rank AS score "
            "FROM index_fts WHERE index_fts MATCH ?"
        )
        params: list = [match_expr]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY rank LIMIT ?"
        params.append(k)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


# ---------- entry extraction helpers ----------

def index_weapon_components(draft_json: dict) -> list[IndexEntry]:
    """Index the buckets of a sealed weapon (or weapon_draft.json)."""
    out: list[IndexEntry] = []
    for crumb in draft_json.get("crumbs") or []:
        out.append(IndexEntry(
            artifact_id=str(crumb.get("crumb_id") or crumb.get("id") or _short_id(crumb)),
            kind="crumb",
            summary=str(crumb.get("name") or crumb.get("title") or "")[:120],
            body=json.dumps(crumb, sort_keys=True),
            tags=" ".join(crumb.get("tags") or []),
        ))
    for shape in draft_json.get("shapes") or []:
        out.append(IndexEntry(
            artifact_id=str(shape.get("shape_id") or _short_id(shape)),
            kind="shape",
            summary=str(shape.get("name") or "")[:120],
            body=json.dumps(shape, sort_keys=True),
        ))
    for store in draft_json.get("stores") or []:
        out.append(IndexEntry(
            artifact_id=str(store.get("store_id") or _short_id(store)),
            kind="store",
            summary=str(store.get("name") or "")[:120],
            body=json.dumps(store, sort_keys=True),
        ))
    for line in draft_json.get("lines") or []:
        out.append(IndexEntry(
            artifact_id=str(line.get("line_id") or _short_id(line)),
            kind="line",
            summary=str(line.get("rule") or line.get("name") or "")[:120],
            body=json.dumps(line, sort_keys=True),
        ))
    return out


def index_facts(facts: list[dict]) -> list[IndexEntry]:
    out: list[IndexEntry] = []
    for f in facts:
        body_text = " ".join(str(x) for x in [
            f.get("predicate"), f.get("value"), f.get("note"), f.get("source"),
        ] if x)
        out.append(IndexEntry(
            artifact_id=f["id"],
            kind="fact",
            summary=f"{f['predicate']} = {f['value']}"[:120],
            body=body_text,
        ))
    return out


def index_leads(leads: list[dict]) -> list[IndexEntry]:
    out: list[IndexEntry] = []
    for ld in leads:
        out.append(IndexEntry(
            artifact_id=ld["id"],
            kind="lead",
            summary=str(ld.get("title", ""))[:120],
            body=" ".join(str(x) for x in [
                ld.get("title"), ld.get("reasoning"), ld.get("proposed_test"),
            ] if x),
            tags=ld.get("priority_computed") or "",
        ))
    return out


def index_manifest(elements: list[dict]) -> list[IndexEntry]:
    out: list[IndexEntry] = []
    for el in elements:
        out.append(IndexEntry(
            artifact_id=el["id"],
            kind="manifest",
            summary=(el.get("metadata", {}).get("heading")
                     or el.get("metadata", {}).get("filename")
                     or el["id"])[:120],
            body=el.get("extracted_text", ""),
            tags=el.get("kind", ""),
        ))
    return out


def _short_id(d: dict) -> str:
    import hashlib
    h = hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()
    return f"_g_{h[:8]}"
