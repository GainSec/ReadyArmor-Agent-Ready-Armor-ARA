"""FactStore: append-only JSONL writer + reader for one session.

Idempotency: identical content (same predicate, value, evidence_refs,
crumb, activation_id) is deduplicated on append. Two assertions of the
same predicate with different values both append; the latest wins for
``by_predicate(exact)``, ``history`` shows both.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ara.runtime.facts.record import Fact, content_hash, mint_id


class FactStore:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "facts.jsonl"
        self._lock = threading.RLock()
        self._cache: list[Fact] | None = None
        self._content_hashes: set[str] = set()

    # ---------- loading ----------

    def all(self) -> list[Fact]:
        if self._cache is not None:
            return list(self._cache)
        with self._lock:
            if self._cache is not None:
                return list(self._cache)
            facts: list[Fact] = []
            hashes: set[str] = set()
            if self.path.exists():
                with self.path.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        f_obj = Fact.from_jsonable(d)
                        facts.append(f_obj)
                        hashes.add(content_hash(
                            predicate=f_obj.predicate, value=f_obj.value,
                            asserted_by_crumb=f_obj.asserted_by_crumb,
                            evidence_refs=f_obj.evidence_refs,
                            activation_id=f_obj.activation_id,
                        ))
            self._cache = facts
            self._content_hashes = hashes
            return list(facts)

    # ---------- writes ----------

    def append(self, fact: Fact) -> Fact:
        """Idempotent on content-hash. Returns the existing fact if duplicate."""
        with self._lock:
            self.all()  # ensure cache loaded
            ch = content_hash(
                predicate=fact.predicate, value=fact.value,
                asserted_by_crumb=fact.asserted_by_crumb,
                evidence_refs=fact.evidence_refs,
                activation_id=fact.activation_id,
            )
            if ch in self._content_hashes:
                # Find the prior fact with matching content
                for prior in self._cache or []:
                    if prior.id == fact.id or content_hash(
                        predicate=prior.predicate, value=prior.value,
                        asserted_by_crumb=prior.asserted_by_crumb,
                        evidence_refs=prior.evidence_refs,
                        activation_id=prior.activation_id,
                    ) == ch:
                        return prior
            self._write_line(json.dumps(fact.to_jsonable(), sort_keys=True))
            assert self._cache is not None
            self._cache.append(fact)
            self._content_hashes.add(ch)
            return fact

    def supersede(
        self,
        fact_id: str,
        new_value: Any,
        *,
        asserted_by_crumb: str,
        evidence_refs: list[str],
        chunk_at_time: str,
        activation_id: str,
        note: str | None = None,
    ) -> Fact:
        prior = self.by_id(fact_id)
        if prior is None:
            raise KeyError(f"unknown fact id: {fact_id}")
        ts = time.time()
        new = Fact(
            id=mint_id(
                predicate=prior.predicate, value=new_value,
                asserted_by_crumb=asserted_by_crumb,
                evidence_refs=evidence_refs,
                activation_id=activation_id,
                asserted_at=ts,
            ),
            predicate=prior.predicate,
            value=new_value,
            asserted_by_crumb=asserted_by_crumb,
            evidence_refs=evidence_refs,
            chunk_at_time=chunk_at_time,
            activation_id=activation_id,
            asserted_at=ts,
            prior_fact=fact_id,
            depends_on=[fact_id],
            note=note,
        )
        return self.append(new)

    # ---------- reads ----------

    def by_id(self, fact_id: str) -> Fact | None:
        for f in self.all():
            if f.id == fact_id:
                return f
        return None

    def by_predicate(
        self, predicate_pattern: str, *, exact: bool = False,
    ) -> list[Fact]:
        if exact:
            matches = [f for f in self.all() if f.predicate == predicate_pattern]
            # Latest-wins ordering for exact match
            return sorted(matches, key=lambda f: f.asserted_at, reverse=True)
        return [f for f in self.all() if f.predicate.startswith(predicate_pattern)]

    def by_evidence(self, evidence_id: str) -> list[Fact]:
        return [f for f in self.all() if evidence_id in f.evidence_refs]

    def by_crumb(self, crumb_id: str) -> list[Fact]:
        return [f for f in self.all() if f.asserted_by_crumb == crumb_id]

    def by_activation(self, activation_id: str) -> list[Fact]:
        return [f for f in self.all() if f.activation_id == activation_id]

    def history(self, predicate_pattern: str) -> list[Fact]:
        items = self.by_predicate(predicate_pattern, exact=True)
        return sorted(items, key=lambda f: f.asserted_at)

    # ---------- internals ----------

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
