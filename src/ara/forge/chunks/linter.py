"""Chunk graph linter — runs at forge seal.

Returns ALL failures (not first-fail). The 4.1-style "no mandatory
production" rule is the most important: every chunk that can emit a
finding must offer at least one exit that does NOT lead to another
finding-emitting chunk.
"""
from __future__ import annotations

from dataclasses import dataclass

from ara.forge.chunks.chunkdef import ChunkDef


@dataclass(frozen=True)
class LinterFailure:
    rule: str
    message: str


def lint_graph(chunks: list[ChunkDef]) -> list[LinterFailure]:
    failures: list[LinterFailure] = []
    failures.extend(_rule_entry_chunk_unique(chunks))
    failures.extend(_rule_all_chunks_reachable(chunks))
    failures.extend(_rule_no_dead_ends(chunks))
    failures.extend(_rule_exit_targets_exist(chunks))
    failures.extend(_rule_caller_tags_valid(chunks))
    failures.extend(_rule_no_mandatory_production(chunks))
    return failures


def _rule_entry_chunk_unique(chunks: list[ChunkDef]) -> list[LinterFailure]:
    entries = [c for c in chunks if c.is_entry]
    if len(entries) == 0:
        return [LinterFailure("ENTRY_CHUNK_REACHABLE", "no entry chunk (is_entry=True)")]
    if len(entries) > 1:
        return [LinterFailure("ENTRY_CHUNK_REACHABLE",
                              f"multiple entry chunks: {[c.id for c in entries]}")]
    return []


def _rule_all_chunks_reachable(chunks: list[ChunkDef]) -> list[LinterFailure]:
    entries = [c for c in chunks if c.is_entry]
    if not entries:
        return []
    by_id = {c.id: c for c in chunks}
    visited: set[str] = set()
    frontier = [entries[0].id]
    while frontier:
        nxt: list[str] = []
        for cid in frontier:
            if cid in visited:
                continue
            visited.add(cid)
            c = by_id.get(cid)
            if c is None:
                continue
            for t in c.exits.values():
                if t.next_chunk_id not in visited:
                    nxt.append(t.next_chunk_id)
        frontier = nxt
    unreachable = [c.id for c in chunks if c.id not in visited]
    if unreachable:
        return [LinterFailure("ALL_CHUNKS_REACHABLE",
                              f"unreachable chunks: {unreachable}")]
    return []


def _rule_no_dead_ends(chunks: list[ChunkDef]) -> list[LinterFailure]:
    out: list[LinterFailure] = []
    for c in chunks:
        if not c.exits and not c.is_terminal:
            out.append(LinterFailure(
                "NO_DEAD_ENDS",
                f"chunk {c.id} has no exits and is not is_terminal",
            ))
    return out


def _rule_exit_targets_exist(chunks: list[ChunkDef]) -> list[LinterFailure]:
    ids = {c.id for c in chunks}
    out: list[LinterFailure] = []
    for c in chunks:
        for tag, t in c.exits.items():
            if t.next_chunk_id not in ids:
                out.append(LinterFailure(
                    "EXIT_TARGETS_EXIST",
                    f"chunk {c.id} exit {tag!r} → {t.next_chunk_id!r} not in graph",
                ))
    return out


_VALID_CALLERS = {"agent", "operator", "system", None}


def _rule_caller_tags_valid(chunks: list[ChunkDef]) -> list[LinterFailure]:
    out: list[LinterFailure] = []
    for c in chunks:
        for tag, t in c.exits.items():
            if t.requires_caller not in _VALID_CALLERS:
                out.append(LinterFailure(
                    "CALLER_TAGS_VALID",
                    f"chunk {c.id} exit {tag!r}: requires_caller={t.requires_caller!r}",
                ))
    return out


def _rule_no_mandatory_production(chunks: list[ChunkDef]) -> list[LinterFailure]:
    by_id = {c.id: c for c in chunks}
    finding_emitting = {c.id for c in chunks if c.can_emit_finding}
    out: list[LinterFailure] = []
    for c in chunks:
        if not c.can_emit_finding:
            continue
        if not c.exits:
            continue  # already flagged by NO_DEAD_ENDS
        non_finding_exit = False
        for tag, t in c.exits.items():
            target = by_id.get(t.next_chunk_id)
            if target is None:
                continue
            if target.id not in finding_emitting:
                non_finding_exit = True
                break
        if not non_finding_exit:
            out.append(LinterFailure(
                "NO_MANDATORY_PRODUCTION",
                f"chunk {c.id} only exits to finding-emitting chunks; "
                "add an investigative-return exit",
            ))
    return out
