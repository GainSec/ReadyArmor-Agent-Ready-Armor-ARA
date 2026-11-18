"""Pack-forge draft accumulator.

Sibling to ``synthesize.WeaponDraft`` but shaped for a *pack*: it collects
the agent's ``propose.*`` / ``extract.*`` proposals (alphabet declarations,
starter entries, validator bindings, chunk graph, policy text, detection
keywords), each with a ``keep`` toggle + provenance, and materializes the
kept items into an :class:`ara.forge.alphabets.pack.AlphabetPack`.

Each item is a dict: ``{"keep": bool, "source": str, "cite": dict, "data": dict}``
where ``source ∈ {"proposed","custom","baseline"}``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ara.forge.alphabets.pack import (
    AlphabetPack,
    ManifestDetectionConfig,
    ValidatorBinding,
    _alpha_from_jsonable,
)

_GROUPS = ("alphabets", "entries", "bindings", "chunks", "policy", "detection")


def _item(data: dict, source: str = "proposed", cite: dict | None = None) -> dict:
    return {"keep": True, "source": source, "cite": cite or {}, "data": data}


@dataclass
class PackDraft:
    name: str
    version: str = "1"
    description: str = ""
    alphabets: list = field(default_factory=list)   # data: {name,shape,fields,axes?,description}
    entries: list = field(default_factory=list)     # data: {alphabet_name, entry}
    bindings: list = field(default_factory=list)    # data: {field,rule,alphabet}
    chunks: list = field(default_factory=list)      # data: ChunkDef dict
    policy: list = field(default_factory=list)      # data: {kind: bulwark|walls|rhema, text}
    detection: list = field(default_factory=list)   # data: {keyword}

    # ---- mutation ----
    def items_for(self, group: str) -> list:
        return getattr(self, group)

    def add_alphabet(self, decl: dict, **kw) -> None:
        self.alphabets.append(_item(decl, **kw))

    def add_entry(self, alphabet_name: str, entry: dict, **kw) -> None:
        self.entries.append(_item({"alphabet_name": alphabet_name, "entry": entry}, **kw))

    def add_binding(self, binding: dict, **kw) -> None:
        self.bindings.append(_item(binding, **kw))

    def add_chunk(self, chunk: dict, **kw) -> None:
        self.chunks.append(_item(chunk, **kw))

    def add_policy(self, kind: str, text: str, **kw) -> None:
        self.policy.append(_item({"kind": kind, "text": text}, **kw))

    def add_detection(self, keyword: str, **kw) -> None:
        self.detection.append(_item({"keyword": keyword}, **kw))

    # ---- materialize ----
    def _kept(self, group: str) -> list:
        return [it["data"] for it in getattr(self, group) if it.get("keep", True)]

    def to_pack(self) -> AlphabetPack:
        # starter entries keyed by alphabet name
        starters: dict[str, list] = {}
        for e in self._kept("entries"):
            starters.setdefault(e["alphabet_name"], []).append(e["entry"])
        alphabets = [
            _alpha_from_jsonable(_decl_to_jsonable(d, starters.get(d["name"], [])))
            for d in self._kept("alphabets")
        ]
        bindings = [ValidatorBinding(**b) for b in self._kept("bindings")]
        chunks = list(self._kept("chunks"))
        policy: dict[str, str] = {}
        for p in self._kept("policy"):
            k = p["kind"]
            policy[k] = (policy.get(k, "") + "\n" + p["text"]).strip() if policy.get(k) else p["text"]
        keywords = [d["keyword"] for d in self._kept("detection")]
        return AlphabetPack(
            name=self.name,
            version=self.version,
            description=self.description,
            alphabets=alphabets,
            validator_bindings=bindings,
            manifest_detection=ManifestDetectionConfig(report_section_keywords=keywords),
            chunk_templates=chunks,
            policy_defaults=policy,
        )

    # ---- curate ----
    def toggle(self, group: str, index: int, keep: bool) -> None:
        self.items_for(group)[index]["keep"] = bool(keep)

    def add_item(self, group: str, data: dict, source: str = "custom") -> None:
        self.items_for(group).append(_item(data, source=source))

    def edit(self, group: str, index: int, data: dict) -> None:
        self.items_for(group)[index]["data"] = data

    def review_lines(self) -> list:
        import json as _json
        out: list = []
        for g in _GROUPS:
            for i, it in enumerate(self.items_for(g)):
                mark = "x" if it.get("keep", True) else " "
                out.append(f"[{mark}] {g}[{i}] ({it['source']}) "
                           f"{_json.dumps(it['data'])[:80]}")
        return out

    # ---- persistence ----
    def to_jsonable(self) -> dict:
        return {"name": self.name, "version": self.version,
                "description": self.description,
                **{g: getattr(self, g) for g in _GROUPS}}

    @classmethod
    def from_jsonable(cls, d: dict) -> "PackDraft":
        return cls(
            name=d["name"], version=d.get("version", "1"),
            description=d.get("description", ""),
            **{g: list(d.get(g, [])) for g in _GROUPS},
        )


def _matrix_row_to_cell(row: dict, axes: list, cell_fields: list) -> dict:
    """Normalise a matrix starter row into ``{coords, cell}``.

    The agent reliably emits matrix content as one FLAT entry per cell
    (``{metric_name, metric_value, multiplier}``) rather than pre-split
    coords/cell. Since the alphabet declares its axes and cell_fields, the
    split is deterministic — do it mechanically instead of relying on the
    agent to format it. Rows already in ``{coords, cell}`` form pass through.
    """
    if not isinstance(row, dict):
        return row
    if "coords" in row or "cell" in row:
        return row
    cf_names = [
        (f.get("name") if isinstance(f, dict) else f) for f in cell_fields
    ]
    coords = {ax: row[ax] for ax in axes if ax in row}
    cell = {n: row[n] for n in cf_names if n in row}
    if not coords and not cell:
        return row  # nothing matched the schema — leave untouched
    return {"coords": coords, "cell": cell}


def _decl_to_jsonable(decl: dict, starter: list) -> dict:
    """Map a ``propose.alphabet`` body → the dict ``_alpha_from_jsonable`` wants."""
    # The agent occasionally omits shape; default to categorical rather than
    # letting ShapeKind(None) raise and block the whole pack's seal.
    shape = decl.get("shape") or "categorical"
    out: dict[str, Any] = {"name": decl["name"], "shape": shape,
                           "description": decl.get("description", ""),
                           "starter": list(starter)}
    if shape in ("categorical", "template_set"):
        out["schema"] = {"fields": decl.get("fields", [])}
    elif shape == "matrix":
        # cell_fields is the matrix-native key; fall back to fields for
        # older proposals that reused the categorical slot.
        axes = decl.get("axes", [])
        cell_fields = decl.get("cell_fields") or decl.get("fields", [])
        out["schema"] = {"axes": axes, "cell_fields": cell_fields}
        out["starter"] = [
            _matrix_row_to_cell(row, axes, cell_fields) for row in starter
        ]
    elif shape == "vector":
        out["schema"] = {"axes": decl.get("axes", [])}
    else:
        out["schema"] = {"fields": decl.get("fields", [])}
    return out


# ---- assembly from extraction events -------------------------------

def build_pack_draft(
    events: list,
    name: str,
    *,
    chunk_baseline: list | None = None,
    policy_baseline: dict | None = None,
) -> PackDraft:
    """Bucket forge extraction events (the pen's channels) into a draft.
    If no chunks/policy were proposed, seed the provided baselines tagged
    ``source="baseline"`` so they're curatable like any other item."""
    d = PackDraft(name=name)
    for ev in events:
        body = ev.get("body") or {}
        ch = ev.get("channel")
        cite = {"manifest_element_id": ev.get("manifest_element_id")} if ev.get("manifest_element_id") else {}
        if ch == "propose.alphabet":
            d.add_alphabet(body, cite=cite)
        elif ch == "extract.conc" and body.get("alphabet_name"):
            # categorical/template_set send {entry}; matrix cells send
            # {coords, cell}. The agent often sends BOTH (coords/cell for the
            # matrix AND a categorical-style entry) — the matrix-native
            # coords/cell MUST win, else the real cell value is discarded in
            # favour of {id,label} and the matrix seals value-less.
            if body.get("coords") is not None or body.get("cell") is not None:
                entry = {"coords": body.get("coords") or {},
                         "cell": body.get("cell") or {}}
            else:
                entry = body.get("entry") or {}
            d.add_entry(body["alphabet_name"], entry, cite=cite)
        elif ch == "propose.binding":
            d.add_binding(body, cite=cite)
        elif ch == "propose.chunk":
            d.add_chunk(body, cite=cite)
        elif ch == "propose.detection":
            # Accept either the MCP body shape {report_section_keywords: [...]}
            # or a single {keyword: ...}.
            kws = body.get("report_section_keywords")
            if isinstance(kws, list):
                for kw in kws:
                    if isinstance(kw, str) and kw.strip():
                        d.add_detection(kw.strip(), cite=cite)
            elif body.get("keyword"):
                d.add_detection(str(body["keyword"]), cite=cite)
        elif ch == "extract.bulwark_rule":
            d.add_policy("bulwark", _render_bulwark(body), cite=cite)
        elif ch == "extract.rhema_rule":
            d.add_policy("rhema", _render_rhema(body), cite=cite)
        elif ch == "extract.walls_path":
            d.add_policy("walls", _render_walls(body), cite=cite)
    if not d.chunks and chunk_baseline:
        for c in chunk_baseline:
            d.add_chunk(dict(c), source="baseline")
    if not d.policy and policy_baseline:
        for k, txt in policy_baseline.items():
            if txt:
                d.add_policy(k, txt, source="baseline")
    # #1: safe mechanical merge of formatting variants of the SAME name
    # (case / underscores / spaces differ but tokens are identical, e.g.
    # "Test Category" == "test_category"). Semantic synonyms with different
    # tokens are left to the prompt — no fuzzy false-merges here.
    import re as _re
    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]", "", str(s).lower())
    canon: dict = {}   # normalized -> first (canonical) name
    kept, seen = [], set()
    for a in d.alphabets:
        nm = str((a.get("data") or {}).get("name") or "")
        if not nm:
            kept.append(a)
            continue
        key = _norm(nm)
        canon.setdefault(key, nm)
        if key in seen:
            continue           # drop the duplicate declaration
        seen.add(key)
        kept.append(a)
    d.alphabets = kept
    for e in d.entries:
        data = e.get("data") or {}
        an = data.get("alphabet_name")
        if an:
            data["alphabet_name"] = canon.get(_norm(an), an)
    for b in d.bindings:
        data = b.get("data") or {}
        al = data.get("alphabet")
        if al:
            data["alphabet"] = canon.get(_norm(al), al)

    # #3: pre-uncheck thin alphabets (<=1 entry) so the operator opts IN to
    # keep one, instead of hand-dropping many over-fragmented dimensions.
    # Vector alphabets carry their content as schema axes, not entry rows, so
    # they legitimately have 0 entries — never thin-drop them on entry count.
    import collections as _collections
    _counts = _collections.Counter(
        str((e.get("data") or {}).get("alphabet_name") or "") for e in d.entries)
    for a in d.alphabets:
        data = a.get("data") or {}
        nm = str(data.get("name") or "")
        if data.get("shape") == "vector":
            continue
        if nm and _counts.get(nm, 0) <= 1:
            a["keep"] = False
    return d


def _render_bulwark(body: dict) -> str:
    parts = [body.get("action", "deny"), body.get("layer", ""), body.get("match", "")]
    return " ".join(str(p) for p in parts if p).strip()


def _render_rhema(body: dict) -> str:
    parts = [body.get("action", "deny"), body.get("layer", ""), body.get("pattern", "")]
    return " ".join(str(p) for p in parts if p).strip()


def _render_walls(body: dict) -> str:
    return f"{body.get('kind', 'deny_path')} {body.get('value', '')}".strip()
