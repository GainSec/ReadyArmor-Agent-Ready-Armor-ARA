"""Server-rendered views for the tool classification registry.

Per spec ``armor-trim-v0.1-amendment.md`` §11. Surfaces:

- ``GET  /tools``                           — registry table.
- ``GET  /tools/<key>``                     — entry detail + audit.
- ``POST /tools/<key>/approve``             — confirm classification.
- ``POST /tools/<key>/override``            — operator edits classification.
- ``POST /tools/<key>/reclassify``          — flag for re-run on next call.
- ``POST /tools/<key>/delete``              — remove a stale entry.

Keys land in the URL path encoded; the route extracts the raw form
(``<tool> | <flag_pattern>``) and looks up by exact match.
"""
from __future__ import annotations

import html
import json
import urllib.parse
from pathlib import Path

from .. import tool_registry as tr_mod
from . import views as base_views


def e(s: object) -> str:
    return html.escape(str(s)) if s is not None else ""


_STATUS_LABELS = {
    tr_mod.STATUS_PENDING_FIRST_USE: ("blocked", "var(--error)"),
    tr_mod.STATUS_PENDING_REVIEW:    ("pending", "var(--warn)"),
    tr_mod.STATUS_CONFIRMED:         ("confirmed", "var(--accent)"),
    tr_mod.STATUS_OPERATOR_OVERRIDDEN: ("overridden", "var(--warn)"),
    tr_mod.STATUS_MERGED_BY_TTL:     ("merged (no review)", "var(--muted)"),
}


def _flag_pills(c: tr_mod.Classification) -> str:
    bits: list[str] = []
    for label, on in [
        ("active", c.active), ("destructive", c.destructive),
        ("intrusive", c.intrusive), ("scanner", c.scanner),
    ]:
        if on:
            bits.append(
                f'<span class="ara-pill" style="margin-right:4px">{e(label)}</span>'
            )
    return "".join(bits) or '<span class="ara-sub">passive</span>'


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def render_tools_index(
    state_root: Path, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
    filter_status: str | None = None,
) -> str:
    rows = tr_mod.list_entries(state_root)
    if filter_status:
        rows = [r for r in rows if r.status == filter_status]

    parts: list[str] = [
        '<section>',
        '<div class="ara-eyebrow">Task</div>',
        '<h2 class="ara-h" style="margin-bottom:4px">Tools</h2>',
        '<p class="ara-sub">Tool classifications built up by the agent on '
        'first use. Operator can approve, edit, or flag for reclassify. '
        'No baseline list ships — this grows organically.</p>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};'
            f'margin:8px 0 16px">{e(flash)}</div>'
        )

    # Filter strip
    parts.append('<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">')
    parts.append(
        f'<a class="ara-btn ara-btn-ghost" href="/tools?t={e(token)}" '
        f'style="text-decoration:none">all</a>'
    )
    for status_key, (label, _) in _STATUS_LABELS.items():
        sel_style = (
            ";border-color:var(--accent)"
            if filter_status == status_key else ""
        )
        parts.append(
            f'<a class="ara-btn ara-btn-ghost" '
            f'href="/tools?t={e(token)}&status={urllib.parse.quote(status_key)}" '
            f'style="text-decoration:none{sel_style}">{e(label)}</a>'
        )
    parts.append('</div>')

    if not rows:
        parts.append(
            '<div class="ara-empty">No classifications yet. Entries are '
            'created the first time the agent fires a tool whose '
            '<code>(name, flag-pattern)</code> isn\'t in the registry.</div>'
        )
        parts.append('</section>')
        return base_views._page("Tools", "tools", "".join(parts), token)

    parts.append(
        '<table class="ara-table" style="width:100%;font-size:12px">'
        '<thead><tr>'
        '<th>Tool</th><th>Flags</th><th>Classification</th>'
        '<th>Conf.</th><th>Status</th><th>Uses</th><th>First seen</th>'
        '<th></th></tr></thead><tbody>'
    )
    for r in rows:
        label, color = _STATUS_LABELS.get(r.status, (r.status, "var(--muted)"))
        first_seen_bits = []
        if r.first_seen_engagement:
            first_seen_bits.append(f"task=<code>{e(r.first_seen_engagement)}</code>")
        if r.first_seen_weapon:
            first_seen_bits.append(f"weapon=<code>{e(r.first_seen_weapon)}</code>")
        first_seen = " ".join(first_seen_bits) or "—"
        eff = r.effective_classification
        override_badge = (
            ' <span class="ara-pill" style="background:var(--warn)">override</span>'
            if r.operator_override is not None else ""
        )
        reclassify_badge = (
            ' <span class="ara-pill" style="background:var(--accent)">reclassify</span>'
            if r.reclassify_on_next_call else ""
        )
        encoded = urllib.parse.quote(r.key, safe="")
        parts.append(
            f'<tr>'
            f'<td><code>{e(r.tool)}</code></td>'
            f'<td><code>{e(r.flag_pattern)}</code></td>'
            f'<td>{_flag_pills(eff)}{override_badge}{reclassify_badge}</td>'
            f'<td>{r.confidence}/3</td>'
            f'<td><span style="color:{color}">{e(label)}</span></td>'
            f'<td>{r.use_count}</td>'
            f'<td style="font-size:11px">{first_seen}</td>'
            f'<td><a class="ara-btn ara-btn-ghost" '
            f'style="padding:2px 8px;font-size:11px" '
            f'href="/tools/{encoded}?t={e(token)}">open</a></td>'
            f'</tr>'
        )
    parts.append('</tbody></table>')
    parts.append('</section>')
    return base_views._page("Tools", "tools", "".join(parts), token)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def render_tool_detail(
    state_root: Path, key: str, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    entry = tr_mod.lookup(state_root, key)
    if entry is None:
        body = (
            f'<section><h2 class="ara-h">Tool entry not found</h2>'
            f'<p class="ara-sub">No registry entry for <code>{e(key)}</code>. '
            f'<a href="/tools?t={e(token)}">Back to list</a>.</p></section>'
        )
        return base_views._page("Tool", "tools", body, token)

    label, color = _STATUS_LABELS.get(entry.status, (entry.status, "var(--muted)"))
    encoded = urllib.parse.quote(key, safe="")
    parts: list[str] = [
        '<section>',
        '<div class="ara-eyebrow">Tool entry</div>',
        f'<h2 class="ara-h" style="margin-bottom:4px">'
        f'<code>{e(entry.tool)}</code> '
        f'<span style="color:var(--muted)">| {e(entry.flag_pattern)}</span></h2>',
        f'<p class="ara-sub">Status: <span style="color:{color}">{e(label)}</span> · '
        f'Confidence {entry.confidence}/3 · '
        f'Classified by {e(entry.classified_by)} at {e(entry.classified_at)}</p>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};'
            f'margin:8px 0 16px">{e(flash)}</div>'
        )

    # Action buttons
    parts.append('<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">')
    if entry.status != tr_mod.STATUS_CONFIRMED:
        parts.append(
            f'<form method="post" action="/tools/{encoded}/approve?t={e(token)}" '
            f'style="margin:0">'
            f'<button class="ara-btn ara-btn-primary" type="submit">Approve</button>'
            f'</form>'
        )
    parts.append(
        f'<form method="post" action="/tools/{encoded}/reclassify?t={e(token)}" '
        f'style="margin:0">'
        f'<button class="ara-btn ara-btn-ghost" type="submit">'
        f'Flag for reclassify</button></form>'
    )
    parts.append(
        f'<form method="post" action="/tools/{encoded}/delete?t={e(token)}" '
        f'style="margin:0" '
        f'onsubmit="return confirm(\'Delete entry for {e(key)}?\');">'
        f'<button class="ara-btn ara-btn-ghost" type="submit" '
        f'style="border-color:var(--error);color:var(--error)">Delete</button>'
        f'</form>'
    )
    parts.append(
        f'<a class="ara-btn ara-btn-ghost" href="/tools?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        '← Back</a>'
    )
    parts.append('</div>')

    # Classification + reasoning
    eff = entry.effective_classification
    parts.append('<details open><summary>Effective classification</summary>')
    parts.append(f'<div style="padding:8px 0">{_flag_pills(eff)}</div>')
    parts.append(
        f'<p class="ara-sub">{e(entry.reasoning)}</p>'
    )
    parts.append('</details>')

    # Operator override (if any)
    if entry.operator_override is not None:
        ov = entry.operator_override
        parts.append('<details open style="margin-top:16px">'
                     '<summary>Operator override</summary>')
        parts.append(f'<div style="padding:8px 0">{_flag_pills(ov.classification)}</div>')
        parts.append(
            f'<p class="ara-sub">{e(ov.reason)} '
            f'<span style="color:var(--muted)">({e(ov.ts)})</span></p>'
        )
        parts.append('</details>')

    # Override form (operator authors a new classification inline)
    parts.append('<details style="margin-top:16px"><summary>Edit classification</summary>')
    parts.append(
        f'<form class="ara-form" method="post" '
        f'action="/tools/{encoded}/override?t={e(token)}">'
    )
    for flag in ("active", "destructive", "intrusive", "scanner"):
        checked = " checked" if getattr(eff, flag) else ""
        parts.append(
            f'<label style="display:inline-flex;gap:4px;margin-right:12px">'
            f'<input type="checkbox" name="{flag}" value="1"{checked}> '
            f'{e(flag)}</label>'
        )
    parts.append(
        '<label style="display:block;margin-top:8px">reason'
        '<input type="text" name="reason" placeholder="why this differs" required>'
        '</label>'
    )
    parts.append(
        '<button class="ara-btn ara-btn-primary" type="submit">'
        'Save override</button>'
    )
    parts.append('</form></details>')

    # Engagements used + raw record
    if entry.engagements_used:
        parts.append('<details style="margin-top:16px"><summary>Tasks that used this tool</summary>')
        parts.append('<ul>')
        for eid in entry.engagements_used:
            parts.append(f'<li><code>{e(eid)}</code></li>')
        parts.append('</ul></details>')

    parts.append('<details style="margin-top:16px"><summary>Raw record</summary>')
    parts.append(
        '<pre style="background:var(--surface);padding:12px;'
        'border-radius:4px;overflow:auto;font-size:11px">'
        f'{e(json.dumps(entry.to_dict(), indent=2, sort_keys=True))}</pre>'
    )
    parts.append('</details>')

    parts.append('</section>')
    return base_views._page(
        f"Tool · {entry.tool}", "tools", "".join(parts), token,
    )


# ---------------------------------------------------------------------------
# POST handlers (return redirect URL)
# ---------------------------------------------------------------------------

def _back(key: str, token: str, *, flash: str | None = None,
          flash_level: str = "info") -> str:
    encoded = urllib.parse.quote(key, safe="")
    out = f"/tools/{encoded}?t={token}"
    if flash:
        out += f"&flash={urllib.parse.quote(flash)}"
    if flash_level != "info":
        out += f"&flash_level={flash_level}"
    return out


def handle_tool_approve(state_root: Path, key: str, token: str) -> str:
    res = tr_mod.operator_approve(state_root, key)
    if res is None:
        return _back(key, token, flash="entry not found", flash_level="bad")
    return _back(key, token, flash="approved")


def handle_tool_override(
    state_root: Path, key: str, body: dict, token: str,
) -> str:
    classification = tr_mod.Classification(
        active=bool(body.get("active")),
        destructive=bool(body.get("destructive")),
        intrusive=bool(body.get("intrusive")),
        scanner=bool(body.get("scanner")),
    )
    reason = (body.get("reason") or "").strip()
    if not reason:
        return _back(key, token, flash="reason required", flash_level="bad")
    res = tr_mod.operator_override(
        state_root, key, classification=classification, reason=reason,
    )
    if res is None:
        return _back(key, token, flash="entry not found", flash_level="bad")
    return _back(key, token, flash="override saved")


def handle_tool_reclassify(state_root: Path, key: str, token: str) -> str:
    res = tr_mod.operator_request_reclassify(state_root, key)
    if res is None:
        return _back(key, token, flash="entry not found", flash_level="bad")
    return _back(key, token, flash="will reclassify on next call")


def handle_tool_delete(state_root: Path, key: str, token: str) -> str:
    gone = tr_mod.delete_entry(state_root, key)
    if not gone:
        return _back(key, token, flash="entry not found", flash_level="bad")
    return f"/tools?t={token}&flash={urllib.parse.quote('entry deleted')}"
