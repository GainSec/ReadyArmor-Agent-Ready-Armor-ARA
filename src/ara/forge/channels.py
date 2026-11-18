"""Forge-authoring channels — the runtime IPC surface the agent uses
during EXHAUST phase to read the manifest and emit typed extractions.

Sibling to ``runtime/{facts,leads,findings}/channels.py``. These are
the missing-piece channels: the catalog at
``seed/weapons/blacksmith-hammer/channel_catalog.json`` lists them
(``mats.search``, ``mats.get``, ``extract.proc/conc/fact_shape/store/
line``) but no one had implemented the Channel classes that back
``Supervisor._on_channel_call``. Every prior agent run hit
``unknown_channel`` on the first call and gave up.

Wired in once at supervisor start via :func:`install_forge_channels`,
which registers all seven channels in the global channel registry. The
channels resolve the active forge session by looking for the (single)
session whose ``phase.txt`` reads ``exhaust``; an explicit
``forge_id`` in the body wins over auto-detection.

Read-side (``mats.search``, ``mats.get``) walks the manifest under
``forge/<forge_id>/manifest.jsonl``. Write-side (``extract.*``) calls
:func:`ara.forge.phases.exhaust.record_extraction`, which validates
``manifest_element_id`` against the manifest, stamps an
``extraction_id``, and appends to ``extractions.jsonl``.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ara.forge import state as fstate
from ara.forge.manifest.element import ManifestElement, read_manifest
from ara.forge.phases.exhaust import record_extraction
from ara.forge.session import ForgePhase, ForgeSession
from ara.runtime.channels import registry
from ara.runtime.channels.base import (
    Channel,
    ChannelContext,
    ChannelRejection,
)


# ---------- bodies ----------

@dataclass
class MatsSearchBody:
    query: str
    limit: int = 20
    forge_id: str | None = None
    kind: str | None = None  # filter by ManifestKind value


@dataclass
class MatsGetBody:
    manifest_element_id: str
    forge_id: str | None = None
    max_chars: int | None = None


@dataclass
class _ExtractBodyBase:
    manifest_element_id: str
    body: dict
    forge_id: str | None = None
    citation: dict | None = None


# Each extract.* channel uses the same shape; subclassing for clarity.

@dataclass
class ExtractProcBody(_ExtractBodyBase):
    pass


@dataclass
class ExtractConcBody(_ExtractBodyBase):
    pass


@dataclass
class ExtractFactShapeBody(_ExtractBodyBase):
    pass


@dataclass
class ExtractStoreBody(_ExtractBodyBase):
    pass


@dataclass
class ExtractResourceBody(_ExtractBodyBase):
    """Agent-extracted task resource. ``body`` should carry ``id``,
    ``class`` (reference/wordlist/script/package/vendor-doc/vuln-db),
    ``name``, ``locator``, allowed uses, approval requirements, and
    rationale. These become sealed weapon runtime capabilities."""
    pass


@dataclass
class ExtractLineBody(_ExtractBodyBase):
    pass


@dataclass
class ExtractRhemaRuleBody(_ExtractBodyBase):
    """Agent-extracted rhema rule. ``body`` should carry ``action``,
    ``layer``, ``pattern`` (string or ``regex:...``), and ``rationale``."""
    pass


@dataclass
class ExtractBulwarkRuleBody(_ExtractBodyBase):
    """Agent-extracted bulwark rule. ``body`` should carry ``action``,
    ``layer`` (fs/exec/net/url/etc.), ``match``, ``scope``
    (``armor:<name>`` / ``weapon`` / ``global``), and ``rationale``."""
    pass


@dataclass
class ExtractWallsPathBody(_ExtractBodyBase):
    """Agent-extracted WALLS entry. ``body`` should carry ``kind``
    (``deny_path`` / ``deny_command`` / ``approval_path``), ``value``
    (the path glob or command name), and ``rationale``."""
    pass


# ---- pack-forge schema-declaration bodies (the `pen` weapon) --------
# Same shape as extract.*; the `body` dict declares a piece of a NEW
# pack's schema rather than extracting a value. ``manifest_element_id``
# is the primary supporting section; ``body["supporting_element_ids"]``
# may list more for provenance.

@dataclass
class ProposeAlphabetBody(_ExtractBodyBase):
    """body: {name, shape ∈ {categorical,matrix,template_set,vector},
    fields:[{name,type}], axes? (matrix/vector), description,
    supporting_element_ids?}."""
    pass


@dataclass
class ProposeBindingBody(_ExtractBodyBase):
    """body: {field, rule ∈ {FIELD_IN_ALPHABET,FIELD_IN_MATRIX,
    FOREIGN_KEY_VALID,VECTOR_IN_RANGE}, alphabet}."""
    pass


@dataclass
class ProposeChunkBody(_ExtractBodyBase):
    """body: a ChunkDef {id, title, prompt_fragment, tool_surface, gems,
    exits, can_emit_finding, is_terminal, is_entry}."""
    pass


@dataclass
class ProposeDetectionBody(_ExtractBodyBase):
    """body: {keyword} — a manifest_detection.report_section keyword."""
    pass


# ---------- helpers ----------

def _resolve_forge_id(state_root: Path, ctx_forge_id: str | None,
                     body_forge_id: str | None) -> str:
    """Pick the forge session this call targets.

    Priority: explicit body.forge_id > ctx.forge_id > sole EXHAUST.
    Rejects if the resolved session isn't found or isn't in EXHAUST
    (write-side) — read-side (mats.*) will accept any active phase.
    """
    if body_forge_id:
        return body_forge_id
    if ctx_forge_id:
        return ctx_forge_id
    forge_root = Path(state_root) / "forge"
    if not forge_root.is_dir():
        raise ChannelRejection("no forge sessions present under state_root")
    candidates = []
    for d in sorted(forge_root.iterdir()):
        if not d.is_dir():
            continue
        phase_file = d / "phase.txt"
        if not phase_file.is_file():
            continue
        try:
            phase = phase_file.read_text().strip().lower()
        except OSError:
            continue
        if phase == "exhaust":
            candidates.append(d.name)
    if not candidates:
        raise ChannelRejection(
            "no forge session in EXHAUST phase; pass body.forge_id explicitly"
        )
    if len(candidates) > 1:
        raise ChannelRejection(
            f"multiple EXHAUST sessions ({candidates}); pass "
            "body.forge_id explicitly"
        )
    return candidates[0]


def _load_session(state_root: Path, forge_id: str) -> ForgeSession:
    try:
        return ForgeSession.load(Path(state_root), forge_id)
    except FileNotFoundError as e:
        raise ChannelRejection(
            f"forge session {forge_id!r} not found"
        ) from e


def _mint_extraction_id() -> str:
    return "x_agent_" + secrets.token_hex(5)


# ---------- read-side: mats.search / mats.get ----------

class MatsSearchChannel(Channel):
    """Keyword-search the deposited materials' manifest. Returns ranked
    hits with ``manifest_element_id`` you can pass to ``mats.get`` for
    full text. Case-insensitive substring + simple multi-term scoring.
    """
    name = "mats.search"
    schema = MatsSearchBody
    sink_subsystem = "mats"

    def __init__(self, *, state_root: Path) -> None:
        self.state_root = Path(state_root)

    def validate(self, body: dict) -> None:
        super().validate(body)
        q = body.get("query", "")
        if not isinstance(q, str) or not q.strip():
            raise ChannelRejection("query required, non-empty string")
        limit = body.get("limit", 20)
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ChannelRejection("limit must be 1..100")

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        forge_id = _resolve_forge_id(
            self.state_root, ctx.forge_id, body.get("forge_id"),
        )
        manifest_path = fstate.forge_manifest_path(self.state_root, forge_id)
        elements = read_manifest(manifest_path)
        if body.get("kind"):
            kind = body["kind"]
            elements = [e for e in elements if e.kind.value == kind]
        elements = [e for e in elements if not e.dropped_by_operator]

        terms = [t for t in re.split(r"\s+", body["query"].lower()) if t]
        scored: list[tuple[float, ManifestElement, list[str]]] = []
        for el in elements:
            text_lower = el.extracted_text.lower()
            hits = [t for t in terms if t in text_lower]
            if not hits:
                continue
            score = sum(text_lower.count(t) for t in hits) + len(hits) * 2
            # Snippet: first occurrence with surrounding context
            first_term = hits[0]
            idx = text_lower.find(first_term)
            start = max(0, idx - 80)
            end = min(len(el.extracted_text), idx + len(first_term) + 80)
            snippet = el.extracted_text[start:end]
            scored.append((score, el, [snippet]))

        scored.sort(key=lambda t: t[0], reverse=True)
        limit = int(body.get("limit", 20))
        results = []
        for score, el, snippets in scored[:limit]:
            results.append({
                "manifest_element_id": el.id,
                "source_mat_id": el.source_mat_id,
                "kind": el.kind.value,
                "score": score,
                "snippet": snippets[0] if snippets else "",
                "byte_range": list(el.byte_range),
            })
        return {
            "forge_id": forge_id,
            "query": body["query"],
            "total_hits": len(scored),
            "returned": len(results),
            "results": results,
        }


@dataclass
class ExhaustStatusBody:
    batch_complete: bool          # True = fully derived this turn's elements
    forge_id: str | None = None
    note: str | None = None


class ExhaustStatusChannel(Channel):
    """The agent declares, at the end of a turn, whether it fully derived this
    turn's elements (``batch_complete: true``) or has more to derive
    (``false``). The driver reads this to cycle the same batch vs advance —
    agent-driven completion, not a wall-clock timeout."""
    name = "exhaust.status"
    schema = ExhaustStatusBody
    sink_subsystem = "turn_status.json"

    def __init__(self, *, state_root: Path) -> None:
        self.state_root = Path(state_root)

    def validate(self, body: dict) -> None:
        super().validate(body)
        if not isinstance(body.get("batch_complete"), bool):
            raise ChannelRejection("batch_complete (bool) required")

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        import json as _json
        forge_id = _resolve_forge_id(
            self.state_root, ctx.forge_id, body.get("forge_id"),
        )
        path = fstate.forge_turn_status_path(self.state_root, forge_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({
            "batch_complete": bool(body["batch_complete"]),
            "note": body.get("note"),
        }))
        return {"forge_id": forge_id, "batch_complete": bool(body["batch_complete"])}


class MatsGetChannel(Channel):
    """Fetch the full extracted text + metadata of a manifest element.
    Use after :class:`MatsSearchChannel` to ground a citation.
    """
    name = "mats.get"
    schema = MatsGetBody
    sink_subsystem = "mats"

    def __init__(self, *, state_root: Path) -> None:
        self.state_root = Path(state_root)

    def validate(self, body: dict) -> None:
        super().validate(body)
        if not isinstance(body.get("manifest_element_id"), str) \
                or not body["manifest_element_id"]:
            raise ChannelRejection("manifest_element_id required")

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        forge_id = _resolve_forge_id(
            self.state_root, ctx.forge_id, body.get("forge_id"),
        )
        manifest_path = fstate.forge_manifest_path(self.state_root, forge_id)
        target = body["manifest_element_id"]
        for el in read_manifest(manifest_path):
            if el.id == target:
                text = el.extracted_text
                max_chars = body.get("max_chars")
                truncated = False
                if isinstance(max_chars, int) and max_chars > 0 and len(text) > max_chars:
                    text = text[:max_chars]
                    truncated = True
                return {
                    "forge_id": forge_id,
                    "manifest_element_id": el.id,
                    "source_mat_id": el.source_mat_id,
                    "kind": el.kind.value,
                    "byte_range": list(el.byte_range),
                    "metadata": el.metadata,
                    "text": text,
                    "truncated": truncated,
                    "dropped_by_operator": el.dropped_by_operator,
                }
        raise ChannelRejection(
            f"manifest_element_id {target!r} not found in forge {forge_id!r}"
        )


# ---------- write-side: extract.* ----------

class _ExtractChannelBase(Channel):
    """Shared logic for the five extract.* channels. Each subclass sets
    ``name`` and ``schema``; everything else is delegated."""

    sink_subsystem = "extractions.jsonl"

    def __init__(self, *, state_root: Path) -> None:
        self.state_root = Path(state_root)

    def validate(self, body: dict) -> None:
        super().validate(body)
        if not isinstance(body.get("manifest_element_id"), str) \
                or not body["manifest_element_id"]:
            raise ChannelRejection("manifest_element_id required")
        inner = body.get("body")
        if not isinstance(inner, dict):
            raise ChannelRejection("body (the extraction payload) must be a dict")

    def _extra_validate(self, state_root: Path, forge_id: str,
                        inner: dict) -> None:
        """Schema-conformance gate against the live session (default no-op).
        Subclasses override to reject non-conforming payloads so the agent is
        forced (via the rejection) to re-emit correctly."""
        return None

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        forge_id = _resolve_forge_id(
            self.state_root, ctx.forge_id, body.get("forge_id"),
        )
        sess = _load_session(self.state_root, forge_id)
        if sess.phase != ForgePhase.EXHAUST:
            raise ChannelRejection(
                f"forge {forge_id!r} is in phase "
                f"{sess.phase.value!r}, EXHAUST required for extractions"
            )
        # Per-channel schema-conformance gate (default no-op). Runs against the
        # live session so it can look up a target alphabet's declared shape.
        self._extra_validate(self.state_root, forge_id, body["body"])
        citation = body.get("citation") or {}
        if "manifest_element_id" not in citation:
            citation["manifest_element_id"] = body["manifest_element_id"]
        if "source" not in citation:
            citation["source"] = "agent"
        try:
            evt = record_extraction(
                sess,
                manifest_element_id=body["manifest_element_id"],
                channel=self.name,
                body=body["body"],
                extraction_id=_mint_extraction_id(),
                citation=citation,
            )
        except ValueError as e:
            raise ChannelRejection(str(e)) from e
        return {
            "forge_id": forge_id,
            "extraction": evt.to_jsonable(),
        }


class ExtractProcChannel(_ExtractChannelBase):
    """Procedure (PROC alphabet): a named, ordered step pattern with
    requires/action/emits, tied to manifest evidence."""
    name = "extract.proc"
    schema = ExtractProcBody


def _lookup_alphabet_decl(state_root: Path, forge_id: str,
                          name: str) -> dict | None:
    """Return the latest ``propose.alphabet`` body for ``name`` in this forge
    session, or None if it hasn't been declared yet."""
    from ara.forge.phases.exhaust import read_extractions
    decl = None
    for ev in read_extractions(state_root, forge_id):
        if ev.channel == "propose.alphabet" and (ev.body or {}).get("name") == name:
            decl = ev.body
    return decl


def _cell_field_names(decl: dict) -> list:
    cf = decl.get("cell_fields") or decl.get("fields") or []
    return [f.get("name") if isinstance(f, dict) else f for f in cf]


def _validate_matrix_cell(decl: dict, inner: dict) -> None:
    """A matrix-targeted extract.conc must carry a coord for EVERY declared
    axis plus at least one cell value — whether emitted as {coords, cell} or
    as a flat entry whose keys match the schema. Rejects the categorical-style
    {id, label} rows that would otherwise seal as empty cells."""
    name = inner.get("alphabet_name")
    axes = list(decl.get("axes") or [])
    cf_names = _cell_field_names(decl)
    if inner.get("coords") is not None or inner.get("cell") is not None:
        coords = inner.get("coords") or {}
        cell = inner.get("cell") or {}
    else:
        entry = inner.get("entry")
        entry = entry if isinstance(entry, dict) else {}
        coords = {ax: entry[ax] for ax in axes if ax in entry}
        cell = {n: entry[n] for n in cf_names if n in entry}
    missing = [ax for ax in axes
               if coords.get(ax) in (None, "")]
    if missing:
        raise ChannelRejection(
            f"matrix alphabet {name!r} cell is missing coordinate(s) for "
            f"axes {missing}. Emit a coord for every axis {axes} plus cell "
            f"value(s) {cf_names} — either as {{coords:{{...}}, cell:{{...}}}} "
            f"or a flat entry with those exact keys."
        )
    has_value = any(cell.get(n) not in (None, "") for n in cf_names) \
        if cf_names else bool(cell)
    if not has_value:
        raise ChannelRejection(
            f"matrix alphabet {name!r} cell has no value for cell field(s) "
            f"{cf_names}. Include the actual cell value (e.g. the weight / "
            f"multiplier), not just the axis labels."
        )


def _validate_categorical_entry(inner: dict) -> None:
    """A categorical/template_set entry needs a non-empty identifying field."""
    entry = inner.get("entry")
    if not isinstance(entry, dict) or not any(
        str(entry.get(k) or "").strip() for k in ("id", "label", "name", "value")
    ):
        raise ChannelRejection(
            f"alphabet {inner.get('alphabet_name')!r} entry needs a non-empty "
            f"id/label/name/value (got {entry!r})"
        )


class ExtractConcChannel(_ExtractChannelBase):
    """Conceptual shape / template (CONC alphabet): rubrics, fix
    libraries, severity matrices, etc."""
    name = "extract.conc"
    schema = ExtractConcBody

    def _extra_validate(self, state_root: Path, forge_id: str,
                        inner: dict) -> None:
        name = inner.get("alphabet_name")
        if not name:
            return  # weapon-forge conc shape, not a pack alphabet entry
        decl = _lookup_alphabet_decl(state_root, forge_id, name)
        if decl is None:
            return  # alphabet not declared yet — nothing to conform to
        shape = (decl.get("shape") or "categorical").strip().lower()
        if shape == "matrix":
            _validate_matrix_cell(decl, inner)
        elif shape == "vector":
            raise ChannelRejection(
                f"alphabet {name!r} is a vector — it ships axes on the "
                f"alphabet, not entry rows. Do not send extract.conc for it."
            )
        else:
            _validate_categorical_entry(inner)


class ExtractFactShapeChannel(_ExtractChannelBase):
    """Fact-shape registration (FACT alphabet schema): the structured
    contract for predicates that subsequent fact.assert calls must
    satisfy at engagement time."""
    name = "extract.fact_shape"
    schema = ExtractFactShapeBody


class ExtractStoreChannel(_ExtractChannelBase):
    """Store / resource (FACT alphabet entry): a place a fact lives or
    a resource the procedure references."""
    name = "extract.store"
    schema = ExtractStoreBody


class ExtractResourceChannel(_ExtractChannelBase):
    """Task resource capability extracted from mats. Unlike
    ``extract.store`` (FACT/storage material), these entries are sealed
    onto the weapon as runtime resources the task may reference/fetch
    with provenance."""
    name = "extract.resource"
    schema = ExtractResourceBody


class ExtractLineChannel(_ExtractChannelBase):
    """Scope-line (META alphabet): explicit in-scope / out-of-scope
    boundary derived from manifest content."""
    name = "extract.line"
    schema = ExtractLineBody


class ExtractRhemaRuleChannel(_ExtractChannelBase):
    """Rhema rule extracted from manifest content. Gets composed into
    the weapon's ``policy/rhema.md`` at synthesize time. Body carries
    a structured rule (``action`` + ``layer`` + ``pattern``) plus a
    rationale + evidence_quote so the operator can review where the
    agent thought the rule belonged."""
    name = "extract.rhema_rule"
    schema = ExtractRhemaRuleBody


class ExtractBulwarkRuleChannel(_ExtractChannelBase):
    """Bulwark rule extracted from manifest content. Composed into the
    weapon's ``policy/bulwark.md`` under ``## armor: weapon`` so it
    layers on top of operator standing rules at equip time."""
    name = "extract.bulwark_rule"
    schema = ExtractBulwarkRuleBody


class ExtractWallsPathChannel(_ExtractChannelBase):
    """WALLS entry (deny_path / deny_command / approval_path) extracted
    from manifest. Composed into ``policy/WALLS.md``. Operator-mat-
    derived deny paths supplement (or replace) the offsec pack's
    standard ssh/aws/etc. coverage."""
    name = "extract.walls_path"
    schema = ExtractWallsPathBody


# ---- pack-forge schema-declaration channels (the `pen` weapon) ------

class ProposeAlphabetChannel(_ExtractChannelBase):
    """Declare a new pack alphabet (name, shape, fields).

    Non-categorical shapes must declare their shape-native structure so the
    sealed pack carries real axes/cells, not empty shells:
      * ``vector`` — ``axes: [{name, min?, max?, description?}]`` (each a
        scored dimension); the vector ships axes, not entry rows.
      * ``matrix`` — ``axes: [dim, ...]`` (>=1 dimension name) plus
        ``cell_fields: [{name, type}]`` describing each cell's payload.
    """
    name = "propose.alphabet"
    schema = ProposeAlphabetBody

    def validate(self, body: dict) -> None:
        super().validate(body)
        decl = body.get("body") or {}
        shape = (decl.get("shape") or "").strip().lower()
        if shape not in ("categorical", "matrix", "template_set", "vector"):
            raise ChannelRejection(
                "alphabet requires an explicit 'shape': one of "
                "categorical | matrix | template_set | vector"
            )
        if shape == "vector":
            axes = decl.get("axes")
            if not isinstance(axes, list) or not axes:
                raise ChannelRejection(
                    "vector alphabet requires non-empty 'axes': "
                    "[{name, min?, max?, description?}]"
                )
            for ax in axes:
                if not isinstance(ax, dict) or not ax.get("name"):
                    raise ChannelRejection(
                        "each vector axis needs a 'name'"
                    )
        elif shape == "matrix":
            axes = decl.get("axes")
            cell_fields = decl.get("cell_fields")
            if not isinstance(axes, list) or not axes \
                    or not all(isinstance(a, str) and a for a in axes):
                raise ChannelRejection(
                    "matrix alphabet requires non-empty 'axes': "
                    "[dimension_name, ...]"
                )
            if not isinstance(cell_fields, list) or not cell_fields:
                raise ChannelRejection(
                    "matrix alphabet requires non-empty 'cell_fields': "
                    "[{name, type}]"
                )
            cf_names = {f.get("name") if isinstance(f, dict) else f
                        for f in cell_fields}
            overlap = set(axes) & cf_names
            if overlap:
                raise ChannelRejection(
                    f"matrix axes and cell_fields must be disjoint — "
                    f"{sorted(overlap)} is declared as both. Axes are the "
                    f"lookup dimensions (e.g. severity_label); cell_fields are "
                    f"the value(s) stored at each cell (e.g. weight). A field "
                    f"cannot be both."
                )


class ProposeBindingChannel(_ExtractChannelBase):
    """Declare a validator binding (field → rule → alphabet)."""
    name = "propose.binding"
    schema = ProposeBindingBody


class ProposeChunkChannel(_ExtractChannelBase):
    """Declare a chunk-graph node for the pack's chunk_templates."""
    name = "propose.chunk"
    schema = ProposeChunkBody


class ProposeDetectionChannel(_ExtractChannelBase):
    """Declare a manifest_detection keyword."""
    name = "propose.detection"
    schema = ProposeDetectionBody


# ---- reconcile channels (RECONCILE reduce step) --------------------
# The reconcile agent edits EXISTING vocabulary rather than mining new. Each
# channel appends a structured decision to the reconcile-decisions log; the
# driver (loop_driver.run_reconcile) applies them to the pack draft and
# re-checks the seal gates as the acceptance test.

@dataclass
class ReconcileBody:
    """Permissive marker schema — reconcile bodies vary by op, so the base
    field-check is skipped and each channel validates its own op fields."""
    forge_id: str | None = None


class _ReconcileChannelBase(Channel):
    """Append one reconcile decision to the decisions log. Subclasses set
    ``name`` and ``op`` and validate the op-specific fields."""
    schema = ReconcileBody
    sink_subsystem = "reconcile_decisions.jsonl"
    op = ""

    def __init__(self, *, state_root: Path) -> None:
        self.state_root = Path(state_root)

    def _validate_op(self, body: dict) -> None:  # override
        return None

    def validate(self, body: dict) -> None:
        # NB: intentionally does NOT call the base field-checker — reconcile
        # payloads are op-specific and would trip its unknown-field guard.
        if not isinstance(body, dict):
            raise ChannelRejection("reconcile body must be a dict")
        self._validate_op(body)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        import json as _json
        forge_id = _resolve_forge_id(
            self.state_root, ctx.forge_id, body.get("forge_id"),
        )
        decision = dict(body)
        decision.pop("forge_id", None)
        decision["op"] = self.op
        path = fstate.forge_reconcile_decisions_path(self.state_root, forge_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(_json.dumps(decision) + "\n")
        return {"forge_id": forge_id, "op": self.op}


class ReconcileMergeChannel(_ReconcileChannelBase):
    """Collapse semantic-duplicate alphabets. Body: {targets:[names], into:name,
    rationale}."""
    name = "reconcile.merge"
    op = "merge"

    def _validate_op(self, body: dict) -> None:
        if not isinstance(body.get("targets"), list) or len(body["targets"]) < 2:
            raise ChannelRejection("merge needs 'targets': >=2 alphabet names")
        if not isinstance(body.get("into"), str) or not body["into"]:
            raise ChannelRejection("merge needs 'into': the surviving name")


class ReconcileSplitChannel(_ReconcileChannelBase):
    """Split one alphabet into source-scoped alphabets. Body: {name,
    into:[{new_name, element_ids:[...]}], rationale}."""
    name = "reconcile.split"
    op = "split"

    def _validate_op(self, body: dict) -> None:
        if not isinstance(body.get("name"), str) or not body["name"]:
            raise ChannelRejection("split needs 'name'")
        into = body.get("into")
        if not isinstance(into, list) or len(into) < 2:
            raise ChannelRejection("split needs 'into': >=2 {new_name, element_ids}")
        for spec in into:
            if not isinstance(spec, dict) or not spec.get("new_name"):
                raise ChannelRejection("each split target needs a 'new_name'")


class ReconcileResolveCellChannel(_ReconcileChannelBase):
    """Pick the authoritative value at a contested coord. Body: {alphabet,
    coords, keep_cell, rationale}."""
    name = "reconcile.resolve_cell"
    op = "resolve_cell"

    def _validate_op(self, body: dict) -> None:
        if not isinstance(body.get("alphabet"), str) or not body["alphabet"]:
            raise ChannelRejection("resolve_cell needs 'alphabet'")
        if not isinstance(body.get("coords"), dict) or not body["coords"]:
            raise ChannelRejection("resolve_cell needs 'coords'")
        if not isinstance(body.get("keep_cell"), dict) or not body["keep_cell"]:
            raise ChannelRejection("resolve_cell needs 'keep_cell'")


class ReconcileDropChannel(_ReconcileChannelBase):
    """Last resort — drop an unusable alphabet. Body: {name, rationale}."""
    name = "reconcile.drop"
    op = "drop"

    def _validate_op(self, body: dict) -> None:
        if not isinstance(body.get("name"), str) or not body["name"]:
            raise ChannelRejection("drop needs 'name'")
        if not str(body.get("rationale") or "").strip():
            raise ChannelRejection("drop needs a 'rationale' (last resort)")


# ---------- bootstrap ----------

def install_forge_channels(state_root: Path) -> dict[str, Channel]:
    """Register the seven forge-authoring channels in the global
    registry. Idempotent — re-registers (overwrites) on re-call so
    state-root changes between supervisor restarts work cleanly.

    Returns the ``{name: Channel}`` map for callers that want to
    inspect / shadow them.
    """
    state_root = Path(state_root)
    chans: list[Channel] = [
        MatsSearchChannel(state_root=state_root),
        MatsGetChannel(state_root=state_root),
        ExhaustStatusChannel(state_root=state_root),
        ExtractProcChannel(state_root=state_root),
        ExtractConcChannel(state_root=state_root),
        ExtractFactShapeChannel(state_root=state_root),
        ExtractStoreChannel(state_root=state_root),
        ExtractResourceChannel(state_root=state_root),
        ExtractLineChannel(state_root=state_root),
        ExtractRhemaRuleChannel(state_root=state_root),
        ExtractBulwarkRuleChannel(state_root=state_root),
        ExtractWallsPathChannel(state_root=state_root),
        ProposeAlphabetChannel(state_root=state_root),
        ProposeBindingChannel(state_root=state_root),
        ProposeChunkChannel(state_root=state_root),
        ProposeDetectionChannel(state_root=state_root),
        ReconcileMergeChannel(state_root=state_root),
        ReconcileSplitChannel(state_root=state_root),
        ReconcileResolveCellChannel(state_root=state_root),
        ReconcileDropChannel(state_root=state_root),
    ]
    out: dict[str, Channel] = {}
    for ch in chans:
        # registry.register raises on duplicate; unregister first
        # so repeated install_forge_channels() calls are idempotent.
        try:
            registry.unregister_for_tests(ch.name)
        except Exception:  # noqa: BLE001
            pass
        registry.register(ch)
        out[ch.name] = ch
    return out


__all__ = [
    "MatsSearchChannel",
    "MatsGetChannel",
    "ExtractProcChannel",
    "ExtractConcChannel",
    "ExtractFactShapeChannel",
    "ExtractStoreChannel",
    "ExtractResourceChannel",
    "ExtractLineChannel",
    "ExtractRhemaRuleChannel",
    "ExtractBulwarkRuleChannel",
    "ExtractWallsPathChannel",
    "ProposeAlphabetChannel",
    "ProposeBindingChannel",
    "ProposeChunkChannel",
    "ProposeDetectionChannel",
    "install_forge_channels",
]


_ = (Any, field)  # silence unused
