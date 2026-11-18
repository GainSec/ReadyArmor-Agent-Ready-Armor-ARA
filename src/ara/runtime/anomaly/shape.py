"""ExpectedEvidenceShape grammar (6 pieces): JSON-style required fields
+ patterns + structural constraints, plus LineMatcher / SectionMatcher
for multi-line text outputs (nmap, wpscan, etc.).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class FieldRequirement:
    name: str
    cardinality: Literal["one", "one_or_more", "exact_n", "any"] = "one"
    cardinality_n: int | None = None


@dataclass(frozen=True)
class FieldPattern:
    name: str
    kind: Literal["regex", "value_in", "type_only"]
    pattern: Any  # str | list | dict


@dataclass(frozen=True)
class StructuralConstraint:
    kind: Literal[
        "http_status_in", "header_present", "header_value_in",
        "content_type_in", "size_below", "encoding_in",
    ]
    body: dict


@dataclass(frozen=True)
class LineMatcher:
    line_regex: str
    cardinality: Literal[
        "one", "one_or_more", "zero_or_more", "exact_n", "any",
    ] = "one_or_more"
    cardinality_n: int | None = None
    label: str = ""


@dataclass(frozen=True)
class SectionMatcher:
    header_regex: str
    cardinality: Literal["required", "optional", "exact_n", "any"] = "required"
    cardinality_n: int | None = None
    body_matchers: list[LineMatcher] = field(default_factory=list)
    label: str = ""


@dataclass(frozen=True)
class ExpectedEvidenceShape:
    required_fields: list[FieldRequirement] = field(default_factory=list)
    field_patterns: dict[str, FieldPattern] = field(default_factory=dict)
    unexpected_fields_threshold: int = 2
    structural_constraints: list[StructuralConstraint] = field(default_factory=list)
    line_matchers: list[LineMatcher] = field(default_factory=list)
    section_matchers: list[SectionMatcher] = field(default_factory=list)
    divergence_threshold: float = 0.5
    derived_from: list[str] = field(default_factory=list)
    operator_modified_at: float | None = None

    def hash(self) -> str:
        canon = json.dumps(self.to_jsonable(), sort_keys=True, default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(canon).hexdigest()

    def to_jsonable(self) -> dict:
        return {
            "required_fields": [asdict(r) for r in self.required_fields],
            "field_patterns": {k: asdict(p) for k, p in self.field_patterns.items()},
            "unexpected_fields_threshold": self.unexpected_fields_threshold,
            "structural_constraints": [asdict(s) for s in self.structural_constraints],
            "line_matchers": [asdict(m) for m in self.line_matchers],
            "section_matchers": [
                {**asdict(m), "body_matchers": [asdict(b) for b in m.body_matchers]}
                for m in self.section_matchers
            ],
            "divergence_threshold": self.divergence_threshold,
            "derived_from": list(self.derived_from),
            "operator_modified_at": self.operator_modified_at,
        }
