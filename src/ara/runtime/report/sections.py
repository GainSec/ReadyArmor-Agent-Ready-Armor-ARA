"""Built-in section auto-generators.

Each takes the pre-built rendering context + the engagement state and
returns markdown. They run at render time and produce strings the Kiln
splices into the final document.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable


def _severity_breakdown(findings: list[dict]) -> Counter[str]:
    return Counter(
        (f.get("severity_name") or "info") for f in findings
    )


def gen_executive_summary(ctx: dict, **_: object) -> str:
    findings = ctx.get("findings") or []
    if not findings:
        return (
            "No findings were signed off in this engagement. "
            "See the coverage section for omissions and the appendix "
            "for evidence captured.\n\n"
        )
    breakdown = _severity_breakdown(findings)
    counts = (
        f"{breakdown.get('critical', 0)} critical, "
        f"{breakdown.get('high', 0)} high, "
        f"{breakdown.get('medium', 0)} medium, "
        f"{breakdown.get('low', 0)} low, "
        f"{breakdown.get('info', 0)} info"
    )
    top_categories = sorted(
        Counter(f.get("category", "uncategorized") for f in findings).items(),
        key=lambda kv: -kv[1],
    )[:5]
    cats = ", ".join(f"`{c}` ({n})" for c, n in top_categories)
    asset_count = len({
        a for f in findings for a in (f.get("affected_assets") or [])
    })
    return (
        f"This engagement produced **{len(findings)} signed-off finding(s)**: "
        f"{counts}.\n\n"
        f"Top categories observed: {cats}.\n\n"
        f"Affected assets touched: {asset_count}.\n\n"
    )


def gen_methodology(ctx: dict, **_: object) -> str:
    fired_crumbs = ctx.get("fired_crumbs") or []
    if not fired_crumbs:
        return "(Methodology telemetry not available for this engagement.)\n\n"
    lines = ["The agent ran the following crumbs during this engagement:\n"]
    for c in fired_crumbs:
        cid = c.get("crumb_id", "?")
        name = c.get("name", "")
        lines.append(f"- `{cid}` — {name}")
    return "\n".join(lines) + "\n\n"


def gen_coverage_omissions(ctx: dict, **_: object) -> str:
    pending = ctx.get("pending_leads") or []
    archived = ctx.get("archived_leads") or []
    parts: list[str] = []
    if pending:
        parts.append(
            f"Open leads at engagement close: **{len(pending)}**"
        )
        for ld in pending[:10]:
            parts.append(f"- `{ld.get('id', '?')}` — {ld.get('title', '')}")
        if len(pending) > 10:
            parts.append(f"- ... and {len(pending) - 10} more")
        parts.append("")
    if archived:
        parts.append(
            f"Archived (aged-out / engagement-closed) leads: "
            f"**{len(archived)}**"
        )
        parts.append("")
    if not parts:
        return "No outstanding leads at engagement close.\n\n"
    return "\n".join(parts) + "\n"


def gen_appendix_evidence(ctx: dict, **_: object) -> str:
    evidence = ctx.get("evidence_index") or []
    if not evidence:
        return "(No evidence captured.)\n\n"
    lines = [
        "| ID | Source | Captured | Summary |",
        "|---|---|---|---|",
    ]
    for ev in evidence[:50]:
        # `\` inside an f-string expression is a syntax error on
        # Python 3.9-3.11; extract before formatting.
        summary_safe = ev.get('summary', '').replace('|', '\\|')
        lines.append(
            f"| `{ev.get('id', '?')}` | "
            f"`{ev.get('source', '?')}` | "
            f"{ev.get('captured_at_iso', '')} | "
            f"{summary_safe} |"
        )
    if len(evidence) > 50:
        lines.append(f"\n_(... {len(evidence) - 50} more evidence rows omitted)_")
    return "\n".join(lines) + "\n\n"


_GENERATORS: dict[str, Callable[..., str]] = {
    "executive_summary": gen_executive_summary,
    "methodology": gen_methodology,
    "coverage_omissions": gen_coverage_omissions,
    "appendix_evidence": gen_appendix_evidence,
}


def get_generator(name: str) -> Callable[..., str] | None:
    return _GENERATORS.get(name)
