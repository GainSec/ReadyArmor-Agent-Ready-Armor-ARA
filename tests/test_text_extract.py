"""Tests for forge.manifest.text_extract — multi-format mat extraction."""
from __future__ import annotations

from pathlib import Path

import pytest

from ara.forge.manifest import text_extract as tx


# ---------- heading splitter ----------

def test_split_no_headings_returns_single_section() -> None:
    out = tx.split_into_sections("plain unstructured body of text")
    assert len(out) == 1
    assert out[0].heading is None
    assert "plain" in out[0].text


def test_split_md_headings() -> None:
    text = (
        "intro line\n"
        "# Top\n"
        "first body content here long enough to count as a real section.\n"
        "## Sub\n"
        "second body content here long enough to count as a real section.\n"
    )
    out = tx.split_into_sections(text)
    headings = [s.heading for s in out]
    assert headings == [None, "Top", "Sub"]


def test_split_finding_pattern() -> None:
    text = (
        "Finding 1: Reflected XSS in /search\n"
        "details details details details details details details\n"
        "Finding 2: SQLi in /login\n"
        "details details details details details details details\n"
    )
    out = tx.split_into_sections(text)
    assert any("Reflected XSS" in (s.heading or "") for s in out)
    assert any("SQLi" in (s.heading or "") for s in out)


def test_split_empty_text() -> None:
    assert tx.split_into_sections("   \n\n  ") == []


def test_split_merges_short_subsections() -> None:
    text = (
        "# Big\n"
        "long body content for the big section here, definitely above sixty.\n"
        "## Tiny\n"
        "x\n"
        "## Tiny2\n"
        "y\n"
        "## Real\n"
        "long body content for the real section here, definitely above sixty.\n"
    )
    out = tx.split_into_sections(text, min_section_chars=60)
    headings = [s.heading for s in out]
    # Tiny / Tiny2 sections merge into Big rather than fragmenting
    assert "Big" in headings
    assert "Real" in headings


# ---------- txt / md ----------

def test_extract_txt(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("# Section\nsome body content here that is long enough.")
    res = tx.extract(p)
    assert res.available
    assert res.backend == "stdlib"
    assert any(s.heading == "Section" for s in res.sections)


def test_extract_md(tmp_path: Path) -> None:
    p = tmp_path / "report.md"
    p.write_text("# Finding 1: XSS\n\nbody content here that is long enough.")
    res = tx.extract(p)
    assert res.available
    assert res.sections[0].heading is not None


def test_extract_unsupported(tmp_path: Path) -> None:
    p = tmp_path / "weird.zzz"
    p.write_text("hello")
    res = tx.extract(p)
    assert not res.available
    assert "unsupported" in (res.error or "")


# ---------- html ----------

def test_extract_html_with_headings(tmp_path: Path) -> None:
    p = tmp_path / "report.html"
    p.write_text(
        "<html><body>"
        "<h1>Main</h1><p>main body content</p>"
        "<h2>Sub</h2><p>sub body content</p>"
        "<script>alert(1)</script>"
        "</body></html>"
    )
    res = tx.extract(p)
    assert res.available
    headings = [s.heading for s in res.sections]
    assert "Main" in headings
    assert "Sub" in headings
    # Script body should not leak
    assert all("alert(1)" not in s.text for s in res.sections)


def test_extract_html_no_headings(tmp_path: Path) -> None:
    p = tmp_path / "plain.html"
    p.write_text("<html><body>Just some plain text body.</body></html>")
    res = tx.extract(p)
    assert res.available
    assert len(res.sections) == 1
    assert "plain text body" in res.sections[0].text


# ---------- csv ----------

def test_extract_csv(tmp_path: Path) -> None:
    p = tmp_path / "data.csv"
    p.write_text("name,severity\nXSS,high\nSQLi,critical\n")
    res = tx.extract(p)
    assert res.available
    assert res.sections
    text = res.sections[0].text
    assert "name | severity" in text
    assert "XSS | high" in text


def test_extract_csv_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("")
    res = tx.extract(p)
    assert res.available
    assert res.sections == []


# ---------- pdf (optional) ----------

def test_extract_pdf_dep_missing(tmp_path: Path, monkeypatch) -> None:
    """When neither pypdf nor pdfminer.six is importable, return an
    install hint rather than crashing."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name: str, *a, **kw):
        if name in ("pypdf", "pdfminer", "pdfminer.high_level"):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4\n%placeholder\n")
    res = tx.extract(p)
    assert not res.available
    assert "pypdf" in (res.install_hint or "")


def test_extract_pdf_real(tmp_path: Path) -> None:
    # Synthesize a tiny one-page PDF with pypdf if it's around.
    pypdf = pytest.importorskip("pypdf")
    out = tmp_path / "tiny.pdf"
    w = pypdf.PdfWriter()
    w.add_blank_page(width=72, height=72)
    with out.open("wb") as f:
        w.write(f)
    res = tx.extract(out)
    assert res.available
    # Blank page → no readable text, but the call shouldn't crash. Backend is
    # pdfplumber (table-aware) when available, else pypdf/pdfminer.
    assert res.backend and res.backend.startswith(("pdfplumber", "pypdf"))


# ---------- docx / xlsx / xls / image (dep-missing branches) ----------

def _patch_missing(monkeypatch, blocked: tuple[str, ...]) -> None:
    import builtins
    real_import = builtins.__import__
    def fake_import(name: str, *a, **kw):
        if name in blocked or any(name.startswith(b + ".") for b in blocked):
            raise ImportError(name)
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_extract_docx_dep_missing(tmp_path: Path, monkeypatch) -> None:
    _patch_missing(monkeypatch, ("docx",))
    p = tmp_path / "x.docx"
    p.write_bytes(b"PK\x03\x04stub")
    res = tx.extract(p)
    assert not res.available
    assert "python-docx" in (res.install_hint or "")


def test_extract_xlsx_dep_missing(tmp_path: Path, monkeypatch) -> None:
    _patch_missing(monkeypatch, ("openpyxl",))
    p = tmp_path / "x.xlsx"
    p.write_bytes(b"PK\x03\x04stub")
    res = tx.extract(p)
    assert not res.available
    assert "openpyxl" in (res.install_hint or "")


def test_extract_xls_dep_missing(tmp_path: Path, monkeypatch) -> None:
    _patch_missing(monkeypatch, ("xlrd",))
    p = tmp_path / "x.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0stub")
    res = tx.extract(p)
    assert not res.available
    assert "xlrd" in (res.install_hint or "")


def test_extract_image_dep_missing(tmp_path: Path, monkeypatch) -> None:
    _patch_missing(monkeypatch, ("pytesseract", "PIL"))
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nstub")
    res = tx.extract(p)
    assert not res.available
    assert "tesseract" in (res.install_hint or "")


# ---------- detector wiring ----------

def test_default_detectors_include_new_kinds() -> None:
    from ara.forge.manifest.kinds import (
        CSVDetector,
        DOCXDetector,
        HTMLDetector,
        TextFileDetector,
        XLSDetector,
        XLSXDetector,
        default_detectors,
    )
    seen = {type(d).__name__ for d in default_detectors()}
    for cls in (HTMLDetector, CSVDetector, DOCXDetector,
                XLSXDetector, XLSDetector, TextFileDetector):
        assert cls.__name__ in seen


def test_html_detector_emits_report_sections(tmp_path: Path) -> None:
    """End-to-end: drop an HTML file, run the detector, get
    REPORT_SECTION elements ready for the auto-extractor."""
    from ara.forge.manifest.element import ManifestKind
    from ara.forge.manifest.kinds import HTMLDetector
    p = tmp_path / "rep.html"
    p.write_text(
        "<html><body>"
        "<h2>Finding 1: XSS in /search</h2>"
        "<p>The search field reflects user input unescaped.</p>"
        "<h2>Finding 2: SQLi in /login</h2>"
        "<p>The login form is concatenating a SQL string.</p>"
        "</body></html>"
    )
    elements = HTMLDetector().enumerate(p, mat_id="m1")
    assert all(e.kind == ManifestKind.REPORT_SECTION for e in elements)
    headings = [e.metadata.get("heading") for e in elements]
    assert any("XSS" in (h or "") for h in headings)
    assert any("SQLi" in (h or "") for h in headings)


def test_csv_detector_emits_report_section(tmp_path: Path) -> None:
    from ara.forge.manifest.element import ManifestKind
    from ara.forge.manifest.kinds import CSVDetector
    p = tmp_path / "data.csv"
    p.write_text("a,b,c\n1,2,3\n")
    elements = CSVDetector().enumerate(p, mat_id="m1")
    assert len(elements) == 1
    assert elements[0].kind == ManifestKind.REPORT_SECTION
    assert "a | b | c" in elements[0].extracted_text


def test_pdf_detector_falls_back_when_dep_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Without pypdf installed, the PDF detector still produces an
    element — it just falls back to PDF_PAGE with an install hint."""
    _patch_missing(monkeypatch, ("pypdf", "pdfminer", "pdfminer.high_level"))
    from ara.forge.manifest.element import ManifestKind
    from ara.forge.manifest.kinds import PDFDetector
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    elements = PDFDetector().enumerate(p, mat_id="m1")
    assert len(elements) == 1
    el = elements[0]
    assert el.kind == ManifestKind.PDF_PAGE
    assert "pypdf" in el.metadata.get("install_hint", "")
