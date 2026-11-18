"""Multi-format text extraction for forge manifest enumeration.

Each format extractor attempts to import its backing library; if the
import fails, the extractor returns ``available=False`` with an
``install_hint`` so the caller can either skip enumeration of that
mat or fall back to an opaque element. Stdlib-only formats (txt,
html, csv) always succeed.

Optional pip dependencies (install with ``pip install ara[extract]``
once the extra is wired into pyproject.toml — for now install
manually):

    pypdf            — PDF text
    python-docx      — DOCX
    openpyxl         — XLSX
    xlrd<2           — XLS (legacy Excel)
    pytesseract+pillow — image OCR (also needs `tesseract` system binary)

Section detection is heuristic: in text the extractor splits on
``# Heading``, ``## Heading``, ``Finding N:`` / ``Vulnerability N:``
patterns, ALL-CAPS short lines, and numbered-list-like headings.
Each detected section becomes one ``ExtractedSection``; extractors
return ``[ExtractedSection(heading=None, text=full_text)]`` when no
structure is found, and the caller emits a single REPORT_SECTION.
"""
from __future__ import annotations

import csv as _csv
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional


# ---------- result types ----------

@dataclass
class ExtractedSection:
    heading: Optional[str]
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """``available=True`` means extraction ran; sections may still be
    empty if the file had no readable text. ``available=False`` →
    ``install_hint`` tells the operator what to install."""
    available: bool
    sections: list[ExtractedSection] = field(default_factory=list)
    install_hint: Optional[str] = None
    error: Optional[str] = None
    backend: Optional[str] = None


# ---------- heading detection ----------

_HEADING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Markdown-style # / ## / ### headings (also work in plain text)
    re.compile(r"^(#{1,6})\s+(.+?)\s*$"),
    # "Finding 12: Reflected XSS in /search"
    re.compile(r"^\s*(Finding|Vulnerability|Issue|Risk|Bug)\s+"
                r"(?:[A-Z]?[\d\.]+)?[:.\s-]+(.+?)\s*$",
                re.IGNORECASE),
    # "## Section 4.1 Authentication" — section/chapter prefix
    re.compile(r"^\s*(?:Section|Chapter|Part)\s+[\w\.]+[:\s-]+(.+?)\s*$",
                re.IGNORECASE),
    # Numbered top-level: "1. Introduction"  /  "4.2 Authentication"
    re.compile(r"^\s*\d+(?:\.\d+){0,3}\.?\s+([A-Z][^.\n]{3,80})\s*$"),
    # ALL CAPS line (3-80 chars, mostly letters) — typical PDF heading
    re.compile(r"^\s*([A-Z][A-Z0-9 \-/&]{2,79})\s*$"),
)


def _looks_like_heading(line: str) -> str | None:
    """Return the cleaned heading text if ``line`` matches any heading
    pattern, else None."""
    s = line.rstrip()
    if not s or len(s) > 200:
        return None
    for pat in _HEADING_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        if not groups:
            continue
        candidate = groups[-1].strip()
        # Reject ALL-CAPS lines that are basically all caps acronyms /
        # boilerplate (e.g. "TLS" alone) — require at least 2 words OR
        # a digit OR a dash.
        if pat.pattern.startswith(r"^\s*([A-Z][A-Z0-9"):
            words = candidate.split()
            if len(words) < 2 and not any(
                ch.isdigit() or ch == "-" for ch in candidate
            ):
                return None
        return candidate
    return None


def split_into_sections(
    text: str, *, min_section_chars: int = 60,
) -> list[ExtractedSection]:
    """Slice ``text`` on heading-looking lines into ExtractedSections.

    A "section" runs from one heading to the next. If no headings
    match, returns a single section with heading=None and the full
    text. Sections shorter than ``min_section_chars`` get merged into
    the next one so we don't fragment on trivial sub-headings.
    """
    if not text.strip():
        return []
    lines = text.splitlines()
    anchors: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        h = _looks_like_heading(line)
        if h is not None:
            anchors.append((i, h))
    if not anchors:
        return [ExtractedSection(heading=None, text=text)]
    sections: list[ExtractedSection] = []
    if anchors[0][0] > 0:
        prelude = "\n".join(lines[: anchors[0][0]]).strip()
        if prelude:
            sections.append(ExtractedSection(heading=None, text=prelude))
    bounds = [a[0] for a in anchors] + [len(lines)]
    for idx, (start, heading) in enumerate(anchors):
        end = bounds[idx + 1]
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue
        if (sections and len(body) < min_section_chars
                and sections[-1].heading is not None):
            # Tiny section — merge into previous
            prev = sections[-1]
            sections[-1] = ExtractedSection(
                heading=prev.heading,
                text=prev.text + "\n\n" + body,
                metadata=prev.metadata,
            )
        else:
            sections.append(ExtractedSection(heading=heading, text=body))
    return sections


# ---------- format extractors ----------

def extract_txt(path: Path) -> ExtractionResult:
    text = path.read_text(encoding="utf-8", errors="replace")
    return ExtractionResult(
        available=True, backend="stdlib",
        sections=split_into_sections(text),
    )


def extract_md(path: Path) -> ExtractionResult:
    # Same path as txt — the heading splitter already handles markdown.
    return extract_txt(path)


class _HTMLTextExtractor(HTMLParser):
    """Streaming HTML→text + heading capture using stdlib only."""

    _SKIP_TAGS = {"script", "style", "noscript", "head", "meta"}
    _BLOCK_TAGS = {"p", "br", "div", "li", "tr", "section", "article"}
    _HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_heading: str | None = None
        self._buf: list[str] = []
        self.sections: list[ExtractedSection] = []
        self._cur_heading: str | None = None
        self._cur_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._HEADING_TAGS:
            # Flush current section
            self._flush_section()
            self._in_heading = tag
            self._buf = []
        elif tag in self._BLOCK_TAGS and self._cur_text:
            self._cur_text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._HEADING_TAGS and self._in_heading == tag:
            self._cur_heading = "".join(self._buf).strip()
            self._in_heading = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_heading:
            self._buf.append(data)
        else:
            self._cur_text.append(data)

    def _flush_section(self) -> None:
        body = "".join(self._cur_text).strip()
        if not body and not self._cur_heading:
            return
        if body or self._cur_heading:
            self.sections.append(ExtractedSection(
                heading=self._cur_heading, text=body,
            ))
        self._cur_heading = None
        self._cur_text = []

    def finish(self) -> list[ExtractedSection]:
        self._flush_section()
        return [s for s in self.sections if s.text or s.heading]


def extract_html(path: Path) -> ExtractionResult:
    raw = path.read_text(encoding="utf-8", errors="replace")
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(available=False, error=f"html parse: {exc}")
    sections = parser.finish()
    if not sections:
        # No headings — fall back to plain text body
        text = re.sub(r"<[^>]+>", "", raw).strip()
        sections = [ExtractedSection(heading=None, text=text)] if text else []
    return ExtractionResult(
        available=True, backend="stdlib", sections=sections,
    )


def extract_csv(path: Path) -> ExtractionResult:
    """Render CSV rows as a single text section. Each row becomes a
    pipe-delimited line so downstream regex/text matchers can scan it.
    """
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = _csv.reader(f)
            rows = list(reader)
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(available=False, error=f"csv: {exc}")
    if not rows:
        return ExtractionResult(available=True, backend="stdlib", sections=[])
    header = " | ".join(rows[0]) if rows else ""
    body_lines = [" | ".join(r) for r in rows[1:]]
    text = (header + "\n" + "-" * len(header) + "\n"
             + "\n".join(body_lines)) if header else "\n".join(body_lines)
    sections = [ExtractedSection(
        heading=path.stem,
        text=text,
        metadata={"row_count": len(rows), "col_count": len(rows[0]) if rows else 0},
    )]
    return ExtractionResult(
        available=True, backend="stdlib", sections=sections,
    )


def _reconstruct_tables_from_words(words: list, x_tol: float = 6,
                                   y_tol: float = 10, min_valcols: int = 2,
                                   min_rows: int = 3) -> list:
    """Rebuild column-aligned tables from pdfplumber word geometry, tolerant of
    wrapped description text. Borderless tables (assurance matrices, coverage
    grids) whose 2D structure the linear text extractor flattens away are
    recovered here. Returns a list of grids (each a list of rows)."""
    import collections
    rows: list = []
    for w in sorted(words, key=lambda w: (round(w["top"] / y_tol), w["x0"])):
        r = round(w["top"] / y_tol)
        if rows and rows[-1][0] == r:
            rows[-1][1].append(w)
        else:
            rows.append((r, [w]))
    xhist = collections.Counter(
        round(w["x0"] / x_tol) * x_tol for _, ws in rows for w in ws)
    anchors = sorted(x for x, n in xhist.items() if n >= min_rows)
    merged: list = []
    for a in anchors:
        if merged and a - merged[-1] <= x_tol:
            continue
        merged.append(a)
    anchors = merged
    if len(anchors) < min_valcols + 1:
        return []

    def colof(x: float) -> int:
        return min(range(len(anchors)), key=lambda i: abs(anchors[i] - x))

    grid_rows: list = []
    desc = ""
    val_row_count = 0
    for _, ws in rows:
        cells = [""] * len(anchors)
        for w in ws:
            ci = colof(w["x0"])
            cells[ci] = (cells[ci] + " " + w["text"]).strip()
        # a value cell is a SHORT token (mark / level / small number) — this
        # keeps real matrix rows and rejects prose that merely x-aligned.
        val_pop = sum(1 for i, c in enumerate(cells)
                      if c and i > 0 and len(c) <= 12)
        if val_pop >= min_valcols:
            grid_rows.append([(desc + " " + cells[0]).strip()] + cells[1:])
            desc = ""
            val_row_count += 1
        elif cells[0]:
            desc = (desc + " " + cells[0]).strip()
    if val_row_count < min_rows:
        return []
    return [grid_rows]


def _grid_to_markdown(grid: list) -> str:
    width = max(len(r) for r in grid)
    lines = ["| " + " | ".join(["col%d" % i for i in range(width)]) + " |",
             "|" + "|".join("---" for _ in range(width)) + "|"]
    for r in grid:
        r = (r + [""] * (width - len(r)))[:width]
        lines.append("| " + " | ".join(c.replace("\n", " ")[:80] for c in r) + " |")
    return "\n".join(lines)


def _pdfplumber_page_text(page) -> str:
    """A page's linear text, plus any reconstructed borderless tables appended
    as markdown grids so the 2D structure survives for downstream consumers."""
    text = page.extract_text() or ""
    try:
        grids = _reconstruct_tables_from_words(page.extract_words())
    except Exception:  # noqa: BLE001
        grids = []
    for g in grids:
        text += "\n\n[reconstructed table]\n" + _grid_to_markdown(g)
    return text


def extract_pdf(path: Path) -> ExtractionResult:
    """PDF text with table-aware reconstruction via pdfplumber (preferred) —
    borderless tables are rebuilt as markdown grids so matrix/scored structure
    survives. Falls back to pypdf, then pdfminer.six. Each page that contains a
    heading-looking line starts its own section.
    """
    try:
        import pdfplumber  # type: ignore[import-not-found]
    except ImportError:
        pdfplumber = None  # type: ignore[assignment]
    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(path)) as pdf:
                page_texts = [_pdfplumber_page_text(pg) for pg in pdf.pages]
            full = "\n\n".join(t for t in page_texts if t.strip())
            return ExtractionResult(
                available=True, backend="pdfplumber (table-aware)",
                sections=split_into_sections(full),
            )
        except Exception:  # noqa: BLE001 — fall through to pypdf
            pass
    try:
        import pypdf  # type: ignore[import-not-found]
    except ImportError:
        try:
            from pdfminer.high_level import extract_text as _pm_extract  # type: ignore[import-not-found]
        except ImportError:
            return ExtractionResult(
                available=False,
                install_hint="pip install pypdf  # or pdfminer.six",
            )
        try:
            text = _pm_extract(str(path))
        except Exception as exc:  # noqa: BLE001
            return ExtractionResult(
                available=False, error=f"pdfminer: {exc}",
            )
        return ExtractionResult(
            available=True, backend="pdfminer.six",
            sections=split_into_sections(text),
        )
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(available=False, error=f"pypdf: {exc}")
    page_texts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            page_texts.append(t)
    full = "\n\n".join(page_texts)
    return ExtractionResult(
        available=True, backend=f"pypdf ({len(reader.pages)}p)",
        sections=split_into_sections(full),
    )


def extract_docx(path: Path) -> ExtractionResult:
    try:
        import docx  # python-docx  # type: ignore[import-not-found]
    except ImportError:
        return ExtractionResult(
            available=False, install_hint="pip install python-docx",
        )
    try:
        d = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(available=False, error=f"docx: {exc}")
    sections: list[ExtractedSection] = []
    cur_heading: str | None = None
    cur_text: list[str] = []
    for para in d.paragraphs:
        style = (para.style.name or "").lower() if para.style else ""
        text = para.text or ""
        if style.startswith("heading"):
            if cur_text or cur_heading:
                body = "\n".join(cur_text).strip()
                if body or cur_heading:
                    sections.append(ExtractedSection(
                        heading=cur_heading, text=body,
                    ))
            cur_heading = text.strip()
            cur_text = []
        else:
            if text:
                cur_text.append(text)
    if cur_text or cur_heading:
        body = "\n".join(cur_text).strip()
        if body or cur_heading:
            sections.append(ExtractedSection(heading=cur_heading, text=body))
    if not sections:
        full = "\n".join(p.text for p in d.paragraphs if p.text).strip()
        if full:
            sections = split_into_sections(full)
    return ExtractionResult(
        available=True, backend="python-docx", sections=sections,
    )


def extract_xlsx(path: Path) -> ExtractionResult:
    try:
        import openpyxl  # type: ignore[import-not-found]
    except ImportError:
        return ExtractionResult(
            available=False, install_hint="pip install openpyxl",
        )
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(available=False, error=f"openpyxl: {exc}")
    sections: list[ExtractedSection] = []
    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]
        rows: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            if not row:
                continue
            cells = [str(c) if c is not None else "" for c in row]
            if any(c.strip() for c in cells):
                rows.append(" | ".join(cells))
        if rows:
            sections.append(ExtractedSection(
                heading=sheet_name,
                text="\n".join(rows),
                metadata={"sheet": sheet_name, "row_count": len(rows)},
            ))
    wb.close()
    return ExtractionResult(
        available=True, backend="openpyxl", sections=sections,
    )


def extract_xls(path: Path) -> ExtractionResult:
    try:
        import xlrd  # type: ignore[import-not-found]
    except ImportError:
        return ExtractionResult(
            available=False, install_hint="pip install 'xlrd<2'",
        )
    try:
        wb = xlrd.open_workbook(str(path))
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(available=False, error=f"xlrd: {exc}")
    sections: list[ExtractedSection] = []
    for sheet in wb.sheets():
        rows: list[str] = []
        for r in range(sheet.nrows):
            row = [str(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
            if any(v.strip() for v in row):
                rows.append(" | ".join(row))
        if rows:
            sections.append(ExtractedSection(
                heading=sheet.name,
                text="\n".join(rows),
                metadata={"sheet": sheet.name, "row_count": len(rows)},
            ))
    return ExtractionResult(
        available=True, backend="xlrd", sections=sections,
    )


def extract_image(path: Path) -> ExtractionResult:
    """Image OCR via pytesseract. Requires the ``tesseract`` system
    binary in addition to the Python package."""
    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return ExtractionResult(
            available=False,
            install_hint="pip install pytesseract pillow  # also needs tesseract binary",
        )
    try:
        img = Image.open(str(path))
        text = pytesseract.image_to_string(img)
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(available=False, error=f"OCR: {exc}")
    if not text.strip():
        return ExtractionResult(
            available=True, backend="tesseract",
            sections=[],
        )
    return ExtractionResult(
        available=True, backend="tesseract",
        sections=split_into_sections(text),
    )


# ---------- public dispatcher ----------

_EXT_DISPATCH: dict[str, Callable[[Path], ExtractionResult]] = {
    ".txt": extract_txt,
    ".log": extract_txt,
    ".md": extract_md,
    ".markdown": extract_md,
    ".html": extract_html,
    ".htm": extract_html,
    ".csv": extract_csv,
    ".tsv": extract_csv,
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".xlsx": extract_xlsx,
    ".xlsm": extract_xlsx,
    ".xls": extract_xls,
    ".png": extract_image,
    ".jpg": extract_image,
    ".jpeg": extract_image,
    ".gif": extract_image,
    ".webp": extract_image,
    ".bmp": extract_image,
    ".tif": extract_image,
    ".tiff": extract_image,
}


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted(_EXT_DISPATCH))


def extract(path: Path) -> ExtractionResult:
    """Top-level dispatch by extension. Returns ``available=False``
    with ``error="unsupported extension"`` for unknown types."""
    ext = path.suffix.lower()
    fn = _EXT_DISPATCH.get(ext)
    if fn is None:
        return ExtractionResult(
            available=False, error=f"unsupported extension: {ext!r}",
        )
    return fn(path)
