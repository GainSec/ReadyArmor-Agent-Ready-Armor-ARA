"""Alphabet pack data model.

A pack is the declaration of what alphabets a task domain has. Forge
ships packs as JSON files under ``forge/alphabets/packs/``; a weapon
selects one via ``ForgeConfig.alphabet_pack``. Operators can override
individual alphabets in their weapon's ``alphabets/_overrides.json``.

Three alphabet shapes in v1:
- ``categorical`` — flat list of typed entries (id + metadata)
- ``matrix``      — N-axis matrix; cells carry typed values
- ``template_set``— list of named templates with foreign keys + body

Adding a new shape (e.g. ``vector``, ``graph``) is a v1.5 follow-up
once a second domain pack is in real use.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from enum import Enum
from pathlib import Path
from typing import Any


class ShapeKind(str, Enum):
    CATEGORICAL = "categorical"
    MATRIX = "matrix"
    TEMPLATE_SET = "template_set"
    VECTOR = "vector"


@dataclass(frozen=True)
class FieldSpec:
    """Schema for one field of an alphabet entry.

    Type strings recognised in v1: ``str``, ``str?`` (optional),
    ``int``, ``float``, ``bool``, ``list[str]``,
    ``ref:<other_alphabet>`` (foreign-key reference; the value must
    appear as an entry id in the named alphabet).
    """
    name: str
    type: str

    @property
    def required(self) -> bool:
        return not self.type.endswith("?")

    @property
    def base_type(self) -> str:
        return self.type.rstrip("?")

    @property
    def is_ref(self) -> bool:
        return self.type.startswith("ref:")

    @property
    def ref_target(self) -> str | None:
        if self.type.startswith("ref:"):
            base = self.base_type
            return base[len("ref:"):]
        return None


@dataclass(frozen=True)
class CategoricalSchema:
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass(frozen=True)
class MatrixSchema:
    axes: list[str] = field(default_factory=list)
    cell_fields: list[FieldSpec] = field(default_factory=list)


@dataclass(frozen=True)
class TemplateSetSchema:
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass(frozen=True)
class VectorAxisSpec:
    name: str
    description: str = ""
    min: int = 0
    max: int = 3


@dataclass(frozen=True)
class VectorSchema:
    """Vector alphabet: a finding-level field that maps each declared
    axis to an int (or float, depending on type) in [min, max].

    Offsec uses this for ``impact = {confidentiality, integrity,
    availability}``; a code-review pack would declare
    ``{correctness, maintainability, performance}``.
    """
    axes: list[VectorAxisSpec] = field(default_factory=list)


@dataclass(frozen=True)
class AlphabetDeclaration:
    name: str
    shape: ShapeKind
    schema: CategoricalSchema | MatrixSchema | TemplateSetSchema | VectorSchema
    starter: list[dict] = field(default_factory=list)
    buckets: dict[str, float] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class ValidatorBinding:
    """Maps a finding-record field to a rule + alphabet."""
    field: str       # e.g. "category", "severity_cell", "fix_template_id"
    rule: str        # FIELD_IN_ALPHABET | FIELD_IN_MATRIX | FOREIGN_KEY_VALID
    alphabet: str    # name of an alphabet declared in this pack


@dataclass(frozen=True)
class ManifestDetectionConfig:
    """Pack-supplied tuning for forge's manifest kind detectors.

    v1 only honors ``report_section.keywords`` — a list of regex
    fragments that the ExampleReportDetector OR-joins to identify
    finding sections in markdown mats. Empty list → detector falls
    back to its hard-coded defaults.
    """
    report_section_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AlphabetPack:
    name: str
    version: str
    description: str
    alphabets: list[AlphabetDeclaration]
    validator_bindings: list[ValidatorBinding] = field(default_factory=list)
    manifest_detection: ManifestDetectionConfig = field(
        default_factory=ManifestDetectionConfig,
    )
    # Pack-supplied chunk templates. Each entry is a serialised
    # :class:`ara.forge.chunks.chunkdef.ChunkDef` (id, title,
    # prompt_fragment, gems, tool_surface, exits, can_emit_finding,
    # is_terminal, is_entry). Synthesize copies these into
    # ``weapon_draft.chunks`` when the operator hasn't authored any.
    chunk_templates: list[dict] = field(default_factory=list)
    # Pack-shipped ARA policy stack — bulwark / WALLS / rhema text
    # baked into every sealed weapon and hot-swapped into state_root
    # at equip time. Lets ``ara weapon equip <id>`` activate the whole
    # forge-v1 policy stack with one command; ``ara weapon unequip``
    # restores the prior operator-set rules.
    policy_defaults: dict[str, str] = field(default_factory=dict)

    def get(self, alphabet_name: str) -> AlphabetDeclaration | None:
        for a in self.alphabets:
            if a.name == alphabet_name:
                return a
        return None

    def to_jsonable(self) -> dict:
        out = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "alphabets": [_alpha_to_jsonable(a) for a in self.alphabets],
            "validator_bindings": [asdict(b) for b in self.validator_bindings],
        }
        if self.manifest_detection.report_section_keywords:
            out["manifest_detection"] = {
                "report_section": {
                    "keywords": list(self.manifest_detection.report_section_keywords),
                },
            }
        if self.chunk_templates:
            out["chunk_templates"] = [dict(c) for c in self.chunk_templates]
        if self.policy_defaults:
            out["policy_defaults"] = dict(self.policy_defaults)
        return out


def _alpha_to_jsonable(a: AlphabetDeclaration) -> dict:
    out: dict = {
        "name": a.name,
        "shape": a.shape.value,
        "description": a.description,
    }
    if isinstance(a.schema, CategoricalSchema):
        out["schema"] = {
            "fields": [{"name": f.name, "type": f.type} for f in a.schema.fields],
        }
    elif isinstance(a.schema, MatrixSchema):
        out["schema"] = {
            "axes": list(a.schema.axes),
            "cell_fields": [{"name": f.name, "type": f.type} for f in a.schema.cell_fields],
        }
    elif isinstance(a.schema, TemplateSetSchema):
        out["schema"] = {
            "fields": [{"name": f.name, "type": f.type} for f in a.schema.fields],
        }
    elif isinstance(a.schema, VectorSchema):
        out["schema"] = {
            "axes": [
                {"name": ax.name, "description": ax.description,
                 "min": ax.min, "max": ax.max}
                for ax in a.schema.axes
            ],
        }
    if a.starter:
        out["starter"] = list(a.starter)
    if a.buckets:
        out["buckets"] = dict(a.buckets)
    return out


def _spec(cls, d: dict):
    """Build a spec dataclass from a free-form agent dict, ignoring any keys
    the class doesn't declare. The forge agent emits richer field/axis dicts
    (e.g. a ``description`` per axis) than the v1 schema stores; dropping the
    extras keeps assembly robust instead of raising on an unexpected kwarg."""
    known = {f.name for f in dataclass_fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})


def _alpha_from_jsonable(d: dict) -> AlphabetDeclaration:
    shape = ShapeKind(d["shape"])
    sd = d.get("schema") or {}
    schema: Any
    if shape == ShapeKind.CATEGORICAL:
        schema = CategoricalSchema(
            fields=[_spec(FieldSpec, f) for f in sd.get("fields") or []],
        )
    elif shape == ShapeKind.MATRIX:
        schema = MatrixSchema(
            axes=list(sd.get("axes") or []),
            cell_fields=[_spec(FieldSpec, f) for f in sd.get("cell_fields") or []],
        )
    elif shape == ShapeKind.TEMPLATE_SET:
        schema = TemplateSetSchema(
            fields=[_spec(FieldSpec, f) for f in sd.get("fields") or []],
        )
    elif shape == ShapeKind.VECTOR:
        schema = VectorSchema(
            axes=[_spec(VectorAxisSpec, ax) for ax in sd.get("axes") or []],
        )
    else:
        raise ValueError(f"unknown shape {shape}")
    return AlphabetDeclaration(
        name=d["name"],
        shape=shape,
        schema=schema,
        starter=list(d.get("starter") or []),
        buckets=dict(d.get("buckets") or {}),
        description=d.get("description", ""),
    )


def load_pack(path: Path) -> AlphabetPack:
    data = json.loads(Path(path).read_text())
    detect_raw = (data.get("manifest_detection") or {}).get("report_section") or {}
    return AlphabetPack(
        name=data["name"],
        version=data.get("version", "1"),
        description=data.get("description", ""),
        alphabets=[_alpha_from_jsonable(a) for a in data.get("alphabets") or []],
        validator_bindings=[
            ValidatorBinding(**b) for b in data.get("validator_bindings") or []
        ],
        manifest_detection=ManifestDetectionConfig(
            report_section_keywords=list(detect_raw.get("keywords") or []),
        ),
        chunk_templates=list(data.get("chunk_templates") or []),
        policy_defaults=dict(data.get("policy_defaults") or {}),
    )


_PACKS_DIR = Path(__file__).parent / "packs"


def builtin_pack_path(name: str) -> Path:
    return _PACKS_DIR / f"{name}.json"


def load_builtin_pack(name: str) -> AlphabetPack:
    path = builtin_pack_path(name)
    if not path.exists():
        raise FileNotFoundError(f"no built-in alphabet pack named {name!r}")
    return load_pack(path)


def list_builtin_packs() -> list[str]:
    """Return the names of every JSON pack shipped under ``packs/``,
    sorted alphabetically."""
    if not _PACKS_DIR.is_dir():
        return []
    return sorted(p.stem for p in _PACKS_DIR.glob("*.json"))


def write_pack(path: Path, pack: AlphabetPack) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack.to_jsonable(), sort_keys=True, indent=2))


# ---- runtime pack store (operator-forged packs) --------------------

def runtime_packs_dir(state_root) -> Path:
    """Where pack-forge writes sealed packs: ``<state_root>/packs``."""
    return Path(state_root) / "packs"


def resolve_pack(name: str, state_root) -> AlphabetPack:
    """Resolve a pack by name: the runtime store (``<state_root>/packs``)
    wins over the shipped built-in dir, so weapon-forge can target a
    freshly-forged pack immediately."""
    rp = runtime_packs_dir(state_root) / f"{name}.json"
    if rp.exists():
        return load_pack(rp)
    return load_builtin_pack(name)


def list_packs(state_root) -> list[str]:
    """Names of every resolvable pack — union of built-in + runtime store,
    sorted, deduped (runtime and built-in of the same name collapse)."""
    names = set(list_builtin_packs())
    rp = runtime_packs_dir(state_root)
    if rp.is_dir():
        names |= {p.stem for p in rp.glob("*.json")}
    return sorted(names)
