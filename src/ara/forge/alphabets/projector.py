"""Project ``extract.conc`` events into structured alphabet entries.

Each extraction body must carry an ``alphabet_name`` discriminator
naming one of the pack's declared alphabets, plus an ``entry`` (or
``cell`` for matrix shapes) matching that alphabet's schema.

Output: a :class:`DerivedAlphabets` bundle keyed by alphabet name,
plus a list of conflicts (same-key extractions disagreeing on body).
The seal phase serializes each derived alphabet under
``<weapon>/alphabets/<name>.json``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ara.forge.alphabets.pack import (
    AlphabetDeclaration,
    AlphabetPack,
    CategoricalSchema,
    FieldSpec,
    MatrixSchema,
    ShapeKind,
    TemplateSetSchema,
    VectorSchema,
)


@dataclass(frozen=True)
class Conflict:
    alphabet_name: str
    key: str            # entry id, or "axis_a/axis_b" for matrix cell
    competing_values: list[dict]


@dataclass
class CategoricalAlphabet:
    name: str
    entries: dict[str, dict] = field(default_factory=dict)


@dataclass
class MatrixAlphabet:
    name: str
    axes: list[str] = field(default_factory=list)
    cells: dict[tuple[str, ...], dict] = field(default_factory=dict)
    buckets: dict[str, float] = field(default_factory=dict)


@dataclass
class TemplateSetAlphabet:
    name: str
    templates: dict[str, dict] = field(default_factory=dict)


@dataclass
class VectorAlphabet:
    """Vector alphabets are pack-declared, not extraction-derived.

    The forge copies the pack's axis declarations directly (no
    projection). Carried in DerivedAlphabets purely so seal/validator
    consumers have a uniform read surface.
    """
    name: str
    axes: list[dict] = field(default_factory=list)  # serialized VectorAxisSpec


@dataclass
class DerivedAlphabets:
    pack: AlphabetPack
    categorical: dict[str, CategoricalAlphabet] = field(default_factory=dict)
    matrix: dict[str, MatrixAlphabet] = field(default_factory=dict)
    template_set: dict[str, TemplateSetAlphabet] = field(default_factory=dict)
    vector: dict[str, VectorAlphabet] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def empty_alphabets(self) -> list[str]:
        out: list[str] = []
        for a in self.pack.alphabets:
            if a.shape == ShapeKind.CATEGORICAL:
                if not self.categorical.get(a.name) or not self.categorical[a.name].entries:
                    out.append(a.name)
            elif a.shape == ShapeKind.MATRIX:
                if not self.matrix.get(a.name) or not self.matrix[a.name].cells:
                    out.append(a.name)
            elif a.shape == ShapeKind.TEMPLATE_SET:
                if not self.template_set.get(a.name) or not self.template_set[a.name].templates:
                    out.append(a.name)
            elif a.shape == ShapeKind.VECTOR:
                # Vector alphabets are pack-declared (axes), not
                # extraction-derived. They're "empty" only if the pack
                # itself declared no axes — almost certainly a pack bug.
                v = self.vector.get(a.name)
                if not v or not v.axes:
                    out.append(a.name)
        return out


# ---------- type checks ----------

def _check_field(value: Any, spec: FieldSpec) -> str | None:
    """Return error string or None if the value matches the spec."""
    if value is None:
        return None if not spec.required else f"required field {spec.name!r} missing"
    base = spec.base_type
    if base == "str":
        if not isinstance(value, str):
            return f"{spec.name!r}: expected str, got {type(value).__name__}"
    elif base == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{spec.name!r}: expected int, got {type(value).__name__}"
    elif base == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{spec.name!r}: expected number, got {type(value).__name__}"
    elif base == "bool":
        if not isinstance(value, bool):
            return f"{spec.name!r}: expected bool, got {type(value).__name__}"
    elif base == "list[str]":
        if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            return f"{spec.name!r}: expected list[str]"
    elif spec.is_ref:
        # ref types are validated cross-alphabet later; here just check it's a string
        if not isinstance(value, (str, list)):
            return f"{spec.name!r}: ref must be str or list[str]"
    return None


def _validate_entry(entry: dict, fields: list[FieldSpec]) -> list[str]:
    errors: list[str] = []
    spec_by_name = {f.name: f for f in fields}
    for spec in fields:
        if spec.required and entry.get(spec.name) is None:
            errors.append(f"required field {spec.name!r} missing")
            continue
        if spec.name in entry:
            err = _check_field(entry[spec.name], spec)
            if err:
                errors.append(err)
    unknown = set(entry) - set(spec_by_name)
    if unknown:
        errors.append(f"unknown fields: {sorted(unknown)}")
    return errors


# ---------- per-shape projection ----------

def _project_categorical(
    decl: AlphabetDeclaration,
    extractions: list[dict],
    conflicts: list[Conflict],
) -> CategoricalAlphabet:
    assert isinstance(decl.schema, CategoricalSchema)
    out = CategoricalAlphabet(name=decl.name)
    for body in extractions:
        entry = body.get("entry") or {}
        errors = _validate_entry(entry, decl.schema.fields)
        if errors:
            raise ValueError(
                f"alphabet {decl.name!r} extraction invalid: {errors}; body={body!r}"
            )
        eid = entry.get("id")
        if not eid:
            raise ValueError(f"alphabet {decl.name!r} entry missing id: {entry}")
        if eid in out.entries:
            existing = out.entries[eid]
            if existing != entry:
                conflicts.append(Conflict(
                    alphabet_name=decl.name,
                    key=eid,
                    competing_values=[existing, entry],
                ))
            continue  # first-wins; conflict captured for operator review
        out.entries[eid] = entry
    return out


def _project_matrix(
    decl: AlphabetDeclaration,
    extractions: list[dict],
    conflicts: list[Conflict],
) -> MatrixAlphabet:
    assert isinstance(decl.schema, MatrixSchema)
    out = MatrixAlphabet(
        name=decl.name,
        axes=list(decl.schema.axes),
        buckets=dict(decl.buckets),
    )
    for body in extractions:
        cell = body.get("cell") or {}
        # axis values
        coords_raw = body.get("coords") or {}
        coords: list[str] = []
        for ax in decl.schema.axes:
            v = coords_raw.get(ax)
            if not isinstance(v, str) or not v:
                raise ValueError(
                    f"alphabet {decl.name!r}: missing/non-string coord for axis {ax!r}"
                )
            coords.append(v)
        key = tuple(coords)
        errors = _validate_entry(cell, decl.schema.cell_fields)
        if errors:
            raise ValueError(
                f"alphabet {decl.name!r} cell invalid: {errors}; body={body!r}"
            )
        if key in out.cells:
            existing = out.cells[key]
            if existing != cell:
                conflicts.append(Conflict(
                    alphabet_name=decl.name,
                    key="/".join(coords),
                    competing_values=[existing, cell],
                ))
            continue
        out.cells[key] = cell
    return out


def _project_template_set(
    decl: AlphabetDeclaration,
    extractions: list[dict],
    conflicts: list[Conflict],
) -> TemplateSetAlphabet:
    assert isinstance(decl.schema, TemplateSetSchema)
    out = TemplateSetAlphabet(name=decl.name)
    for body in extractions:
        entry = body.get("entry") or {}
        errors = _validate_entry(entry, decl.schema.fields)
        if errors:
            raise ValueError(
                f"alphabet {decl.name!r} template invalid: {errors}; body={body!r}"
            )
        tid = entry.get("id")
        if not tid:
            raise ValueError(f"alphabet {decl.name!r} template missing id: {entry}")
        if tid in out.templates:
            existing = out.templates[tid]
            if existing != entry:
                conflicts.append(Conflict(
                    alphabet_name=decl.name,
                    key=tid,
                    competing_values=[existing, entry],
                ))
            continue
        out.templates[tid] = entry
    return out


# ---------- public projection entry point ----------

def project(
    extractions: list[dict],
    pack: AlphabetPack,
) -> DerivedAlphabets:
    """Project every CONC extraction body into the alphabet it names.

    Each ``body`` must carry ``alphabet_name`` keying a pack alphabet,
    plus either ``entry`` (categorical/template_set) or
    ``coords``+``cell`` (matrix).
    """
    by_alphabet: dict[str, list[dict]] = {a.name: [] for a in pack.alphabets}
    for ev in extractions:
        body = ev.get("body") or ev
        alphabet_name = body.get("alphabet_name")
        if not alphabet_name:
            continue  # not an alphabet extraction
        if alphabet_name not in by_alphabet:
            raise ValueError(
                f"extraction targets unknown alphabet {alphabet_name!r}; "
                f"declared in pack: {sorted(by_alphabet)}"
            )
        by_alphabet[alphabet_name].append(body)

    derived = DerivedAlphabets(pack=pack)
    for decl in pack.alphabets:
        rows = by_alphabet[decl.name]
        if decl.shape == ShapeKind.CATEGORICAL:
            derived.categorical[decl.name] = _project_categorical(decl, rows, derived.conflicts)
        elif decl.shape == ShapeKind.MATRIX:
            derived.matrix[decl.name] = _project_matrix(decl, rows, derived.conflicts)
        elif decl.shape == ShapeKind.TEMPLATE_SET:
            derived.template_set[decl.name] = _project_template_set(
                decl, rows, derived.conflicts,
            )
        elif decl.shape == ShapeKind.VECTOR:
            assert isinstance(decl.schema, VectorSchema)
            if rows:
                raise ValueError(
                    f"vector alphabet {decl.name!r} doesn't accept "
                    f"extractions; declare axes in the pack instead "
                    f"(got {len(rows)} extractions)"
                )
            derived.vector[decl.name] = VectorAlphabet(
                name=decl.name,
                axes=[
                    {"name": ax.name, "description": ax.description,
                     "min": ax.min, "max": ax.max}
                    for ax in decl.schema.axes
                ],
            )
    # Foreign-key validation deferred — callers run validate_foreign_keys
    # AFTER any merge_starter so starter-only categories are reachable
    # by template-set foreign keys.
    return derived


def validate_foreign_keys(derived: DerivedAlphabets) -> None:
    """For each ``ref:other_alphabet`` field, confirm referenced ids exist."""
    for decl in derived.pack.alphabets:
        if decl.shape == ShapeKind.CATEGORICAL:
            entries = derived.categorical[decl.name].entries.values()
            schema_fields = decl.schema.fields  # type: ignore[union-attr]
        elif decl.shape == ShapeKind.TEMPLATE_SET:
            entries = derived.template_set[decl.name].templates.values()
            schema_fields = decl.schema.fields  # type: ignore[union-attr]
        else:
            continue
        ref_fields = [f for f in schema_fields if f.is_ref]
        if not ref_fields:
            continue
        for entry in entries:
            for f in ref_fields:
                target = f.ref_target
                if target is None:
                    continue
                target_alpha = derived.pack.get(target)
                if target_alpha is None:
                    raise ValueError(
                        f"alphabet {decl.name!r} ref:{target} → unknown alphabet"
                    )
                # gather ids from target alphabet
                if target_alpha.shape == ShapeKind.CATEGORICAL:
                    target_ids = set(derived.categorical[target].entries)
                elif target_alpha.shape == ShapeKind.TEMPLATE_SET:
                    target_ids = set(derived.template_set[target].templates)
                else:
                    target_ids = set()
                value = entry.get(f.name)
                refs = value if isinstance(value, list) else ([value] if value else [])
                for r in refs:
                    if r not in target_ids:
                        raise ValueError(
                            f"alphabet {decl.name!r} entry {entry.get('id')!r}: "
                            f"{f.name}={r!r} not in {target!r}"
                        )


# ---------- merge starter ----------

def _starter_key(entry: dict):
    """A stable id for a categorical/template_set starter row. The agent
    doesn't always emit an ``id``; fall back to label/name/value (slugged)
    so a well-formed entry that merely lacks an explicit id still merges."""
    import re
    for k in ("id", "label", "name", "value"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"[^a-z0-9]+", "_", v.strip().lower()).strip("_") \
                if k != "id" else v
    return None


def merge_starter(derived: DerivedAlphabets) -> None:
    """Additively merge starter entries from the pack into each
    alphabet. Operator-extracted entries always win on id/coord
    collisions (their entry stays, starter is skipped). Each starter
    entry that survives is tagged ``source="starter"`` so the seal
    record can audit operator-vs-pack provenance.

    Why additive rather than skip-if-non-empty: foreign-key references
    in starter templates (e.g. ``fix_library`` → ``category``)
    presuppose the corresponding starter ids exist in the target
    alphabet. With skip-if-non-empty, a partial extraction in
    ``category`` would erase every starter category id, breaking
    fk refs in ``fix_library`` starter templates. Additive merge
    keeps both intact.
    """
    for decl in derived.pack.alphabets:
        if not decl.starter:
            continue
        if decl.shape == ShapeKind.CATEGORICAL:
            alpha = derived.categorical[decl.name]
            for entry in decl.starter:
                e = dict(entry)
                e["source"] = "starter"
                key = _starter_key(e)
                if key is None:
                    continue  # unkeyed starter row — skip rather than crash
                e.setdefault("id", key)
                alpha.entries.setdefault(key, e)
        elif decl.shape == ShapeKind.TEMPLATE_SET:
            alpha = derived.template_set[decl.name]
            for entry in decl.starter:
                e = dict(entry)
                e["source"] = "starter"
                key = _starter_key(e)
                if key is None:
                    continue
                e.setdefault("id", key)
                alpha.templates.setdefault(key, e)
        elif decl.shape == ShapeKind.MATRIX:
            alpha = derived.matrix[decl.name]
            assert isinstance(decl.schema, MatrixSchema)
            axes = list(decl.schema.axes)
            for entry in decl.starter:
                # Starter shape: ``{"coords": {axis: value, ...},
                # "cell": {field: value, ...}}``. Tolerate the same
                # body shape that ``record_extraction(extract.severity_cell, ...)``
                # would produce — keeps starter authors from having to
                # learn a separate format.
                coords_raw = entry.get("coords") or {}
                cell = entry.get("cell") or {}
                if not coords_raw or not cell:
                    continue
                coord_tuple: list[str] = []
                ok = True
                for ax in axes:
                    v = coords_raw.get(ax)
                    if not isinstance(v, str) or not v:
                        ok = False
                        break
                    coord_tuple.append(v)
                if not ok:
                    continue
                key = tuple(coord_tuple)
                if key in alpha.cells:
                    continue  # operator-extracted cell wins
                cell_with_source = dict(cell)
                cell_with_source["source"] = "starter"
                alpha.cells[key] = cell_with_source
        # ShapeKind.VECTOR — pack ships the axes themselves; no per-entry
        # starter content applies. Already populated via _project_vector
        # at projection time.


# ---------- write ----------

def write_derived(weapon_dir, derived: DerivedAlphabets) -> None:
    """Serialize each derived alphabet under
    ``<weapon_dir>/alphabets/<name>.json``."""
    from pathlib import Path
    a_dir = Path(weapon_dir) / "alphabets"
    a_dir.mkdir(parents=True, exist_ok=True)
    for decl in derived.pack.alphabets:
        path = a_dir / f"{decl.name}.json"
        payload: dict
        if decl.shape == ShapeKind.CATEGORICAL:
            payload = {
                "shape": "categorical",
                "entries": list(derived.categorical[decl.name].entries.values()),
            }
        elif decl.shape == ShapeKind.MATRIX:
            m = derived.matrix[decl.name]
            payload = {
                "shape": "matrix",
                "axes": m.axes,
                "buckets": m.buckets,
                "cells": [
                    {"coords": dict(zip(m.axes, list(coords))), **cell}
                    for coords, cell in m.cells.items()
                ],
            }
        elif decl.shape == ShapeKind.TEMPLATE_SET:
            payload = {
                "shape": "template_set",
                "templates": list(derived.template_set[decl.name].templates.values()),
            }
        elif decl.shape == ShapeKind.VECTOR:
            payload = {
                "shape": "vector",
                "axes": list(derived.vector[decl.name].axes),
            }
        else:
            continue
        path.write_text(json.dumps(payload, sort_keys=True, indent=2))
    # Also write the pack manifest for audit
    (a_dir / "_pack.json").write_text(
        json.dumps({
            "name": derived.pack.name,
            "version": derived.pack.version,
        }, sort_keys=True, indent=2)
    )
    if derived.conflicts:
        (a_dir / "_conflicts.json").write_text(json.dumps(
            [
                {"alphabet": c.alphabet_name, "key": c.key,
                 "competing_values": c.competing_values}
                for c in derived.conflicts
            ],
            sort_keys=True, indent=2,
        ))
