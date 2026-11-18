"""ManifestElement schema + JSONL IO.

Element id stability (orchestrator §5.1):
  elem_id = "elem_" + sha256(content_hash || mat_relative_path)[:10]
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class ManifestKind(str, Enum):
    PDF_PAGE = "pdf_page"
    MD_SECTION = "md_section"
    CODE_BLOCK = "code_block"
    URL_SNAPSHOT = "url_snapshot"
    IMAGE = "image"
    REPORT_SECTION = "report_section"
    GENERIC_TEXT = "generic_text"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ManifestElement:
    id: str
    source_mat_id: str
    byte_range: tuple[int, int]
    sha256: str
    kind: ManifestKind
    extracted_text: str
    metadata: dict = field(default_factory=dict)
    complexity: float | None = None
    complexity_inputs: dict | None = None
    dropped_by_operator: bool = False

    def to_jsonable(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["byte_range"] = list(self.byte_range)
        return d

    @classmethod
    def from_jsonable(cls, d: dict) -> "ManifestElement":
        return cls(
            id=d["id"],
            source_mat_id=d["source_mat_id"],
            byte_range=tuple(d["byte_range"]),  # type: ignore[arg-type]
            sha256=d["sha256"],
            kind=ManifestKind(d["kind"]),
            extracted_text=d["extracted_text"],
            metadata=d.get("metadata") or {},
            complexity=d.get("complexity"),
            complexity_inputs=d.get("complexity_inputs"),
            dropped_by_operator=d.get("dropped_by_operator", False),
        )


def make_element_id(content: bytes, mat_relative_path: str) -> str:
    content_hash = hashlib.sha256(content).hexdigest()
    h = hashlib.sha256()
    h.update(content_hash.encode("ascii"))
    h.update(mat_relative_path.encode("utf-8"))
    return "elem_" + h.hexdigest()[:10]


def write_manifest(path: Path, elements: list[ManifestElement]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for el in elements:
            f.write(json.dumps(el.to_jsonable(), sort_keys=True) + "\n")


def append_element(path: Path, el: ManifestElement) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(el.to_jsonable(), sort_keys=True) + "\n")


def read_manifest(path: Path) -> list[ManifestElement]:
    if not path.exists():
        return []
    out: list[ManifestElement] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(ManifestElement.from_jsonable(json.loads(line)))
    return out
