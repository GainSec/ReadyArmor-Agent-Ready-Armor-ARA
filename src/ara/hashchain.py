"""Shared primitives for the append-only hash-chain used by both the
audit (``policy_hits.jsonl``) and transcript (``transcript.jsonl``)
writers. Keeping a single implementation means ``verify_chain`` and each
writer compute hashes the same way — no subtle divergence."""

from __future__ import annotations

import hashlib


GENESIS_HASH = "0" * 64


def hash_line(prev_hash: str, line_body: str) -> str:
    """Hash the payload as it will be written — after prev_hash has been
    embedded but before the newline. Same function used by verify_chain."""
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(line_body.encode("utf-8"))
    return h.hexdigest()
