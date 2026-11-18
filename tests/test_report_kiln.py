"""Tests for runtime.report — engine, sections, kiln, channel."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext, ChannelRejection
from ara.runtime.findings.alphabets import (
    CategoryEntry, CategoryEnum, FixLibrary, FixTemplate,
    RubricCell, SeverityRubric,
    write_category_enum, write_fix_library, write_severity_rubric,
)
from ara.runtime.findings.channels import install_finding_channels
from ara.runtime.facts.channels import install_fact_channels
from ara.runtime.leads.channels import install_lead_channels
from ara.runtime.report.channels import install_report_channel
from ara.runtime.report.engine import TemplateError, render
from ara.runtime.report.kiln import ReportRequest, render_report
from ara.runtime.report.record import (
    Report, ReportStore, compute_content_hash, mint_report_id,
)
from ara.runtime.report.sections import (
    gen_appendix_evidence,
    gen_coverage_omissions,
    gen_executive_summary,
    gen_methodology,
)
from ara.runtime.report.template import (
    SectionSpec, WeaponReportTemplate,
    offsec_default, read as read_template, write as write_template,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()
    yield
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


# ---------- engine ----------

def test_engine_simple_substitution() -> None:
    out = render("hello {{ name }}", {"name": "world"})
    assert out == "hello world"


def test_engine_dotted_path() -> None:
    out = render("{{ a.b.c }}", {"a": {"b": {"c": "x"}}})
    assert out == "x"


def test_engine_missing_path_renders_empty() -> None:
    out = render("[{{ a.b.missing }}]", {"a": {"b": {}}})
    assert out == "[]"


def test_engine_for_loop() -> None:
    out = render(
        "{% for x in items %}- {{ x }}\n{% endfor %}",
        {"items": ["a", "b", "c"]},
    )
    assert out == "- a\n- b\n- c\n"


def test_engine_nested_for() -> None:
    out = render(
        "{% for r in rows %}{% for c in r.cols %}{{ c }} {% endfor %}\n{% endfor %}",
        {"rows": [{"cols": ["1", "2"]}, {"cols": ["3"]}]},
    )
    assert out == "1 2 \n3 \n"


def test_engine_if_truthy() -> None:
    out = render("{% if x %}yes{% endif %}", {"x": True})
    assert out == "yes"
    out2 = render("{% if x %}yes{% endif %}", {"x": False})
    assert out2 == ""
    out3 = render("{% if x %}yes{% endif %}", {"x": None})
    assert out3 == ""


def test_engine_for_iter_must_be_list() -> None:
    with pytest.raises(TemplateError, match="for-loop expects a list"):
        render("{% for x in y %}{% endfor %}", {"y": "a string"})


def test_engine_invalid_path_raises() -> None:
    with pytest.raises(TemplateError):
        render("{{ a-b }}", {"a-b": "x"})


def test_engine_unterminated_block() -> None:
    with pytest.raises(TemplateError, match="unterminated"):
        render("{% for x in y %}", {"y": []})


def test_engine_unknown_tag() -> None:
    with pytest.raises(TemplateError, match="unknown tag"):
        render("{% foobar %}", {})


def test_engine_comment_stripped() -> None:
    out = render("a {# this is gone #}b", {})
    assert out == "a b"


def test_engine_refuses_callable() -> None:
    """SSTI defence: even if a context value is somehow a callable,
    the engine must refuse to format it (not call it)."""
    def attacker():
        return "pwned"
    with pytest.raises(TemplateError, match="callable; refused"):
        render("{{ x }}", {"x": attacker})


def test_engine_no_method_calls_via_path() -> None:
    """Even with attribute traversal, the engine must not invoke methods."""
    class C:
        def __init__(self) -> None:
            self.value = "ok"

        def attack(self) -> str:
            return "oh no"

    obj = C()
    # Plain attribute renders fine
    assert render("{{ x.value }}", {"x": obj}) == "ok"
    # Method access raises — engine refuses callables
    with pytest.raises(TemplateError):
        render("{{ x.attack }}", {"x": obj})


# ---------- record ----------

def test_report_id_format() -> None:
    rid = mint_report_id()
    assert rid.startswith("R:") and len(rid) == 12


def test_report_round_trip(tmp_path: Path) -> None:
    s = ReportStore(tmp_path)
    r = Report(
        id=mint_report_id(), engagement_id="eng-1", weapon_id="w",
        template_version="1", findings_included=["F:abc"],
        body_markdown="# hi", rendered_at=time.time(),
    )
    s.append(r)
    assert s.by_id(r.id) == r


def test_compute_content_hash_changes_on_mutation() -> None:
    r1 = Report(
        id="R:abc", engagement_id="e", weapon_id="w",
        template_version="1", findings_included=[],
        body_markdown="hello", rendered_at=1.0,
    )
    h1 = compute_content_hash(r1)
    from dataclasses import replace
    r2 = replace(r1, body_markdown="hello world")
    assert compute_content_hash(r2) != h1


# ---------- template ----------

def test_template_round_trip(tmp_path: Path) -> None:
    tpl = offsec_default()
    p = tmp_path / "tpl.json"
    write_template(p, tpl)
    loaded = read_template(p)
    assert loaded.name == "offsec_default"
    assert {s.name for s in loaded.sections} == {
        "header", "executive_summary", "findings",
        "methodology", "coverage", "appendix_evidence",
    }


def test_offsec_default_section_kinds() -> None:
    tpl = offsec_default()
    by_name = {s.name: s for s in tpl.sections}
    assert by_name["header"].kind == "static"
    assert by_name["findings"].kind == "finding_loop"
    assert by_name["executive_summary"].kind == "auto"


# ---------- section auto-generators ----------

def test_executive_summary_with_findings() -> None:
    findings = [
        {"id": "F:1", "category": "xss.reflected", "severity_name": "medium",
         "affected_assets": ["target.test"]},
        {"id": "F:2", "category": "idor", "severity_name": "high",
         "affected_assets": ["target.test"]},
    ]
    out = gen_executive_summary({"findings": findings})
    assert "2 signed-off" in out
    assert "high" in out
    assert "`xss.reflected`" in out


def test_executive_summary_empty() -> None:
    out = gen_executive_summary({"findings": []})
    assert "No findings" in out


def test_methodology_with_crumbs() -> None:
    out = gen_methodology({"fired_crumbs": [
        {"crumb_id": "c1", "name": "probe sqli"},
        {"crumb_id": "c2", "name": "probe xss"},
    ]})
    assert "`c1`" in out
    assert "`c2`" in out


def test_methodology_empty_returns_placeholder() -> None:
    out = gen_methodology({"fired_crumbs": []})
    assert "not available" in out


def test_coverage_omissions_pending_leads() -> None:
    out = gen_coverage_omissions({"pending_leads": [
        {"id": "L:1", "title": "open IDOR"},
    ], "archived_leads": []})
    assert "Open leads" in out
    assert "L:1" in out


def test_appendix_evidence_table() -> None:
    out = gen_appendix_evidence({"evidence_index": [
        {"id": "e:1", "source": "tool:curl", "captured_at_iso": "2026-04-25T00:00:00Z",
         "summary": "x"},
    ]})
    assert "`e:1`" in out
    assert "tool:curl" in out


# ---------- kiln end-to-end ----------

def _seed_alphabets(weapon_dir: Path) -> None:
    write_category_enum(
        weapon_dir / "alphabets" / "category_enum.json",
        CategoryEnum(entries={
            "xss.reflected": CategoryEntry(id="xss.reflected", label="Reflected XSS"),
            "idor": CategoryEntry(id="idor", label="IDOR"),
        }),
    )
    write_severity_rubric(
        weapon_dir / "alphabets" / "severity_rubric.json",
        SeverityRubric(
            data_classes=["PII"], accesses=["read", "write"],
            matrix={"PII": {"read": RubricCell(6.0, "medium"),
                            "write": RubricCell(8.0, "high")}},
        ),
    )
    write_fix_library(
        weapon_dir / "alphabets" / "fix_library.json",
        FixLibrary(templates={
            "fix.xss.encode_output": FixTemplate(
                id="fix.xss.encode_output", title="Encode output",
                applicable_categories=["xss.reflected"],
                body="encode at boundary",
            ),
        }),
    )


def _setup_engagement(tmp_path: Path) -> tuple[Path, Path]:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    _seed_alphabets(weapon_dir)
    return sessions_root, eng_dir, weapon_dir  # type: ignore[return-value]


def _file_and_sign_off_finding(
    sessions_root: Path, eng_dir: Path, weapon_dir: Path,
    *, title: str, category: str, cell: list,
) -> str:
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={},
                     summary="s", activation_id="act-1")
    install_fact_channels(sessions_root=sessions_root)
    install_lead_channels(sessions_root=sessions_root)
    chans = install_finding_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )
    ctx_a = ChannelContext(
        engagement_id="eng-1", activation_id="act-1",
        caller="agent", audit_sink=eng_dir / "events.jsonl",
    )
    lead_chan = registry.get("lead.file")
    out = lead_chan.call({"title": "lead",
                           "reasoning": "r",
                           "evidence_refs": [rec.id]}, ctx_a)
    drafted = chans["finding.file"].call({
        "title": title,
        "category": category,
        "severity_cell": cell,
        "impact": {"confidentiality": 2, "integrity": 0, "availability": 0},
        "evidence_refs": [rec.id],
        "fact_refs": [],
        "lead_refs": [out["lead"]["id"]],
        "provenance_chain": ["c_000", out["lead"]["id"]],
        "fix_template_id": "fix.xss.encode_output" if category == "xss.reflected" else None,
        "fix_custom": None if category == "xss.reflected" else {
            "what_changed": "Add owner check on /api/orders/{id}",
            "why_it_fixes": "Blocks cross-tenant read",
            "verification_method": "fetch with non-owner token expects 403",
        },
        "origin": "agent_derived",
        "affected_assets": ["target.test"],
    }, ctx_a)
    ctx_o = ChannelContext(
        engagement_id="eng-1", activation_id="act-1",
        caller="operator", audit_sink=eng_dir / "events.jsonl",
    )
    chans["finding.sign_off"].call({
        "finding_id": drafted["finding"]["id"], "operator_id": "op-1",
    }, ctx_o)
    return drafted["finding"]["id"]


def test_render_report_offsec_template_with_findings(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _setup_engagement(tmp_path)
    fid_xss = _file_and_sign_off_finding(
        sessions_root, eng_dir, weapon_dir,
        title="Reflected XSS in /search",
        category="xss.reflected", cell=["PII", "read"],
    )
    fid_idor = _file_and_sign_off_finding(
        sessions_root, eng_dir, weapon_dir,
        title="IDOR on /api/orders/{id}",
        category="idor", cell=["PII", "write"],
    )

    req = ReportRequest(
        engagement_id="eng-1", weapon_id="wstg-mini-v1",
        session_dir=eng_dir,
        fired_crumbs=[{"crumb_id": "c_000", "name": "probe IDOR"}],
    )
    report, diagnostics = render_report(req)
    body = report.body_markdown
    # Header has the expected counts
    assert "# Engagement report — eng-1" in body
    assert "2 signed off" in body
    # Both findings rendered with their titles
    assert "Reflected XSS in /search" in body
    assert "IDOR on /api/orders/{id}" in body
    # Categories rendered in finding blocks
    assert "`xss.reflected`" in body
    assert "`idor`" in body
    # Methodology section reflects the fired crumb
    assert "`c_000`" in body
    # Diagnostics: every section either ok or skipped
    assert all(d.ok for d in diagnostics)
    # Persisted: report.md on disk + report record in store
    assert (eng_dir / "report.md").exists()
    store_report = ReportStore(eng_dir).by_id(report.id)
    assert store_report is not None
    assert store_report.findings_included == [fid_idor, fid_xss] or \
           store_report.findings_included == [fid_xss, fid_idor]
    assert report.content_hash and report.content_hash.startswith("sha256:")


def test_render_report_no_findings_skips_finding_loop(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _setup_engagement(tmp_path)
    req = ReportRequest(
        engagement_id="eng-1", weapon_id="w", session_dir=eng_dir,
    )
    report, diagnostics = render_report(req)
    assert "0 signed off" in report.body_markdown
    assert "No findings" in report.body_markdown
    by_name = {d.name: d for d in diagnostics}
    # finding_loop section produced empty content; engine still renders
    # it ok though the body is "" (no title injected by kiln since
    # block-renderer outputs "" for empty findings list)
    assert by_name["findings"].ok


def test_render_report_findings_sorted_by_severity(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _setup_engagement(tmp_path)
    _file_and_sign_off_finding(
        sessions_root, eng_dir, weapon_dir,
        title="Low risk thing",
        category="xss.reflected", cell=["PII", "read"],   # medium
    )
    _file_and_sign_off_finding(
        sessions_root, eng_dir, weapon_dir,
        title="High risk thing",
        category="idor", cell=["PII", "write"],            # high
    )
    req = ReportRequest(
        engagement_id="eng-1", weapon_id="w", session_dir=eng_dir,
    )
    report, _ = render_report(req)
    body = report.body_markdown
    # Higher severity finding should appear before lower in the
    # finding_loop output
    assert body.index("High risk thing") < body.index("Low risk thing")


def test_report_content_hash_pinned(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _setup_engagement(tmp_path)
    _file_and_sign_off_finding(
        sessions_root, eng_dir, weapon_dir,
        title="t", category="xss.reflected", cell=["PII", "read"],
    )
    req = ReportRequest(
        engagement_id="eng-1", weapon_id="w", session_dir=eng_dir,
    )
    r1, _ = render_report(req)
    r2, _ = render_report(req)
    # IDs differ (fresh uuid each call); body+hash differ in
    # rendered_at_iso; this just confirms hash is well-formed.
    assert r1.content_hash != r2.content_hash
    assert all(r.content_hash.startswith("sha256:") for r in (r1, r2))


# ---------- channel ----------

def test_report_render_channel_operator_only(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _setup_engagement(tmp_path)
    install_report_channel(sessions_root=sessions_root)
    ch = registry.get("report.render")
    ctx_a = ChannelContext(
        engagement_id="eng-1", activation_id="a-1",
        caller="agent", audit_sink=eng_dir / "events.jsonl",
    )
    with pytest.raises(ChannelRejection, match="not allowed"):
        ch.call({"weapon_id": "w"}, ctx_a)


def test_report_render_channel_happy_path(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _setup_engagement(tmp_path)
    _file_and_sign_off_finding(
        sessions_root, eng_dir, weapon_dir,
        title="t", category="xss.reflected", cell=["PII", "read"],
    )
    install_report_channel(sessions_root=sessions_root)
    ch = registry.get("report.render")
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a-1",
        caller="operator", audit_sink=eng_dir / "events.jsonl",
    )
    out = ch.call({"weapon_id": "w"}, ctx)
    assert out["report_id"].startswith("R:")
    assert len(out["findings_included"]) == 1
    assert out["content_hash"].startswith("sha256:")
    assert (eng_dir / "report.md").exists()


def test_report_render_channel_with_supplied_template(tmp_path: Path) -> None:
    sessions_root, eng_dir, weapon_dir = _setup_engagement(tmp_path)
    install_report_channel(sessions_root=sessions_root)
    ch = registry.get("report.render")
    minimal_tpl = WeaponReportTemplate(
        name="minimal", version="1", description="",
        sections=[
            SectionSpec(name="hdr", kind="static",
                        template_src="# {{ engagement.id }}\n"),
        ],
    )
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a-1",
        caller="operator", audit_sink=eng_dir / "events.jsonl",
    )
    out = ch.call({
        "weapon_id": "w",
        "template": minimal_tpl.to_jsonable(),
    }, ctx)
    body = (eng_dir / "report.md").read_text()
    assert body.startswith("# eng-1")


def test_report_render_channel_invalid_template_rejected(tmp_path: Path) -> None:
    sessions_root, eng_dir, _weapon = _setup_engagement(tmp_path)
    install_report_channel(sessions_root=sessions_root)
    ch = registry.get("report.render")
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a-1",
        caller="operator", audit_sink=eng_dir / "events.jsonl",
    )
    with pytest.raises(ChannelRejection, match="invalid template"):
        ch.call({"weapon_id": "w", "template": {"bogus": "structure"}}, ctx)
