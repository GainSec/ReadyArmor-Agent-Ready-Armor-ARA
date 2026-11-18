"""Kind detectors. Each turns a mat file into a list[ManifestElement].

v1 ships file-extension-based detection — no PDF/image parsing yet.
PDF and image kinds produce a single coarse element each (deferred
parsing per skeleton §5.3).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

from ara.forge.manifest.element import (
    ManifestElement,
    ManifestKind,
    make_element_id,
)


class KindDetector(Protocol):
    priority: int
    kind: ManifestKind

    def detect(self, mat_path: Path) -> bool: ...
    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]: ...


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------- markdown ----------

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
_MD_MAX_SECTION = 5000


class MarkdownDetector:
    priority = 50
    kind = ManifestKind.MD_SECTION

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() in (".md", ".markdown")

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        text = mat_path.read_text(encoding="utf-8", errors="replace")
        heading_offsets: list[tuple[int, str, int]] = []
        for m in _MD_HEADING_RE.finditer(text):
            heading_offsets.append((m.start(), m.group(2).strip(), len(m.group(1))))
        if not heading_offsets:
            data = text.encode("utf-8")
            return [ManifestElement(
                id=make_element_id(data, mat_path.name),
                source_mat_id=mat_id,
                byte_range=(0, len(data)),
                sha256=_sha(data),
                kind=ManifestKind.MD_SECTION,
                extracted_text=text[:1000],
                metadata={"heading": None, "level": 0},
            )]
        bounds = heading_offsets + [(len(text), "", 0)]
        out: list[ManifestElement] = []
        for i in range(len(heading_offsets)):
            start = bounds[i][0]
            end = bounds[i + 1][0]
            heading = bounds[i][1]
            level = bounds[i][2]
            section = text[start:end]
            sub_chunks = _split_long(section, _MD_MAX_SECTION)
            cursor = start
            for j, chunk in enumerate(sub_chunks):
                data = chunk.encode("utf-8")
                rel_path = f"{mat_path.name}:section:{i}:{j}"
                out.append(ManifestElement(
                    id=make_element_id(data, rel_path),
                    source_mat_id=mat_id,
                    byte_range=(cursor, len(data)),
                    sha256=_sha(data),
                    kind=ManifestKind.MD_SECTION,
                    extracted_text=chunk[:1000],
                    metadata={
                        "heading": heading,
                        "level": level,
                        "chunk_index": j,
                        "chunk_total": len(sub_chunks),
                    },
                ))
                cursor += len(data)
        return out


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = 0
    while cur < len(text):
        chunks.append(text[cur:cur + limit])
        cur += limit
    return chunks


# ---------- code ----------

_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".mjs", ".cjs",
}
_CODE_MAX = 10 * 1024


class CodeRepoDetector:
    priority = 60
    kind = ManifestKind.CODE_BLOCK

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() in _CODE_EXTS

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        data = mat_path.read_bytes()
        if len(data) <= _CODE_MAX:
            return [ManifestElement(
                id=make_element_id(data, mat_path.name),
                source_mat_id=mat_id,
                byte_range=(0, len(data)),
                sha256=_sha(data),
                kind=ManifestKind.CODE_BLOCK,
                extracted_text=data[:1000].decode("utf-8", errors="replace"),
                metadata={"filename": mat_path.name, "language": mat_path.suffix.lstrip(".")},
            )]
        out: list[ManifestElement] = []
        cursor = 0
        idx = 0
        while cursor < len(data):
            chunk = data[cursor:cursor + _CODE_MAX]
            rel = f"{mat_path.name}:chunk:{idx}"
            out.append(ManifestElement(
                id=make_element_id(chunk, rel),
                source_mat_id=mat_id,
                byte_range=(cursor, len(chunk)),
                sha256=_sha(chunk),
                kind=ManifestKind.CODE_BLOCK,
                extracted_text=chunk[:1000].decode("utf-8", errors="replace"),
                metadata={
                    "filename": mat_path.name,
                    "language": mat_path.suffix.lstrip("."),
                    "chunk_index": idx,
                },
            ))
            cursor += _CODE_MAX
            idx += 1
        return out


# ---------- text-extracting detectors (pdf / html / csv / docx / xls / xlsx / image / txt) ----------
#
# All of these run :func:`ara.forge.manifest.text_extract.extract` and emit
# one REPORT_SECTION per detected section so the offsec auto-extractor
# (which only fires on REPORT_SECTION) can chew on the contents. When
# the optional dependency is missing, the detector falls back to its
# original opaque element with the install hint in metadata so the
# operator sees what's needed.


def _emit_report_sections(
    mat_path: Path, mat_id: str, *, fallback_kind: ManifestKind,
    fallback_label: str,
) -> list[ManifestElement]:
    """Run text_extract and turn each section into a REPORT_SECTION
    element. Falls back to ``fallback_kind`` with an install/error
    hint when extraction isn't available."""
    from ara.forge.manifest import text_extract as _tx
    res = _tx.extract(mat_path)
    if not res.available:
        data = mat_path.read_bytes()
        meta = {"filename": mat_path.name}
        if res.install_hint:
            meta["install_hint"] = res.install_hint
        if res.error:
            meta["extract_error"] = res.error
        hint = res.install_hint or res.error or "no extractor"
        return [ManifestElement(
            id=make_element_id(data, mat_path.name),
            source_mat_id=mat_id,
            byte_range=(0, len(data)),
            sha256=_sha(data),
            kind=fallback_kind,
            extracted_text=f"[{fallback_label} {mat_path.name} — {hint}]",
            metadata=meta,
        )]
    if not res.sections:
        # Extraction ran but produced nothing — opaque fallback with
        # the backend recorded so operators can debug.
        data = mat_path.read_bytes()
        return [ManifestElement(
            id=make_element_id(data, mat_path.name),
            source_mat_id=mat_id,
            byte_range=(0, len(data)),
            sha256=_sha(data),
            kind=fallback_kind,
            extracted_text=(
                f"[{fallback_label} {mat_path.name} — no readable text "
                f"(backend={res.backend or '?'})]"
            ),
            metadata={"filename": mat_path.name,
                       "backend": res.backend or "",
                       "section_count": 0},
        )]
    out: list[ManifestElement] = []
    for i, sec in enumerate(res.sections):
        body = (sec.heading + "\n" + sec.text) if sec.heading else sec.text
        data = body.encode("utf-8")
        rel = f"{mat_path.name}:section:{i}"
        out.append(ManifestElement(
            id=make_element_id(data, rel),
            source_mat_id=mat_id,
            byte_range=(0, len(data)),
            sha256=_sha(data),
            kind=ManifestKind.REPORT_SECTION,
            extracted_text=body[:1000],
            metadata={
                "filename": mat_path.name,
                "heading": sec.heading,
                "section_index": i,
                "section_total": len(res.sections),
                "backend": res.backend or "",
                "source_format": fallback_label,
                **sec.metadata,
            },
        ))
    return out


class PDFDetector:
    priority = 40
    kind = ManifestKind.PDF_PAGE

    def detect(self, mat_path: Path) -> bool:
        if mat_path.suffix.lower() != ".pdf":
            return False
        try:
            with mat_path.open("rb") as f:
                head = f.read(4)
            return head == b"%PDF"
        except OSError:
            return False

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.PDF_PAGE, fallback_label="PDF",
        )


class HTMLDetector:
    priority = 41
    kind = ManifestKind.REPORT_SECTION

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() in (".html", ".htm")

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.GENERIC_TEXT, fallback_label="HTML",
        )


class CSVDetector:
    priority = 42
    kind = ManifestKind.REPORT_SECTION

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() in (".csv", ".tsv")

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.GENERIC_TEXT, fallback_label="CSV",
        )


class DOCXDetector:
    priority = 43
    kind = ManifestKind.REPORT_SECTION

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() == ".docx"

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.UNKNOWN, fallback_label="DOCX",
        )


class XLSXDetector:
    priority = 44
    kind = ManifestKind.REPORT_SECTION

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() in (".xlsx", ".xlsm")

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.UNKNOWN, fallback_label="XLSX",
        )


class XLSDetector:
    priority = 45
    kind = ManifestKind.REPORT_SECTION

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() == ".xls"

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.UNKNOWN, fallback_label="XLS",
        )


class TextFileDetector:
    """Plain ``.txt`` / ``.log`` files run through the heading splitter
    and emit REPORT_SECTION elements (so the auto-extractor can scan
    them) — distinct from the broader GenericTextDetector fallback."""
    priority = 46
    kind = ManifestKind.REPORT_SECTION

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() in (".txt", ".log")

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.GENERIC_TEXT, fallback_label="TXT",
        )


# ---------- url snapshot ----------

class URLSnapshotDetector:
    priority = 30
    kind = ManifestKind.URL_SNAPSHOT

    def detect(self, mat_path: Path) -> bool:
        if mat_path.suffix.lower() not in (".json", ".har"):
            return False
        try:
            data = json.loads(mat_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return False
        if isinstance(data, dict):
            if "log" in data and isinstance(data["log"], dict):  # HAR shape
                return True
            if "url" in data and "body" in data:  # url-snapshot shape
                return True
        return False

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        raw = mat_path.read_bytes()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        url = ""
        if isinstance(data, dict):
            if "url" in data:
                url = str(data.get("url", ""))
            elif "log" in data:
                entries = data["log"].get("entries") or []
                if entries:
                    url = entries[0].get("request", {}).get("url", "")
        return [ManifestElement(
            id=make_element_id(raw, mat_path.name),
            source_mat_id=mat_id,
            byte_range=(0, len(raw)),
            sha256=_sha(raw),
            kind=ManifestKind.URL_SNAPSHOT,
            extracted_text=f"URL snapshot: {url}",
            metadata={"url": url, "filename": mat_path.name},
        )]


# ---------- image ----------

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


class ImageDetector:
    priority = 70
    kind = ManifestKind.IMAGE

    def detect(self, mat_path: Path) -> bool:
        return mat_path.suffix.lower() in _IMAGE_EXTS

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        # Try OCR first; if pytesseract isn't available, fall back to
        # the original opaque-image element with the install hint.
        return _emit_report_sections(
            mat_path, mat_id,
            fallback_kind=ManifestKind.IMAGE, fallback_label="image",
        )


# ---------- example report ----------

_DEFAULT_REPORT_SECTION_KEYWORDS = ("finding", "vulnerability", "vulnerab", "issue")


def _build_keyword_re(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a keyword list into a heading-prefix regex matching any
    of the keywords as whole-word-or-prefix tokens."""
    safe = [re.escape(k) for k in keywords if k]
    if not safe:
        safe = [re.escape(k) for k in _DEFAULT_REPORT_SECTION_KEYWORDS]
    pattern = "|".join(safe)
    return re.compile(rf"^\s*##?\s*(?:{pattern})\b", re.I | re.M)


def _build_inline_keyword_re(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Looser regex that matches the keyword anywhere in a heading
    (not just at the start) — used when slicing a report into sections.
    """
    safe = [re.escape(k) for k in keywords if k]
    if not safe:
        safe = [re.escape(k) for k in _DEFAULT_REPORT_SECTION_KEYWORDS]
    return re.compile("|".join(safe), re.I)


_FINDING_SECTION_RE = _build_keyword_re(_DEFAULT_REPORT_SECTION_KEYWORDS)


class ExampleReportDetector:
    priority = 10  # highest priority — runs first
    kind = ManifestKind.REPORT_SECTION

    def __init__(
        self,
        keywords: tuple[str, ...] | None = None,
    ) -> None:
        """Pack-supplied keywords override the hard-coded default
        (``finding|vulnerability|issue``). Empty / None → defaults.
        """
        self._keywords = tuple(keywords) if keywords else _DEFAULT_REPORT_SECTION_KEYWORDS
        self._heading_re = _build_keyword_re(self._keywords)
        self._inline_re = _build_inline_keyword_re(self._keywords)

    def detect(self, mat_path: Path) -> bool:
        if mat_path.suffix.lower() not in (".md", ".markdown"):
            return False
        try:
            text = mat_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return bool(self._heading_re.search(text))

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        text = mat_path.read_text(encoding="utf-8", errors="replace")
        # Split on finding-section headings (look for any heading containing the word).
        anchors: list[tuple[int, str]] = []
        for m in _MD_HEADING_RE.finditer(text):
            heading = m.group(2).strip()
            if self._inline_re.search(heading):
                anchors.append((m.start(), heading))
        if not anchors:
            data = text.encode("utf-8")
            return [ManifestElement(
                id=make_element_id(data, mat_path.name),
                source_mat_id=mat_id,
                byte_range=(0, len(data)),
                sha256=_sha(data),
                kind=ManifestKind.REPORT_SECTION,
                extracted_text=text[:1000],
                metadata={"filename": mat_path.name, "heading": None},
            )]
        bounds = anchors + [(len(text), "")]
        out: list[ManifestElement] = []
        for i in range(len(anchors)):
            start = bounds[i][0]
            end = bounds[i + 1][0]
            section = text[start:end]
            data = section.encode("utf-8")
            rel = f"{mat_path.name}:finding:{i}"
            out.append(ManifestElement(
                id=make_element_id(data, rel),
                source_mat_id=mat_id,
                byte_range=(start, len(data)),
                sha256=_sha(data),
                kind=ManifestKind.REPORT_SECTION,
                extracted_text=section[:1000],
                metadata={
                    "filename": mat_path.name,
                    "heading": bounds[i][1],
                    "finding_index": i,
                },
            ))
        return out


# ---------- generic-text fallback ----------

class GenericTextDetector:
    priority = 100  # lowest priority
    kind = ManifestKind.GENERIC_TEXT

    def detect(self, mat_path: Path) -> bool:
        try:
            data = mat_path.read_bytes()[:4096]
        except OSError:
            return False
        # Heuristic: ≤2% non-printable bytes excluding common whitespace
        if not data:
            return False
        printable = sum(
            1 for b in data
            if 32 <= b < 127 or b in (9, 10, 13)
        )
        return (printable / len(data)) >= 0.95

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        data = mat_path.read_bytes()
        return [ManifestElement(
            id=make_element_id(data, mat_path.name),
            source_mat_id=mat_id,
            byte_range=(0, len(data)),
            sha256=_sha(data),
            kind=ManifestKind.GENERIC_TEXT,
            extracted_text=data[:1000].decode("utf-8", errors="replace"),
            metadata={"filename": mat_path.name},
        )]


# ---------- unknown fallback ----------

class UnknownDetector:
    priority = 999
    kind = ManifestKind.UNKNOWN

    def detect(self, mat_path: Path) -> bool:
        return True  # always claims if nothing else does

    def enumerate(self, mat_path: Path, *, mat_id: str) -> list[ManifestElement]:
        data = mat_path.read_bytes() if mat_path.exists() else b""
        return [ManifestElement(
            id=make_element_id(data, mat_path.name),
            source_mat_id=mat_id,
            byte_range=(0, len(data)),
            sha256=_sha(data),
            kind=ManifestKind.UNKNOWN,
            extracted_text=f"[unknown kind {mat_path.name}]",
            metadata={"filename": mat_path.name},
        )]


def default_detectors(
    *,
    report_section_keywords: tuple[str, ...] | None = None,
) -> list[KindDetector]:
    """Detectors ordered by priority (lower runs first).

    ``report_section_keywords`` (typically pack-supplied via
    :class:`ManifestDetectionConfig`) tunes the ExampleReportDetector;
    None → hard-coded offsec defaults.
    """
    detectors: list[KindDetector] = [
        ExampleReportDetector(keywords=report_section_keywords),
        URLSnapshotDetector(),
        PDFDetector(),
        HTMLDetector(),
        CSVDetector(),
        DOCXDetector(),
        XLSXDetector(),
        XLSDetector(),
        TextFileDetector(),
        MarkdownDetector(),
        CodeRepoDetector(),
        ImageDetector(),
        GenericTextDetector(),
        UnknownDetector(),
    ]
    return sorted(detectors, key=lambda d: d.priority)
