"""Post-forge semantic dedup for Tailor candidates — a REDUCE pass.

The forge is a map (per-element), so it over-produces near-duplicate candidates
(e.g. one WSTG methodology-boundary absolute per section). Dedup is inherently
holistic — it must see the whole set — so it runs once, after the forge, as an
agent pass that clusters same-intent candidates and keeps one per cluster.

Safety (this is opt-in and must be trivially reversible):
  * NOT auto-run by the forge — call ``dedup_tailor_session`` explicitly.
  * NON-DESTRUCTIVE — dropped candidates are moved to ``deduped_out`` (tagged
    with ``_dedup_merged_into``), never deleted. ``candidates`` keeps only the
    survivors, so downstream ingest naturally uses the deduped set.
  * ``restore_dedup`` moves everything back in one call (full undo).
  * ``apply=False`` returns the plan without touching the session (preview).
  * ``strictness`` tunes how eager the merging is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import temper_lifecycle
from . import temper_driver

_STRICTNESS = {
    "aggressive": "Merge freely: collapse everything expressing a similar "
                  "boundary or theme into one.",
    "balanced": "Merge candidates with the SAME intent; keep meaningfully "
                "distinct guardrails (different targets/tools/conditions) separate.",
    "conservative": "Merge ONLY near-verbatim restatements of the same guardrail; "
                    "when in doubt, keep them separate.",
}


def _cluster_prompt(kind: str, strictness: str) -> str:
    return (
        f"You deduplicate {kind} guardrail candidates. Cluster candidates that "
        "duplicate EACH OTHER in intent. Output ONLY a compact JSON array of "
        "groups; each group is an array of candidate indices that are duplicates, "
        "the FIRST kept and the rest dropped, e.g. [[3,17,42],[8,9]]. Include "
        "ONLY groups of 2+; omit candidates that are distinct. "
        + _STRICTNESS.get(strictness, _STRICTNESS["balanced"])
    )


def _candidate_line(i: int, c: dict) -> str:
    ctrl = c.get("control") or {}
    key = (ctrl.get("principle") or ctrl.get("match") or ctrl.get("pattern")
           or ctrl.get("trigger") or "")
    return f"{i}|{str(c.get('title') or '')[:60]}|{str(key)[:70]}"


def _parse_index_groups(text: str) -> list:
    import json

    t = str(text or "").replace("```json", "").replace("```", "").strip()
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j < i:
        return []
    try:
        data = json.loads(t[i:j + 1])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for g in data if isinstance(data, list) else []:
        if isinstance(g, list):
            ints = [x for x in g if isinstance(x, int)]
            if len(ints) >= 2:
                out.append(ints)
    return out


@dataclass
class DedupResult:
    per_destination: dict = field(default_factory=dict)  # dest -> (before, kept)
    kept_total: int = 0
    dropped_total: int = 0
    applied: bool = False


def _cluster_destination(
    group: list, kind: str, strictness: str,
    invoke: Callable[[str, str], str],
) -> list:
    """Return index-groups (first index kept) for one destination's candidates."""
    if len(group) < 2:
        return []
    lines = [_candidate_line(i, c) for i, c in enumerate(group)]
    user = (
        "CANDIDATES (idx|title|key):\n" + "\n".join(lines)
        + "\n\nOutput the compact JSON array of duplicate index-groups now."
    )
    try:
        raw = invoke(_cluster_prompt(kind, strictness), user)
    except Exception:  # noqa: BLE001 — a failed pass just means "no merges"
        return []
    valid = set(range(len(group)))
    groups = []
    for g in _parse_index_groups(raw):
        g = [x for x in g if x in valid]
        if len(g) >= 2:
            groups.append(g)
    return groups


def dedup_tailor_session(
    state_root: Path,
    *,
    strictness: str = "balanced",
    apply: bool = True,
    destinations: list | None = None,
    model_invoke: Callable[[str, str], str] | None = None,
    model_endpoint: str | None = None,
    model_name: str = "qwen/qwen3.6-35b-a3b",
    timeout_s: float = 240.0,
) -> DedupResult:
    """Cluster same-intent candidates per destination and keep one per cluster.

    Non-destructive: survivors stay in ``candidates``; merged duplicates move to
    ``deduped_out`` tagged with ``_dedup_merged_into``. Use ``apply=False`` to
    preview, ``restore_dedup`` to undo.
    """
    state = Path(state_root)
    record = temper_lifecycle.load_tailor_session(state)
    if record is None:
        raise temper_lifecycle.TailorLifecycleError("no tailor session found")
    cands = list(record.get("candidates") or [])

    invoke = model_invoke or (
        lambda sys_p, usr_p: temper_driver._default_model_invoke(
            sys_p, usr_p, endpoint=model_endpoint,
            model=model_name, timeout_s=timeout_s,
        )
    )

    # group by destination, preserving order
    by_dest: dict = {}
    for c in cands:
        by_dest.setdefault(str(c.get("destination") or ""), []).append(c)

    result = DedupResult()
    kept: list = []
    dropped: list = []
    for dest, group in by_dest.items():
        if destinations and dest not in destinations:
            kept.extend(group)
            result.per_destination[dest] = (len(group), len(group))
            continue
        groups = _cluster_destination(group, dest or "candidate", strictness, invoke)
        keep_idx = set(range(len(group)))
        for g in groups:
            keep = g[0]
            for dx in g[1:]:
                if dx in keep_idx and dx != keep:
                    keep_idx.discard(dx)
                    merged = dict(group[dx])
                    merged["_dedup_merged_into"] = group[keep].get("candidate_id")
                    dropped.append(merged)
        kept.extend(group[i] for i in range(len(group)) if i in keep_idx)
        result.per_destination[dest] = (len(group), len(keep_idx))

    result.kept_total = len(kept)
    result.dropped_total = len(dropped)

    if apply:
        record["candidates"] = kept
        record["deduped_out"] = list(record.get("deduped_out") or []) + dropped
        history = list(record.get("dedup_history") or [])
        history.append({
            "ts": time.time(),
            "strictness": strictness,
            "destinations": destinations or "all",
            "kept": result.kept_total,
            "dropped": result.dropped_total,
        })
        record["dedup_history"] = history
        temper_lifecycle._write_tailor_session(state, record)
        result.applied = True

    return result


def restore_dedup(state_root: Path) -> int:
    """Undo dedup: move every ``deduped_out`` candidate back into ``candidates``
    (dropping the ``_dedup_merged_into`` tag). Returns how many were restored."""
    state = Path(state_root)
    record = temper_lifecycle.load_tailor_session(state)
    if record is None:
        return 0
    out = list(record.get("deduped_out") or [])
    for c in out:
        c.pop("_dedup_merged_into", None)
    record["candidates"] = list(record.get("candidates") or []) + out
    record["deduped_out"] = []
    temper_lifecycle._write_tailor_session(state, record)
    return len(out)
