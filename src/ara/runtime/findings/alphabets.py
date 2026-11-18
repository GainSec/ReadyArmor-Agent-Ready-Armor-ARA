"""Per-weapon alphabets: category enum, severity rubric, fix library.

Loaded from the sealed weapon directory. v1 supports JSON-on-disk; YAML
parsing is deferred (would require an extra dep). When the weapon ships
without an alphabet, callers receive an :class:`Alphabets` with empty
maps and validators reject anything not bypassed by the operator.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CategoryEntry:
    id: str
    label: str = ""
    description: str = ""
    requires_proposed: bool = False


@dataclass(frozen=True)
class CategoryEnum:
    entries: dict[str, CategoryEntry] = field(default_factory=dict)

    def has(self, cat_id: str) -> bool:
        return cat_id in self.entries

    def is_other(self, cat_id: str) -> bool:
        e = self.entries.get(cat_id)
        return bool(e and e.requires_proposed)


@dataclass(frozen=True)
class RubricCell:
    score: float
    name: str  # severity_name


@dataclass(frozen=True)
class SeverityRubric:
    data_classes: list[str] = field(default_factory=list)
    accesses: list[str] = field(default_factory=list)
    matrix: dict[str, dict[str, RubricCell]] = field(default_factory=dict)

    def has_cell(self, data_class: str, access: str) -> bool:
        row = self.matrix.get(data_class)
        return bool(row and access in row)

    def lookup(self, data_class: str, access: str) -> RubricCell | None:
        row = self.matrix.get(data_class)
        if not row:
            return None
        return row.get(access)


@dataclass(frozen=True)
class FixTemplate:
    id: str
    title: str
    applicable_categories: list[str] = field(default_factory=list)
    body: str = ""


@dataclass(frozen=True)
class FixLibrary:
    templates: dict[str, FixTemplate] = field(default_factory=dict)

    def has(self, fix_id: str) -> bool:
        return fix_id in self.templates

    def applicable(self, fix_id: str, category: str) -> bool:
        t = self.templates.get(fix_id)
        if t is None:
            return False
        return category in t.applicable_categories


@dataclass(frozen=True)
class Alphabets:
    categories: CategoryEnum
    rubric: SeverityRubric
    fix_library: FixLibrary


# ---------- loaders ----------

def load_category_enum(path: Path) -> CategoryEnum:
    """Supports projector format ``{shape: "categorical", entries: [...]}``
    and legacy format ``{categories: [...]}``."""
    if not path.exists():
        return CategoryEnum()
    data = json.loads(path.read_text())
    raw_entries = data.get("entries") if data.get("shape") == "categorical" \
        else data.get("categories", [])
    out: dict[str, CategoryEntry] = {}
    for raw in raw_entries or []:
        entry = CategoryEntry(
            id=raw["id"],
            label=raw.get("label", ""),
            description=raw.get("description", ""),
            requires_proposed=bool(raw.get("requires_proposed", False)),
        )
        out[entry.id] = entry
    return CategoryEnum(entries=out)


def load_severity_rubric(path: Path) -> SeverityRubric:
    """Load a severity rubric from disk.

    Supports two formats:
    - **Projector format** (alphabet pack engine output):
      ``{shape: "matrix", axes: ["data_class", "access"],
         cells: [{coords: {...}, score, bucket}], buckets: {...}}``
    - **Legacy format** (hand-seeded by tests / pre-pack tooling):
      ``{axes: {data_class: [...], access: [...]},
         matrix: {dc: {ax: {score, name}}}}``
    """
    if not path.exists():
        return SeverityRubric()
    data = json.loads(path.read_text())
    if data.get("shape") == "matrix" or isinstance(data.get("axes"), list):
        # Projector format
        axes = list(data.get("axes") or [])
        cells = data.get("cells") or []
        matrix: dict[str, dict[str, RubricCell]] = {}
        data_classes: list[str] = []
        accesses: list[str] = []
        for cell in cells:
            coords = cell.get("coords") or {}
            if len(axes) < 2:
                continue
            dc = coords.get(axes[0], "")
            ax = coords.get(axes[1], "")
            if not dc or not ax:
                continue
            if dc not in data_classes:
                data_classes.append(dc)
            if ax not in accesses:
                accesses.append(ax)
            matrix.setdefault(dc, {})[ax] = RubricCell(
                score=float(cell.get("score", 0.0)),
                name=str(cell.get("bucket", cell.get("name", ""))),
            )
        return SeverityRubric(
            data_classes=data_classes, accesses=accesses, matrix=matrix,
        )
    # Legacy format
    axes_dict = data.get("axes") or {}
    matrix_raw = data.get("matrix") or {}
    matrix = {}
    for dc, row in matrix_raw.items():
        matrix[dc] = {
            access: RubricCell(
                score=float(cell["score"]),
                name=str(cell.get("name", cell.get("bucket", ""))),
            )
            for access, cell in row.items()
        }
    return SeverityRubric(
        data_classes=list(axes_dict.get("data_class") or []),
        accesses=list(axes_dict.get("access") or []),
        matrix=matrix,
    )


def load_fix_library(path: Path) -> FixLibrary:
    """Supports projector format ``{shape: "template_set", templates: [...]}``
    and legacy format ``{templates: [...]}``."""
    if not path.exists():
        return FixLibrary()
    data = json.loads(path.read_text())
    raw_templates = data.get("templates") or []
    out: dict[str, FixTemplate] = {}
    for raw in raw_templates:
        applicable = raw.get("applicable_categories") or []
        if isinstance(applicable, str):
            applicable = [applicable]
        t = FixTemplate(
            id=raw["id"],
            title=raw.get("title", ""),
            applicable_categories=list(applicable),
            body=raw.get("body", ""),
        )
        out[t.id] = t
    return FixLibrary(templates=out)


def load_alphabets(weapon_dir: Path) -> Alphabets:
    """Load the three alphabets from ``<weapon>/alphabets/*.json``.
    Missing files yield empty alphabets (validator will then reject
    findings whose category/severity-cell aren't covered).
    """
    a_dir = Path(weapon_dir) / "alphabets"
    return Alphabets(
        categories=load_category_enum(a_dir / "category_enum.json"),
        rubric=load_severity_rubric(a_dir / "severity_rubric.json"),
        fix_library=load_fix_library(a_dir / "fix_library.json"),
    )


# ---------- writers (forge synthesis side) ----------

def write_category_enum(path: Path, enum: CategoryEnum) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "categories": [
            {
                "id": e.id, "label": e.label,
                "description": e.description,
                "requires_proposed": e.requires_proposed,
            }
            for e in enum.entries.values()
        ],
    }, sort_keys=True, indent=2))


def write_severity_rubric(path: Path, rubric: SeverityRubric) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    matrix = {
        dc: {
            access: {"score": cell.score, "name": cell.name}
            for access, cell in row.items()
        }
        for dc, row in rubric.matrix.items()
    }
    path.write_text(json.dumps({
        "axes": {"data_class": rubric.data_classes, "access": rubric.accesses},
        "matrix": matrix,
    }, sort_keys=True, indent=2))


def write_fix_library(path: Path, lib: FixLibrary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "templates": [
            {
                "id": t.id, "title": t.title,
                "applicable_categories": t.applicable_categories,
                "body": t.body,
            }
            for t in lib.templates.values()
        ],
    }, sort_keys=True, indent=2))
