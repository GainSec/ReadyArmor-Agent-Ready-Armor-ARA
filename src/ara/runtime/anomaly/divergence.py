"""Divergence calculator. Pure structural comparison — no LLM."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ara.runtime.anomaly.shape import (
    ExpectedEvidenceShape,
    FieldPattern,
    FieldRequirement,
    LineMatcher,
    SectionMatcher,
    StructuralConstraint,
)


@dataclass(frozen=True)
class DivergenceVector:
    missing_required: list[str] = field(default_factory=list)
    pattern_mismatches: dict[str, str] = field(default_factory=dict)
    unexpected_fields: list[str] = field(default_factory=list)
    structural_violations: list[str] = field(default_factory=list)
    line_matcher_failures: list[str] = field(default_factory=list)
    section_matcher_failures: list[str] = field(default_factory=list)
    raw_score: float = 0.0


def compute_divergence(
    evidence: bytes | str | dict,
    shape: ExpectedEvidenceShape,
    *,
    content_type: str | None = None,
    http_status: int | None = None,
    headers: dict[str, str] | None = None,
) -> DivergenceVector:
    json_obj: dict | list | None = None
    text: str = ""
    if isinstance(evidence, dict):
        json_obj = evidence
        text = json.dumps(evidence)
    else:
        if isinstance(evidence, bytes):
            text = evidence.decode("utf-8", errors="replace")
        else:
            text = evidence
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                json_obj = parsed
        except Exception:
            pass

    missing = _missing_required(json_obj, shape.required_fields)
    pattern_mis = _pattern_mismatches(json_obj, shape.field_patterns)
    unexpected = _unexpected_fields(json_obj, shape)
    structural = _structural_violations(
        shape.structural_constraints,
        content_type=content_type, http_status=http_status, headers=headers or {},
    )
    line_fails = _line_matcher_failures(text, shape.line_matchers)
    section_fails = _section_matcher_failures(text, shape.section_matchers)

    score = _aggregate_score(
        shape, missing=missing, pattern_mis=pattern_mis, unexpected=unexpected,
        structural=structural, line_fails=line_fails, section_fails=section_fails,
    )
    return DivergenceVector(
        missing_required=missing,
        pattern_mismatches=pattern_mis,
        unexpected_fields=unexpected,
        structural_violations=structural,
        line_matcher_failures=line_fails,
        section_matcher_failures=section_fails,
        raw_score=score,
    )


def _missing_required(
    obj: dict | list | None, reqs: list[FieldRequirement],
) -> list[str]:
    if obj is None:
        return [r.name for r in reqs]
    out: list[str] = []
    for r in reqs:
        values = _resolve_path(obj, r.name)
        n = len(values)
        ok = True
        if r.cardinality == "one" and n != 1:
            ok = False
        elif r.cardinality == "one_or_more" and n < 1:
            ok = False
        elif r.cardinality == "exact_n" and n != (r.cardinality_n or 0):
            ok = False
        # "any" → always ok
        if not ok:
            out.append(r.name)
    return out


def _resolve_path(obj: Any, dotted: str) -> list:
    """Resolve a simple JSON path like ``$.users[*].id`` or ``a.b``.

    v1 supports root ``$``, dotted attributes, ``[*]`` for all, and
    integer indices ``[N]``. Returns a list of matched values.
    """
    if dotted.startswith("$."):
        dotted = dotted[2:]
    elif dotted.startswith("$"):
        dotted = dotted[1:]
    parts = re.findall(r"[A-Za-z0-9_\-]+|\[\*\]|\[\d+\]", dotted)
    cursors: list[Any] = [obj]
    for p in parts:
        next_cursors: list[Any] = []
        for cur in cursors:
            if p == "[*]":
                if isinstance(cur, list):
                    next_cursors.extend(cur)
            elif p.startswith("[") and p.endswith("]"):
                idx = int(p[1:-1])
                if isinstance(cur, list) and 0 <= idx < len(cur):
                    next_cursors.append(cur[idx])
            else:
                if isinstance(cur, dict) and p in cur:
                    next_cursors.append(cur[p])
        cursors = next_cursors
        if not cursors:
            return []
    return cursors


def _pattern_mismatches(
    obj: dict | list | None, patterns: dict[str, FieldPattern],
) -> dict[str, str]:
    out: dict[str, str] = {}
    if obj is None or not patterns:
        return out
    for path, pat in patterns.items():
        values = _resolve_path(obj, path)
        for v in values:
            if not _value_matches(v, pat):
                out[path] = repr(v)[:100]
                break
    return out


def _value_matches(v: Any, pat: FieldPattern) -> bool:
    if pat.kind == "regex":
        if not isinstance(pat.pattern, str):
            return False
        return bool(re.search(pat.pattern, str(v)))
    if pat.kind == "value_in":
        if not isinstance(pat.pattern, list):
            return False
        return v in pat.pattern
    if pat.kind == "type_only":
        type_map = {
            "str": str, "int": int, "float": float,
            "bool": bool, "list": list, "dict": dict,
        }
        py_type = type_map.get(str(pat.pattern))
        if py_type is None:
            return False
        return isinstance(v, py_type)
    return True


def _unexpected_fields(
    obj: dict | list | None, shape: ExpectedEvidenceShape,
) -> list[str]:
    if not isinstance(obj, dict):
        return []
    expected_top: set[str] = set()
    for r in shape.required_fields:
        first = _first_field_name(r.name)
        if first:
            expected_top.add(first)
    for name in shape.field_patterns:
        first = _first_field_name(name)
        if first:
            expected_top.add(first)
    return sorted(set(obj.keys()) - expected_top)


def _first_field_name(path: str) -> str | None:
    p = path
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("$"):
        p = p[1:]
    parts = re.findall(r"[A-Za-z0-9_\-]+|\[.*?\]", p)
    for tok in parts:
        if not tok.startswith("["):
            return tok
    return None


def _structural_violations(
    constraints: list[StructuralConstraint],
    *,
    content_type: str | None,
    http_status: int | None,
    headers: dict[str, str],
) -> list[str]:
    out: list[str] = []
    norm_headers = {k.lower(): v for k, v in headers.items()}
    for c in constraints:
        body = c.body or {}
        if c.kind == "http_status_in":
            allowed = body.get("allowed") or []
            if http_status is None or http_status not in allowed:
                out.append(f"http_status_in: got {http_status} not in {allowed}")
        elif c.kind == "content_type_in":
            allowed = body.get("allowed") or []
            if content_type is None or all(a not in content_type for a in allowed):
                out.append(f"content_type_in: got {content_type} not in {allowed}")
        elif c.kind == "header_present":
            name = (body.get("name") or "").lower()
            if name and name not in norm_headers:
                out.append(f"header_present: missing {name!r}")
        elif c.kind == "header_value_in":
            name = (body.get("name") or "").lower()
            allowed = body.get("allowed") or []
            if name and norm_headers.get(name) not in allowed:
                out.append(f"header_value_in: {name!r}={norm_headers.get(name)!r}")
        elif c.kind == "size_below":
            limit = int(body.get("bytes") or 0)
            actual = int(body.get("actual_bytes") or 0)
            if limit and actual > limit:
                out.append(f"size_below: {actual} > {limit}")
        elif c.kind == "encoding_in":
            allowed = body.get("allowed") or []
            enc = body.get("actual")
            if allowed and enc not in allowed:
                out.append(f"encoding_in: got {enc!r} not in {allowed}")
    return out


def _line_matcher_failures(text: str, matchers: list[LineMatcher]) -> list[str]:
    if not matchers or not text:
        return [m.label or m.line_regex for m in matchers] if matchers else []
    failures: list[str] = []
    lines = text.splitlines()
    for m in matchers:
        try:
            r = re.compile(m.line_regex)
        except re.error:
            failures.append(f"{m.label or m.line_regex}: bad regex")
            continue
        hits = sum(1 for line in lines if r.search(line))
        if not _check_cardinality(hits, m.cardinality, m.cardinality_n):
            failures.append(f"{m.label or m.line_regex}: hits={hits}")
    return failures


def _section_matcher_failures(text: str, matchers: list[SectionMatcher]) -> list[str]:
    if not matchers:
        return []
    failures: list[str] = []
    lines = text.splitlines()
    for m in matchers:
        try:
            header_re = re.compile(m.header_regex)
        except re.error:
            failures.append(f"{m.label or m.header_regex}: bad header regex")
            continue
        sections = _extract_sections(lines, header_re)
        if not _check_cardinality(len(sections), m.cardinality, m.cardinality_n):
            failures.append(f"{m.label or m.header_regex}: section count={len(sections)}")
            continue
        # body_matchers run against each section's body
        for sec_lines in sections:
            sec_text = "\n".join(sec_lines)
            body_fails = _line_matcher_failures(sec_text, m.body_matchers)
            if body_fails:
                failures.append(
                    f"{m.label or m.header_regex}: body matcher fails: {body_fails}"
                )
                break
    return failures


def _extract_sections(lines: list[str], header_re: "re.Pattern[str]") -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if header_re.search(line):
            if current is not None:
                sections.append(current)
            current = []
        elif current is not None:
            current.append(line)
    if current is not None:
        sections.append(current)
    return sections


def _check_cardinality(n: int, kind: str, exact_n: int | None) -> bool:
    if kind == "one":
        return n == 1
    if kind in ("required", "one_or_more"):
        return n >= 1
    if kind == "zero_or_more" or kind == "any" or kind == "optional":
        return True
    if kind == "exact_n":
        return n == (exact_n or 0)
    return True


_W = {
    "missing": 0.30,
    "pattern": 0.20,
    "unexpected": 0.15,
    "structural": 0.10,
    "lines": 0.15,
    "sections": 0.10,
}


def _aggregate_score(
    shape: ExpectedEvidenceShape, *,
    missing: list, pattern_mis: dict, unexpected: list,
    structural: list, line_fails: list, section_fails: list,
) -> float:
    total_required = max(1, len(shape.required_fields))
    total_patterns = max(1, len(shape.field_patterns))
    total_struct = max(1, len(shape.structural_constraints))
    total_lines = max(1, len(shape.line_matchers))
    total_sections = max(1, len(shape.section_matchers))

    missing_ratio = (
        (len(missing) / total_required) if shape.required_fields else 0.0
    )
    pattern_ratio = (
        (len(pattern_mis) / total_patterns) if shape.field_patterns else 0.0
    )
    unexpected_ratio = min(1.0, len(unexpected) / max(1, shape.unexpected_fields_threshold))
    structural_ratio = (
        (len(structural) / total_struct) if shape.structural_constraints else 0.0
    )
    lines_ratio = (
        (len(line_fails) / total_lines) if shape.line_matchers else 0.0
    )
    sections_ratio = (
        (len(section_fails) / total_sections) if shape.section_matchers else 0.0
    )
    score = (
        _W["missing"] * missing_ratio
        + _W["pattern"] * pattern_ratio
        + _W["unexpected"] * unexpected_ratio
        + _W["structural"] * structural_ratio
        + _W["lines"] * lines_ratio
        + _W["sections"] * sections_ratio
    )
    return min(1.0, max(0.0, score))
