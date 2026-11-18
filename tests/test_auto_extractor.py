"""Tests for forge.alphabets.auto_extractor."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara.forge import state as fstate
from ara.forge.alphabets.auto_extractor import (
    AutoExtractReport,
    ExtractedConcBody,
    HeuristicOffsecExtractor,
    _resolve_category,
    _resolve_fix,
    _resolve_rubric_cell,
    get_extractor,
    register_extractor,
    run_auto_extract,
)
from ara.forge.alphabets.pack import load_builtin_pack
from ara.forge.manifest.element import ManifestElement, ManifestKind
from ara.forge.phases import deposit, exhaust, manifest
from ara.forge.session import ForgeConfig, ForgePhase, ForgeSession
from ara.runtime import audit as raudit


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    raudit.reset_registry_for_tests()
    yield
    raudit.reset_registry_for_tests()


# ---------- pure heuristic helpers ----------

def test_resolve_category_known_keywords() -> None:
    pack = load_builtin_pack("offsec")
    cat, _ = _resolve_category("Reflected XSS in /search", pack)
    assert cat == "xss.reflected"
    cat, _ = _resolve_category("SQL injection in /login", pack)
    assert cat == "sqli"
    cat, _ = _resolve_category("Insecure Direct Object Reference", pack)
    assert cat == "idor"
    cat, _ = _resolve_category("Server-Side Request Forgery", pack)
    assert cat == "ssrf"


def test_resolve_category_falls_back_to_other_with_proposal() -> None:
    pack = load_builtin_pack("offsec")
    text = "Some completely novel thing that doesn't match keywords"
    cat, proposed = _resolve_category(text, pack)
    assert cat == "other"
    assert proposed and "novel" in proposed


def test_resolve_rubric_cell_from_severity_line() -> None:
    text = "Severity: High\n\nLeaks PII through the /users endpoint."
    out = _resolve_rubric_cell(text)
    assert out is not None
    coords, cell = out
    assert coords["data_class"] == "PII"
    assert cell["bucket"] == "high"
    assert cell["score"] >= 7.0


def test_resolve_rubric_cell_from_cvss() -> None:
    text = "CVSS: 9.5\n\nPrivilege escalation lets attacker take over admin account."
    out = _resolve_rubric_cell(text)
    assert out is not None
    coords, cell = out
    assert cell["score"] == 9.5
    assert cell["bucket"] == "critical"
    assert coords["access"] == "admin"


def test_resolve_rubric_cell_returns_none_without_severity() -> None:
    out = _resolve_rubric_cell("Just a description with no severity markers.")
    assert out is None


def test_resolve_rubric_cell_access_inference() -> None:
    text = "Severity: Medium\n\nLeaks email addresses (read-only) from the API."
    out = _resolve_rubric_cell(text)
    assert out is not None
    coords, _ = out
    assert coords["access"] == "read"

    text2 = "Severity: Medium\n\nAttacker can modify other users' orders."
    out2 = _resolve_rubric_cell(text2)
    assert out2 is not None
    coords2, _ = out2
    assert coords2["access"] == "write"


def test_resolve_fix_from_remediation_section() -> None:
    text = (
        "## Finding\nReflected XSS.\n\n"
        "## Remediation\n"
        "Encode all reflected user input in HTML and JavaScript contexts.\n"
        "Use an output encoder; never blacklist.\n"
    )
    fix = _resolve_fix(text, "xss.reflected")
    assert fix is not None
    assert fix["applicable_categories"] == ["xss.reflected"]
    assert "Encode" in fix["body"]
    assert fix["id"].startswith("fix.xss.reflected.")


def test_resolve_fix_returns_none_without_remediation() -> None:
    assert _resolve_fix("just prose, no fix section", "xss.reflected") is None


def test_resolve_fix_returns_none_without_category() -> None:
    text = "## Remediation\nDo a thing."
    assert _resolve_fix(text, None) is None


# ---------- HeuristicOffsecExtractor ----------

def _make_element(*, mat_id: str, heading: str, body: str,
                   eid: str = "elem_a") -> ManifestElement:
    text = f"## {heading}\n\n{body}\n"
    data = text.encode()
    import hashlib
    return ManifestElement(
        id=eid,
        source_mat_id=mat_id,
        byte_range=(0, len(data)),
        sha256=hashlib.sha256(data).hexdigest(),
        kind=ManifestKind.REPORT_SECTION,
        extracted_text=text,
        metadata={"heading": heading, "filename": "report.md"},
    )


def test_extractor_emits_three_alphabets_per_section() -> None:
    el = _make_element(
        mat_id="m1",
        heading="Reflected XSS in /search",
        body=(
            "Severity: Medium\n\n"
            "User input reflected in /search?q=... without HTML encoding.\n\n"
            "## Remediation\n"
            "Encode reflected user input in the HTML context.\n"
        ),
    )
    pack = load_builtin_pack("offsec")
    extractor = HeuristicOffsecExtractor()
    out = extractor.extract(elements=[el], mats_dir=Path("/tmp"), pack=pack)
    by_alpha = {b.alphabet_name for b in out}
    assert by_alpha == {"category", "severity_rubric", "fix_library"}
    cat = next(b for b in out if b.alphabet_name == "category")
    assert cat.body["entry"]["id"] == "xss.reflected"


def test_extractor_dedupes_repeated_categories() -> None:
    el1 = _make_element(mat_id="m1", heading="XSS reflected #1",
                         body="Severity: Low\n", eid="e1")
    el2 = _make_element(mat_id="m1", heading="Reflected XSS #2",
                         body="Severity: Low\n", eid="e2")
    pack = load_builtin_pack("offsec")
    out = HeuristicOffsecExtractor().extract(
        elements=[el1, el2], mats_dir=Path("/tmp"), pack=pack,
    )
    cat_count = sum(1 for b in out if b.alphabet_name == "category")
    assert cat_count == 1  # both elements proposed xss.reflected; first wins


def test_extractor_skips_dropped_elements() -> None:
    el = _make_element(mat_id="m1", heading="XSS", body="Severity: Low")
    el = ManifestElement(**{**el.__dict__, "dropped_by_operator": True})
    out = HeuristicOffsecExtractor().extract(
        elements=[el], mats_dir=Path("/tmp"),
        pack=load_builtin_pack("offsec"),
    )
    assert out == []


def test_extractor_skips_non_report_section() -> None:
    el = ManifestElement(
        id="e", source_mat_id="m", byte_range=(0, 1),
        sha256="0" * 64, kind=ManifestKind.MD_SECTION,
        extracted_text="just a regular section", metadata={},
    )
    out = HeuristicOffsecExtractor().extract(
        elements=[el], mats_dir=Path("/tmp"),
        pack=load_builtin_pack("offsec"),
    )
    assert out == []


def test_register_extractor_replaces_existing() -> None:
    class _Custom:
        pack_name = "test_custom"
        def extract(self, **_): return []
    register_extractor(_Custom())
    assert get_extractor("test_custom") is not None


# ---------- run_auto_extract orchestrator ----------

def _build_forge_with_report(tmp_path: Path) -> ForgeSession:
    state_root = tmp_path / "state"
    src = tmp_path / "src"
    src.mkdir()
    mat = src / "example_report.md"
    mat.write_text(
        "# Example report\n\n"
        "## Finding A — Reflected XSS in /search\n"
        "Severity: Medium\n\n"
        "User input reflected in /search?q=... without HTML encoding.\n\n"
        "## Remediation\n"
        "Encode reflected user input in HTML context.\n\n"
        "## Finding B — IDOR on /api/orders/{id}\n"
        "Severity: High\n\n"
        "Sequential ids let attacker modify other tenants' orders.\n\n"
        "## Remediation\n"
        "Add authorization check on read AND write.\n"
    )
    sess = ForgeSession.new(
        state_root, weapon_target_id="w1", operator_id="op",
        config=ForgeConfig(alphabet_pack="offsec"),
    )
    deposit.deposit_mat(sess, mat)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    # Cover every element with a noop extract.proc so coverage works
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess, manifest_element_id=el.id, channel="extract.proc",
            body={"crumb_id": f"c_{i}", "name": "x"},
            extraction_id=f"x_proc_{i}",
        )
    return sess


def test_run_auto_extract_records_extractions(tmp_path: Path) -> None:
    sess = _build_forge_with_report(tmp_path)
    report = run_auto_extract(sess)
    assert report.pack_name == "offsec"
    assert report.elements_scanned >= 2
    assert report.extractions_recorded >= 4  # at least 2 cats + 2 severities
    # Each alphabet hit
    assert report.by_alphabet.get("category", 0) >= 2
    assert report.by_alphabet.get("severity_rubric", 0) >= 2
    assert report.by_alphabet.get("fix_library", 0) >= 2


def test_run_auto_extract_skips_existing_on_rerun(tmp_path: Path) -> None:
    sess = _build_forge_with_report(tmp_path)
    first = run_auto_extract(sess)
    second = run_auto_extract(sess)
    assert second.skipped_existing >= first.extractions_recorded
    assert second.extractions_recorded == 0


def test_run_auto_extract_no_skip_re_records(tmp_path: Path) -> None:
    sess = _build_forge_with_report(tmp_path)
    run_auto_extract(sess)
    second = run_auto_extract(sess, skip_existing=False)
    assert second.extractions_recorded > 0


def test_run_auto_extract_requires_exhaust_phase(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sess = ForgeSession.new(
        state_root, weapon_target_id="w", operator_id="op",
        config=ForgeConfig(alphabet_pack="offsec"),
    )
    with pytest.raises(ValueError, match="EXHAUST"):
        run_auto_extract(sess)


def test_run_auto_extract_no_pack_returns_zero(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text("# X\n")
    sess = ForgeSession.new(
        state_root, weapon_target_id="w", operator_id="op",
        config=ForgeConfig(alphabet_pack=""),  # no pack
    )
    deposit.deposit_mat(sess, src / "x.md")
    deposit.finalize_deposit(sess)
    manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    report = run_auto_extract(sess)
    assert report.pack_name == ""
    assert report.extractions_recorded == 0


# ---------- end-to-end through synthesize+seal ----------

def test_auto_extract_then_synthesize_and_seal_produces_alphabets(
    tmp_path: Path,
) -> None:
    """Full happy path: deposit example-report mat, run auto-extract,
    synthesize, seal. The sealed weapon should ship populated alphabet
    files even though the operator never recorded a single extract.conc
    by hand."""
    from ara.forge.phases import seal as seal_mod
    from ara.forge.phases import synthesize as synth_mod
    sess = _build_forge_with_report(tmp_path)
    run_auto_extract(sess)
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    draft = synth_mod.synthesize(sess)
    assert draft.alphabet_pack_name == "offsec"
    assert "category" in draft.alphabets
    sess.transition(ForgePhase.REVIEW, reason="synth done")
    seal_mod.request_seal(sess)
    result = seal_mod.seal(sess, weapon_id="w1")
    assert result.ok, f"seal failed: {result.failures}"
    weapon_dir = fstate.weapon_dir(sess.state_root, "w1")
    cat_data = json.loads((weapon_dir / "alphabets" / "category.json").read_text())
    cat_ids = {e["id"] for e in cat_data["entries"]}
    assert "xss.reflected" in cat_ids
    assert "idor" in cat_ids
    rubric_data = json.loads(
        (weapon_dir / "alphabets" / "severity_rubric.json").read_text(),
    )
    assert len(rubric_data["cells"]) >= 1
    fix_data = json.loads((weapon_dir / "alphabets" / "fix_library.json").read_text())
    assert len(fix_data["templates"]) >= 1
