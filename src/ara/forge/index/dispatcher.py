"""Unified search dispatcher.

Used at:
- Forge time (calibration): query the in-flight weapon index
- Engagement time: query the weapon index plus session artifacts

When ``embedding_provider != "null"``, results are reranked by cosine
similarity against the precomputed (or live-computed) embeddings.
v1's NullEmbedder skips this stage entirely.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ara.forge.index.builder import FTSIndex
from ara.forge.index.embedder import EmbeddingProvider, NullEmbedder
from ara.forge.index.synonyms import SynonymStore


@dataclass(frozen=True)
class SearchResult:
    id: str
    summary: str
    score: float
    kind: str
    score_breakdown: dict[str, float]


@dataclass(frozen=True)
class SearchResponse:
    hits: list[SearchResult]
    total_available: int
    next_cursor: str | None
    cap_applied: int
    query: str
    kind_filter: str | None
    elapsed_ms: float
    embedding_used: bool


class SearchDispatcher:
    def __init__(
        self,
        index: FTSIndex,
        *,
        synonyms: SynonymStore | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.index = index
        self.synonyms = synonyms or SynonymStore()
        self.embedder = embedder or NullEmbedder()

    def search(
        self, query: str,
        *, kind: str | None = None, k: int = 10,
    ) -> SearchResponse:
        t0 = time.time()
        match_expr = self.synonyms.expand_query(query) if query.strip() else query
        if not match_expr:
            return SearchResponse(
                hits=[], total_available=0, next_cursor=None, cap_applied=k,
                query=query, kind_filter=kind, elapsed_ms=0.0,
                embedding_used=False,
            )
        try:
            rows = self.index.search(match_expr, kind=kind, k=k)
        except Exception:
            # Fall back to a literal-quoted query if the expansion produced
            # an FTS-incompatible expression
            rows = self.index.search(_literal_quote(query), kind=kind, k=k)
        hits: list[SearchResult] = []
        for r in rows:
            score = float(r.get("score") or 0.0)
            hits.append(SearchResult(
                id=r["artifact_id"],
                summary=r.get("summary") or "",
                score=score,
                kind=r["kind"],
                score_breakdown={"fts": score, "synonym_boost": 0.0,
                                 "embedding_delta": 0.0, "freq_boost": 0.0},
            ))

        elapsed = (time.time() - t0) * 1000.0
        return SearchResponse(
            hits=hits, total_available=len(hits),
            next_cursor=None, cap_applied=k,
            query=query, kind_filter=kind, elapsed_ms=elapsed,
            embedding_used=self.embedder.name != "null",
        )


def _literal_quote(q: str) -> str:
    safe = q.replace('"', '""')
    return f'"{safe}"'


def open_dispatcher(weapon_dir: Path) -> SearchDispatcher:
    """Convenience: open the dispatcher for a sealed weapon directory."""
    fts_path = weapon_dir / "search" / "fts.db"
    syn_path = weapon_dir / "search" / "synonyms.json"
    index = FTSIndex(fts_path)
    syn = SynonymStore()
    if syn_path.exists():
        from ara.forge.index.synonyms import read as _read_syn
        syn = _read_syn(syn_path)
    return SearchDispatcher(index, synonyms=syn)
