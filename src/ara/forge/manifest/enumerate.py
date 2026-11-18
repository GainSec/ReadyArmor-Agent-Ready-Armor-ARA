"""Walk a mats directory + dispatch to detectors → ManifestElement[]."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ara.forge.manifest.element import ManifestElement, ManifestKind
from ara.forge.manifest.kinds import KindDetector, default_detectors


@dataclass(frozen=True)
class ManifestCoverage:
    by_kind: dict[str, int]
    by_mat: dict[str, int]
    total_elements: int
    total_mats: int


def mat_id_for(mat_path: Path) -> str:
    """Stable per-mat id derived from filename + content hash."""
    h = hashlib.sha256()
    h.update(mat_path.name.encode("utf-8"))
    try:
        h.update(mat_path.read_bytes())
    except OSError:
        pass
    return "mat_" + h.hexdigest()[:10]


def enumerate_mats(
    mats_dir: Path,
    *,
    detectors: list[KindDetector] | None = None,
    report_section_keywords: tuple[str, ...] | None = None,
) -> list[ManifestElement]:
    """Walk ``mats_dir`` and produce ManifestElements via priority-ordered
    detectors. ``report_section_keywords`` (pack-supplied) tunes the
    ExampleReportDetector when no explicit detector list is given.
    """
    if detectors is None:
        detectors = default_detectors(
            report_section_keywords=report_section_keywords,
        )
    elements: list[ManifestElement] = []
    if not mats_dir.exists():
        return elements
    for mat_path in sorted(mats_dir.rglob("*")):
        if not mat_path.is_file():
            continue
        if mat_path.name.startswith("."):
            continue
        # Skip deposit-phase metadata sidecars (<filename>.meta.json)
        if mat_path.name.endswith(".meta.json"):
            continue
        mid = mat_id_for(mat_path)
        chosen: KindDetector | None = None
        for det in detectors:
            try:
                if det.detect(mat_path):
                    chosen = det
                    break
            except Exception:
                continue
        if chosen is None:
            continue
        elements.extend(chosen.enumerate(mat_path, mat_id=mid))
    return elements


def coverage(elements: list[ManifestElement]) -> ManifestCoverage:
    by_kind: dict[str, int] = {}
    by_mat: dict[str, int] = {}
    for el in elements:
        if el.dropped_by_operator:
            continue
        kind_v = el.kind.value if isinstance(el.kind, ManifestKind) else str(el.kind)
        by_kind[kind_v] = by_kind.get(kind_v, 0) + 1
        by_mat[el.source_mat_id] = by_mat.get(el.source_mat_id, 0) + 1
    return ManifestCoverage(
        by_kind=by_kind,
        by_mat=by_mat,
        total_elements=sum(by_kind.values()),
        total_mats=len(by_mat),
    )


def write_coverage(path: Path, cov: ManifestCoverage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "by_kind": cov.by_kind,
        "by_mat": cov.by_mat,
        "total_elements": cov.total_elements,
        "total_mats": cov.total_mats,
    }, sort_keys=True, indent=2))
