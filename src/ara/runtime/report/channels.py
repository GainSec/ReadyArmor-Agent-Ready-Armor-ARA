"""report.render typed channel.

Operator-only. Renders a report from the engagement's signed-off
findings + the offsec default template (or a supplied template).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection
from ara.runtime.report.kiln import ReportRequest, render_report
from ara.runtime.report.template import WeaponReportTemplate


@dataclass
class ReportRenderBody:
    weapon_id: str
    fired_crumbs: list[dict] = field(default_factory=list)
    template: dict | None = None       # serialized WeaponReportTemplate
    out_relpath: str | None = None     # relative to session_dir


class ReportRenderChannel(Channel):
    name = "report.render"
    schema = ReportRenderBody
    sink_subsystem = "reports.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        if not ctx.engagement_id:
            raise ChannelRejection("engagement_id required")
        session_dir = self.sessions_root / ctx.engagement_id
        template = None
        if body.get("template"):
            try:
                template = WeaponReportTemplate.from_jsonable(body["template"])
            except Exception as e:
                raise ChannelRejection(f"invalid template: {e}") from e
        out_path = None
        if body.get("out_relpath"):
            out_path = session_dir / body["out_relpath"]
        req = ReportRequest(
            engagement_id=ctx.engagement_id,
            weapon_id=body["weapon_id"],
            session_dir=session_dir,
            template=template,
            fired_crumbs=list(body.get("fired_crumbs") or []),
            out_path=out_path,
        )
        report, diagnostics = render_report(req)
        return {
            "report_id": report.id,
            "findings_included": list(report.findings_included),
            "content_hash": report.content_hash,
            "out_path": str(out_path or (session_dir / "report.md")),
            "section_diagnostics": [
                {"name": d.name, "ok": d.ok, "skipped": d.skipped,
                 "error": d.error}
                for d in diagnostics
            ],
        }


registry.register_class(ReportRenderChannel)


def install_report_channel(*, sessions_root: Path) -> Channel:
    """Idempotent: returns the already-registered instance if present."""
    if registry.has("report.render"):
        return registry.get("report.render")
    ch = ReportRenderChannel(sessions_root=sessions_root)
    registry.register(ch)
    return ch
