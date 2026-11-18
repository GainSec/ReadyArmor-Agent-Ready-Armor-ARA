"""Multi-source reconciliation for pack forge (the reduce-step fix).

See docs/superpowers/specs/2026-07-17-pack-forge-reconciliation-design.md.

``build_pack_draft`` unions extractions across all mats naively, so a concept
that appears incompatibly in two mats produces an incoherent alphabet
(contradictory matrix cells) or a semantic-duplicate pair. This module:

  * ``detect_conflict_groups`` — a MECHANICAL work-list of what needs an agent
    decision (so the reconcile agent is only invoked where there is work), and
  * ``apply_*`` — the MECHANICAL transforms that realise an agent's decision on
    the ``PackDraft`` (merge / split / resolve_cell / drop).

The agent turn in between (loop_driver.run_reconcile) supplies the judgement;
this module supplies detection and application. Gates in phases.seal stay as
the acceptance test the reconciled draft must pass.
"""
from __future__ import annotations

import json
import re

from ara.forge.pack_draft import PackDraft, _item


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _kept(draft: PackDraft, group: str) -> list:
    return [it for it in getattr(draft, group) if it.get("keep", True)]


def _alpha_decls(draft: PackDraft) -> dict:
    """name -> alphabet item (kept only)."""
    out: dict = {}
    for it in _kept(draft, "alphabets"):
        nm = (it.get("data") or {}).get("name")
        if nm:
            out[nm] = it
    return out


def _entry_items(draft: PackDraft, alphabet: str) -> list:
    return [it for it in _kept(draft, "entries")
            if (it.get("data") or {}).get("alphabet_name") == alphabet]


def _norm_tokens(s: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", str(s).lower()) if t}


def _split_siblings(a: str, b: str) -> bool:
    """True if a and b are products of the same split (`base__x`, `base__y`) —
    intentionally similar names that must NOT be re-flagged as duplicates."""
    return ("__" in a and "__" in b
            and a.split("__", 1)[0] == b.split("__", 1)[0])


def _j(obj) -> str:
    return json.dumps(obj, sort_keys=True)


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def detect_contradictions(draft: PackDraft) -> list:
    """Matrices where one coordinate carries >1 distinct cell value."""
    out: list = []
    for name, decl in _alpha_decls(draft).items():
        if (decl.get("data") or {}).get("shape") != "matrix":
            continue
        by_coord: dict = {}
        for it in _entry_items(draft, name):
            e = (it.get("data") or {}).get("entry") or {}
            coords = e.get("coords")
            if coords is None:
                continue
            ck = _j(coords)
            vk = _j(e.get("cell"))
            cite = (it.get("cite") or {})
            by_coord.setdefault(ck, {}).setdefault(vk, []).append(cite)
        conflicts = [
            {"coords": json.loads(ck),
             "values": [{"cell": json.loads(vk), "cites": cites}
                        for vk, cites in vals.items()]}
            for ck, vals in by_coord.items() if len(vals) > 1
        ]
        if conflicts:
            out.append({"kind": "contradiction", "alphabet": name,
                        "conflicts": conflicts})
    return out


def detect_duplicates(draft: PackDraft, *, name_overlap: float = 0.6) -> list:
    """Same-shape alphabets whose names are token-similar (semantic dups the
    formatting-variant merge misses, e.g. cvss_base_weights /
    cvss_base_metric_weights). Conservative — the agent confirms/merges."""
    decls = _alpha_decls(draft)
    names = list(decls)
    used: set = set()
    groups: list = []
    for i, a in enumerate(names):
        if a in used:
            continue
        grp = [a]
        ta = _norm_tokens(a)
        for b in names[i + 1:]:
            if b in used:
                continue
            if (decls[a]["data"].get("shape")
                    != decls[b]["data"].get("shape")):
                continue
            if _split_siblings(a, b):
                continue  # intentional split products, not duplicates
            tb = _norm_tokens(b)
            if not ta or not tb:
                continue
            jac = len(ta & tb) / len(ta | tb)
            if jac >= name_overlap:
                grp.append(b)
        if len(grp) > 1:
            used.update(grp)
            groups.append({"kind": "duplicate", "alphabets": grp})
    return groups


def detect_conflict_groups(draft: PackDraft) -> list:
    """The full reconciliation work-list. Empty → no agent turns needed."""
    return detect_contradictions(draft) + detect_duplicates(draft)


# --------------------------------------------------------------------------
# apply — mechanical realisation of an agent decision
# --------------------------------------------------------------------------

def apply_merge(draft: PackDraft, targets, into: str) -> None:
    """Collapse ``targets`` into ``into``: re-point their entries and bindings,
    drop the redundant declarations. ``into`` is kept."""
    victims = {t for t in targets if t != into}
    for it in draft.alphabets:
        if (it.get("data") or {}).get("name") in victims:
            it["keep"] = False
    for it in draft.entries:
        d = it.get("data") or {}
        if d.get("alphabet_name") in victims:
            d["alphabet_name"] = into
    for it in draft.bindings:
        d = it.get("data") or {}
        for key in ("alphabet", "alphabet_name"):
            if d.get(key) in victims:
                d[key] = into


def apply_drop(draft: PackDraft, name: str) -> None:
    """Drop an alphabet and everything pointing at it."""
    for it in draft.alphabets:
        if (it.get("data") or {}).get("name") == name:
            it["keep"] = False
    for it in draft.entries:
        if (it.get("data") or {}).get("alphabet_name") == name:
            it["keep"] = False
    for it in draft.bindings:
        d = it.get("data") or {}
        if name in (d.get("alphabet"), d.get("alphabet_name")):
            it["keep"] = False


def apply_resolve_cell(draft: PackDraft, alphabet: str, coords: dict,
                       keep_cell: dict) -> None:
    """At ``coords`` in ``alphabet``, keep only the cell equal to
    ``keep_cell``; drop the conflicting rows at that coordinate."""
    ck = _j(coords)
    kk = _j(keep_cell)
    for it in _entry_items(draft, alphabet):
        e = (it.get("data") or {}).get("entry") or {}
        if e.get("coords") is None or _j(e.get("coords")) != ck:
            continue
        if _j(e.get("cell")) != kk:
            it["keep"] = False


def apply_split(draft: PackDraft, name: str, into: list) -> None:
    """Split ``name`` into several source-scoped alphabets.

    ``into`` = [{"new_name": str, "element_ids": [manifest_element_id, ...]}].
    Each entry of ``name`` is re-pointed to the new alphabet owning its source
    element; entries whose source isn't claimed are dropped (unassigned)."""
    decl_it = next((it for it in draft.alphabets
                    if (it.get("data") or {}).get("name") == name), None)
    if decl_it is None:
        return
    owner: dict = {}                       # element_id -> new_name
    for spec in into:
        nn = spec.get("new_name")
        if not nn:
            continue
        new_decl = dict(decl_it["data"])
        new_decl["name"] = nn
        draft.alphabets.append(_item(new_decl, source="reconcile"))
        for eid in spec.get("element_ids") or []:
            owner[eid] = nn
    for it in _entry_items(draft, name):
        eid = (it.get("cite") or {}).get("manifest_element_id")
        nn = owner.get(eid)
        if nn:
            it["data"]["alphabet_name"] = nn
        else:
            it["keep"] = False
    decl_it["keep"] = False


# --------------------------------------------------------------------------
# dispatch — apply a list of agent decisions
# --------------------------------------------------------------------------

def apply_decision(draft: PackDraft, decision: dict) -> None:
    """Apply one reconcile.* decision body to the draft."""
    op = decision.get("op")
    if op == "merge":
        apply_merge(draft, decision["targets"], decision["into"])
    elif op == "split":
        apply_split(draft, decision["name"], decision["into"])
    elif op == "resolve_cell":
        apply_resolve_cell(draft, decision["alphabet"],
                           decision["coords"], decision["keep_cell"])
    elif op == "drop":
        apply_drop(draft, decision["name"])
    else:
        raise ValueError(f"unknown reconcile op {op!r}")
