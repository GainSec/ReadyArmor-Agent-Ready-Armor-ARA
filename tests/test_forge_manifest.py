"""Tests for forge.manifest (element + kinds + enumerate)."""
from __future__ import annotations

import json
from pathlib import Path

from ara.forge.manifest.element import (
    ManifestElement,
    ManifestKind,
    make_element_id,
    read_manifest,
    write_manifest,
)
from ara.forge.manifest.enumerate import (
    coverage,
    enumerate_mats,
    write_coverage,
)
from ara.forge.manifest.kinds import (
    CodeRepoDetector,
    ExampleReportDetector,
    GenericTextDetector,
    MarkdownDetector,
    PDFDetector,
    URLSnapshotDetector,
    default_detectors,
)


def test_make_element_id_deterministic() -> None:
    a = make_element_id(b"hello", "foo.md")
    b = make_element_id(b"hello", "foo.md")
    c = make_element_id(b"hello", "bar.md")
    d = make_element_id(b"world", "foo.md")
    assert a == b
    assert a != c
    assert a != d
    assert a.startswith("elem_") and len(a) == 5 + 10


def test_manifest_element_round_trip(tmp_path: Path) -> None:
    el = ManifestElement(
        id="elem_abc",
        source_mat_id="mat_x",
        byte_range=(0, 100),
        sha256="0" * 64,
        kind=ManifestKind.MD_SECTION,
        extracted_text="hello",
        metadata={"heading": "Intro", "level": 1},
    )
    p = tmp_path / "manifest.jsonl"
    write_manifest(p, [el])
    loaded = read_manifest(p)
    assert len(loaded) == 1
    assert loaded[0].id == "elem_abc"
    assert loaded[0].byte_range == (0, 100)
    assert loaded[0].kind == ManifestKind.MD_SECTION


def test_markdown_detector_section_split(tmp_path: Path) -> None:
    md = "# A\nbody A\n\n## B\nbody B\n\n## C\nbody C\n"
    p = tmp_path / "doc.md"
    p.write_text(md)
    d = MarkdownDetector()
    assert d.detect(p)
    elements = d.enumerate(p, mat_id="mat_doc")
    assert len(elements) == 3
    assert all(e.kind == ManifestKind.MD_SECTION for e in elements)
    headings = [e.metadata["heading"] for e in elements]
    assert headings == ["A", "B", "C"]


def test_markdown_no_headings_one_element(tmp_path: Path) -> None:
    p = tmp_path / "plain.md"
    p.write_text("just plain text body.")
    elements = MarkdownDetector().enumerate(p, mat_id="m")
    assert len(elements) == 1
    assert elements[0].metadata["heading"] is None


def test_code_detector_small_file(tmp_path: Path) -> None:
    p = tmp_path / "main.py"
    p.write_text("def f():\n    return 1\n")
    d = CodeRepoDetector()
    assert d.detect(p)
    elements = d.enumerate(p, mat_id="m")
    assert len(elements) == 1
    assert elements[0].kind == ManifestKind.CODE_BLOCK
    assert elements[0].metadata["language"] == "py"


def test_code_detector_large_file_chunks(tmp_path: Path) -> None:
    p = tmp_path / "big.py"
    p.write_text("# line\n" * 5000)  # ~35 KB
    elements = CodeRepoDetector().enumerate(p, mat_id="m")
    assert len(elements) >= 3
    assert all(e.kind == ManifestKind.CODE_BLOCK for e in elements)
    # All chunks share the same source_mat_id
    assert {e.source_mat_id for e in elements} == {"m"}


def test_pdf_detector_only_claims_pdf_magic(tmp_path: Path) -> None:
    fake_pdf = tmp_path / "doc.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n...rest...")
    not_pdf = tmp_path / "doc2.pdf"
    not_pdf.write_bytes(b"NOT_PDF")
    d = PDFDetector()
    assert d.detect(fake_pdf)
    assert not d.detect(not_pdf)
    elements = d.enumerate(fake_pdf, mat_id="m")
    assert len(elements) == 1
    assert elements[0].kind == ManifestKind.PDF_PAGE


def test_url_snapshot_detector(tmp_path: Path) -> None:
    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"url": "https://example.com", "body": "<html/>"}))
    d = URLSnapshotDetector()
    assert d.detect(p)
    elements = d.enumerate(p, mat_id="m")
    assert elements[0].kind == ManifestKind.URL_SNAPSHOT
    assert elements[0].metadata["url"] == "https://example.com"


def test_url_snapshot_rejects_plain_json(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"foo": "bar"}))
    assert not URLSnapshotDetector().detect(p)


def test_example_report_detector(tmp_path: Path) -> None:
    md = "# Report\n\n## Finding 1\nXSS in /search\n\n## Finding 2\nSQLi in /login\n"
    p = tmp_path / "report.md"
    p.write_text(md)
    d = ExampleReportDetector()
    assert d.detect(p)
    elements = d.enumerate(p, mat_id="m")
    assert len(elements) == 2
    assert all(e.kind == ManifestKind.REPORT_SECTION for e in elements)


def test_generic_text_detector_text_only(tmp_path: Path) -> None:
    p = tmp_path / "log.txt"
    p.write_text("plain log line 1\nline 2\n")
    d = GenericTextDetector()
    assert d.detect(p)
    bin_p = tmp_path / "blob.bin"
    bin_p.write_bytes(bytes(range(256)) * 10)
    assert not d.detect(bin_p)


def test_example_report_detector_pack_keywords(tmp_path: Path) -> None:
    """Pack-supplied keywords let a non-offsec domain (e.g. code review)
    detect its own section names."""
    md = tmp_path / "review.md"
    md.write_text("# Review\n\n## Defect 1\nx\n\n## Smell 2\ny\n")
    # Default detector doesn't recognise these → returns false
    default = ExampleReportDetector()
    assert not default.detect(md)
    # Pack-supplied keywords pick them up
    code_review = ExampleReportDetector(keywords=("defect", "smell"))
    assert code_review.detect(md)
    elements = code_review.enumerate(md, mat_id="m")
    assert len(elements) == 2
    assert all(e.kind == ManifestKind.REPORT_SECTION for e in elements)


def test_default_detectors_accepts_pack_keywords() -> None:
    dets = default_detectors(report_section_keywords=("defect",))
    er = next(d for d in dets if type(d).__name__ == "ExampleReportDetector")
    assert er._keywords == ("defect",)


def test_default_detectors_priority_order() -> None:
    dets = default_detectors()
    priorities = [d.priority for d in dets]
    assert priorities == sorted(priorities)
    # ExampleReport must be ahead of Markdown
    names = [type(d).__name__ for d in dets]
    assert names.index("ExampleReportDetector") < names.index("MarkdownDetector")


def test_enumerate_mats_picks_highest_priority(tmp_path: Path) -> None:
    md_finding = tmp_path / "report.md"
    md_finding.write_text("# Findings\n\n## Finding A\ntext\n## Finding B\ntext\n")
    plain_md = tmp_path / "notes.md"
    plain_md.write_text("# Notes\n\nsome notes\n")
    code = tmp_path / "x.py"
    code.write_text("print(1)\n")
    elements = enumerate_mats(tmp_path)
    kinds = {e.kind for e in elements}
    assert ManifestKind.REPORT_SECTION in kinds
    assert ManifestKind.MD_SECTION in kinds
    assert ManifestKind.CODE_BLOCK in kinds


def test_coverage_aggregation(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("# h\nbody\n## h2\nbody2\n")
    code = tmp_path / "b.py"
    code.write_text("x = 1\n")
    elements = enumerate_mats(tmp_path)
    cov = coverage(elements)
    assert cov.total_elements == 3
    assert cov.total_mats == 2
    assert cov.by_kind.get("md_section") == 2
    assert cov.by_kind.get("code_block") == 1


def test_write_coverage(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("# h\nbody\n")
    elements = enumerate_mats(tmp_path)
    cov = coverage(elements)
    out = tmp_path / "manifest_coverage.json"
    write_coverage(out, cov)
    data = json.loads(out.read_text())
    assert data["total_mats"] == 1


def test_dropped_elements_excluded_from_coverage(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("# h1\nbody\n## h2\nbody2\n")
    elements = enumerate_mats(tmp_path)
    elements[0] = ManifestElement(**{**elements[0].__dict__, "dropped_by_operator": True})
    cov = coverage(elements)
    assert cov.total_elements == len(elements) - 1
