"""WeaponShapes — in-memory map ``crumb_id`` → :class:`ExpectedEvidenceShape`.

Loaded once at engagement start from the sealed weapon's crumbs;
immutable for engagement lifetime (matches Q6-A from the 3.2 design).

The weapon JSON's crumbs may carry an ``expected_evidence_shape``
dict (matching the :mod:`runtime.anomaly.shape` schema). Crumbs with
no shape are excluded — the auto-firer skips them silently (Q7-A).
"""
from __future__ import annotations

import json
from pathlib import Path

from ara.runtime.anomaly.shape import (
    ExpectedEvidenceShape,
    FieldPattern,
    FieldRequirement,
    LineMatcher,
    SectionMatcher,
    StructuralConstraint,
)


def _build_shape(d: dict) -> ExpectedEvidenceShape:
    return ExpectedEvidenceShape(
        required_fields=[FieldRequirement(**r) for r in d.get("required_fields") or []],
        field_patterns={
            name: FieldPattern(**p) for name, p in (d.get("field_patterns") or {}).items()
        },
        unexpected_fields_threshold=int(d.get("unexpected_fields_threshold", 2)),
        structural_constraints=[
            StructuralConstraint(**c) for c in d.get("structural_constraints") or []
        ],
        line_matchers=[LineMatcher(**m) for m in d.get("line_matchers") or []],
        section_matchers=[
            SectionMatcher(
                header_regex=m.get("header_regex", ""),
                cardinality=m.get("cardinality", "required"),
                cardinality_n=m.get("cardinality_n"),
                body_matchers=[LineMatcher(**b) for b in m.get("body_matchers") or []],
                label=m.get("label", ""),
            )
            for m in d.get("section_matchers") or []
        ],
        divergence_threshold=float(d.get("divergence_threshold", 0.5)),
        derived_from=list(d.get("derived_from") or []),
        operator_modified_at=d.get("operator_modified_at"),
    )


class WeaponShapes:
    """Immutable map crumb_id → ExpectedEvidenceShape."""

    def __init__(self, shapes: dict[str, ExpectedEvidenceShape]) -> None:
        self._shapes = dict(shapes)

    def get(self, crumb_id: str) -> ExpectedEvidenceShape | None:
        return self._shapes.get(crumb_id)

    def has(self, crumb_id: str) -> bool:
        return crumb_id in self._shapes

    def known_crumb_ids(self) -> list[str]:
        return sorted(self._shapes)


def load_from_weapon(weapon_dir: Path) -> WeaponShapes:
    """Load shapes from a sealed weapon directory.

    Reads ``weapon.json`` if present; for each crumb with an
    ``expected_evidence_shape`` field, parses it into an
    :class:`ExpectedEvidenceShape`. Crumbs without one are omitted —
    the auto-firer treats them as "no shape declared, skip silently."
    """
    weapon_json = Path(weapon_dir) / "weapon.json"
    if not weapon_json.exists():
        return WeaponShapes({})
    data = json.loads(weapon_json.read_text())
    out: dict[str, ExpectedEvidenceShape] = {}
    for crumb in data.get("crumbs") or []:
        crumb_id = crumb.get("crumb_id") or crumb.get("id")
        shape_dict = crumb.get("expected_evidence_shape")
        if not crumb_id or not shape_dict:
            continue
        try:
            out[crumb_id] = _build_shape(shape_dict)
        except (TypeError, ValueError, KeyError):
            # Malformed shape — skip so a bad crumb doesn't break the
            # whole engagement load. The auto-firer's per-fire crash
            # safety (Q5-A) is the second line of defense.
            continue
    return WeaponShapes(out)


def empty() -> WeaponShapes:
    return WeaponShapes({})
