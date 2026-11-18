"""Synonym + alias layer for the search dispatcher.

A small operator-curated YAML / JSON file mapping canonical terms to
variants (and the reverse alias map). Loaded by :class:`SynonymStore`
and applied at query time before the FTS lookup.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SynonymStore:
    canonical: dict[str, list[str]] = field(default_factory=dict)
    alias_to_canonical: dict[str, str] = field(default_factory=dict)

    def add(self, canonical: str, variants: list[str]) -> None:
        canon = canonical.lower().strip()
        existing = self.canonical.setdefault(canon, [])
        for v in variants:
            v_norm = v.lower().strip()
            if v_norm and v_norm != canon and v_norm not in existing:
                existing.append(v_norm)
                self.alias_to_canonical[v_norm] = canon

    def expand_query(self, q: str) -> str:
        """Expand a query so any canonical term in q is OR-joined with its
        variants. Returns an FTS5-compatible match expression.
        """
        terms = _tokenize(q)
        if not terms:
            return q
        out: list[str] = []
        for term in terms:
            t = term.lower()
            canon = self.alias_to_canonical.get(t, t)
            variants = self.canonical.get(canon, [])
            cluster = [canon] + variants
            cluster = list(dict.fromkeys(cluster))  # dedup, preserve order
            if len(cluster) == 1:
                out.append(_quote_fts(cluster[0]))
            else:
                joined = " OR ".join(_quote_fts(c) for c in cluster)
                out.append(f"({joined})")
        return " ".join(out)

    def to_json_dict(self) -> dict:
        return {"canonical": self.canonical, "alias_to_canonical": self.alias_to_canonical}

    @classmethod
    def from_json_dict(cls, d: dict) -> "SynonymStore":
        s = cls()
        for canon, variants in (d.get("canonical") or {}).items():
            s.add(canon, list(variants))
        return s


def _tokenize(q: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]+", q)


def _quote_fts(term: str) -> str:
    # FTS5 requires terms with non-alpha to be quoted; safest to always quote.
    safe = term.replace('"', '""')
    return f'"{safe}"'


def write(path: Path, store: SynonymStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store.to_json_dict(), sort_keys=True, indent=2))


def read(path: Path) -> SynonymStore:
    if not path.exists():
        return SynonymStore()
    return SynonymStore.from_json_dict(json.loads(path.read_text()))
