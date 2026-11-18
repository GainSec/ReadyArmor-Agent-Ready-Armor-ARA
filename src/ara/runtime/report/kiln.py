"""Report Kiln — render signed-off findings into structured markdown.

Pipeline:
1. Pull signed-off findings from FindingStore.
2. Build the rendering context (engagement, weapon, severity_dist,
   findings, optional fired_crumbs / pending_leads / evidence_index).
3. For each section in the template:
   - ``static`` → render with the mini-engine.
   - ``finding_loop`` → render once per finding, splice with `\\n`.
   - ``auto`` → call the named generator.
4. Persist the Report record + write the markdown file.

The mini-engine (``runtime.report.engine``) is SSTI-safe; the
findings + leads + evidence dicts come from inside the trust boundary
(audit-logged channel writes) so prose-injection of `{{ ... }}` is
not a feasible attack — but the engine still refuses callable values
defensively.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.findings.store import FindingStore
from ara.runtime.leads.store import LeadStore
from ara.runtime.report.engine import TemplateError, render
from ara.runtime.report.record import (
    Report,
    ReportStore,
    compute_content_hash,
    mint_report_id,
)
from ara.runtime.report.sections import get_generator
from ara.runtime.report.template import (
    SectionSpec,
    WeaponReportTemplate,
    offsec_default,
)


@dataclass
class ReportRequest:
    engagement_id: str
    weapon_id: str
    session_dir: Path
    template: WeaponReportTemplate | None = None  # None → offsec_default
    fired_crumbs: list[dict] = field(default_factory=list)
    out_path: Path | None = None  # None → session_dir/report.md


@dataclass(frozen=True)
class SectionDiagnostic:
    name: str
    ok: bool
    skipped: bool = False
    error: str | None = None
    bytes_written: int = 0


def _build_context(req: ReportRequest, findings: list[dict],
                    leads_pending: list[dict], leads_archived: list[dict],
                    evidence: list[dict]) -> dict:
    breakdown: Counter[str] = Counter(
        (f.get("severity_name") or "info") for f in findings
    )
    return {
        "engagement": {"id": req.engagement_id},
        "weapon": {"id": req.weapon_id},
        "rendered_at_iso": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "findings": findings,
        "findings_count": len(findings),
        "severity_dist": {
            "critical": breakdown.get("critical", 0),
            "high": breakdown.get("high", 0),
            "medium": breakdown.get("medium", 0),
            "low": breakdown.get("low", 0),
            "info": breakdown.get("info", 0),
        },
        "fired_crumbs": req.fired_crumbs,
        "pending_leads": leads_pending,
        "archived_leads": leads_archived,
        "evidence_index": evidence,
    }


def _evidence_index_from_binder(binder: EvidenceBinder) -> list[dict]:
    """Best-effort dump of every evidence record for the appendix.

    Uses the binder's index directly (not search) so we don't need a
    query string. Bounded by what fits in memory for v1.
    """
    rows = binder._index._conn.execute(  # noqa: SLF001 (intentional)
        "SELECT id, source, captured_at, summary "
        "FROM evidence ORDER BY captured_at"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        ts = r["captured_at"]
        try:
            iso = (
                datetime.fromtimestamp(float(ts), tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (TypeError, ValueError):
            iso = ""
        out.append({
            "id": r["id"], "source": r["source"],
            "captured_at_iso": iso, "summary": r["summary"],
        })
    return out


def render_report(req: ReportRequest) -> tuple[Report, list[SectionDiagnostic]]:
    template = req.template or offsec_default()
    findings_store = FindingStore(req.session_dir)
    findings = [f.to_jsonable() for f in findings_store.signed_off()]
    findings.sort(key=lambda f: -float(f.get("severity_score", 0.0)))

    lead_store = LeadStore(req.session_dir)
    leads_pending = [
        ld.to_jsonable() for ld in lead_store.by_status("open", "investigating")
    ]
    leads_archived = [
        ld.to_jsonable() for ld in lead_store.by_status("archived")
    ]

    binder = EvidenceBinder(req.session_dir)
    evidence_index = _evidence_index_from_binder(binder)

    ctx = _build_context(
        req, findings,
        leads_pending=leads_pending, leads_archived=leads_archived,
        evidence=evidence_index,
    )

    parts: list[str] = []
    diagnostics: list[SectionDiagnostic] = []

    for sec in template.sections:
        try:
            content = _render_section(sec, ctx)
        except TemplateError as e:
            diagnostics.append(SectionDiagnostic(
                name=sec.name, ok=False, error=str(e),
            ))
            continue
        if not content.strip() and sec.optional:
            diagnostics.append(SectionDiagnostic(
                name=sec.name, ok=True, skipped=True,
            ))
            continue
        if sec.title:
            parts.append(f"## {sec.title}\n\n")
        parts.append(content)
        if not content.endswith("\n"):
            parts.append("\n")
        diagnostics.append(SectionDiagnostic(
            name=sec.name, ok=True, bytes_written=len(content),
        ))

    body = "".join(parts)
    rendered_at = time.time()
    rid = mint_report_id()
    report = Report(
        id=rid,
        engagement_id=req.engagement_id,
        weapon_id=req.weapon_id,
        template_version=template.version,
        findings_included=[f["id"] for f in findings],
        body_markdown=body,
        rendered_at=rendered_at,
        section_diagnostics=[
            {"name": d.name, "ok": d.ok, "skipped": d.skipped,
             "error": d.error, "bytes_written": d.bytes_written}
            for d in diagnostics
        ],
    )
    # Compute content hash over a copy WITHOUT the hash field itself
    from dataclasses import replace
    report = replace(report, content_hash=compute_content_hash(report))

    out_path = req.out_path or (req.session_dir / "report.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body)

    ReportStore(req.session_dir).append(report)
    return report, diagnostics


def _render_section(sec: SectionSpec, ctx: dict) -> str:
    if sec.kind == "static":
        return render(sec.template_src, ctx)
    if sec.kind == "auto":
        gen = get_generator(sec.auto_generator)
        if gen is None:
            raise TemplateError(
                f"unknown auto_generator: {sec.auto_generator!r}"
            )
        return gen(ctx)
    if sec.kind == "finding_loop":
        findings = ctx.get("findings") or []
        if not findings:
            return ""
        chunks: list[str] = []
        for f in findings:
            inner_ctx = dict(ctx)
            inner_ctx["finding"] = f
            chunks.append(render(sec.template_src, inner_ctx))
        return "\n".join(chunks)
    raise TemplateError(f"unknown section kind: {sec.kind!r}")
