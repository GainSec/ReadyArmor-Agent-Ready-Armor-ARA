"""Operator-facing TOD (time-of-day) modal page.

Per spec ``armor-trim-v0.1-amendment.md`` §7. When an engagement's
TOD window closes mid-test, the supervisor blocks new ``oc:*`` calls
and surfaces a 4-way modal to the operator:

1. **Stop until window opens** — agent suspends; auto-resume next window.
2. **Continue for TTL** — operator-granted extension; full posture.
3. **Pivot to artifact review** — read-only on engagement mats; no
   active testing.
4. **Decide with agent** — interactive; per-call approval.

This module renders the modal, lists per-engagement TOD state, and
handles POST grants.

Pages:

- ``GET  /tod``                       — index of engagements + state.
- ``GET  /tod/<engagement_id>``       — per-engagement modal page.
- ``POST /tod/<engagement_id>/grant`` — grant a decision.
"""
from __future__ import annotations

import html
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .. import trims as trims_mod
from ..runtime import tod as tod_mod
from . import views as base_views


def e(s: object) -> str:
    return html.escape(str(s)) if s is not None else ""


_STATE_LABELS = {
    "in_window":      ("in window", "var(--accent)"),
    "blocked":        ("blocked", "var(--error)"),
    "extended":       ("extended", "var(--warn)"),
    "pivot_readonly": ("pivot · read-only", "var(--warn)"),
}


# ---------------------------------------------------------------------------
# Helpers — discover engagements with TOD windows
# ---------------------------------------------------------------------------

def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _list_engagements_with_tod(state_root: Path) -> list[dict]:
    """Walk every applied trim and surface engagements that declare
    a TOD window. Returns rows with engagement_id, armor, trim_id,
    tod_allowed, current state."""
    out: list[dict] = []
    seen: set[str] = set()
    for trim in trims_mod.list_all_trims(state_root):
        eid = trim.engagement_id
        if not eid or eid in seen:
            continue
        if not trim.tod_allowed:
            continue
        seen.add(eid)
        st = tod_mod.load_state(state_root, eid)
        in_win = tod_mod.in_any_window(trim.tod_allowed, _now_epoch())
        next_open = tod_mod.next_window_open(trim.tod_allowed, _now_epoch())
        out.append({
            "engagement_id": eid,
            "armor": trim.armor,
            "trim_id": trim.trim_id,
            "current_state": st.current_state,
            "active_decision": st.active_decision,
            "in_window_now": in_win,
            "next_open_epoch": next_open,
            "tod_allowed": trim.tod_allowed,
        })
    return out


def _fmt_epoch(epoch: float | None) -> str:
    if epoch is None:
        return "—"
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def render_tod_index(
    state_root: Path, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    rows = _list_engagements_with_tod(state_root)
    parts: list[str] = [
        '<section>',
        '<div class="ara-eyebrow">Tasks</div>',
        '<h2 class="ara-h" style="margin-bottom:4px">Time-of-day windows</h2>',
        '<p class="ara-sub">Tasks that declared <code>tod_allowed</code>'
        ' windows. When current time falls outside a window, the agent halts'
        ' and the operator decides: stop, extend, pivot to read-only, or '
        'interactive per-call.</p>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};'
            f'margin:8px 0 16px">{e(flash)}</div>'
        )
    if not rows:
        parts.append(
            '<div class="ara-empty">No TOD-restricted tasks active. '
            'Add a window in the trim wizard under '
            '<a href="/trims/derive?t=' + e(token) + '">Derive trim</a>.</div>'
        )
        parts.append('</section>')
        return base_views._page("TOD", "tod", "".join(parts), token)

    parts.append(
        '<table class="ara-table" style="width:100%;font-size:12px">'
        '<thead><tr>'
        '<th>Task</th><th>Armor / Trim</th>'
        '<th>State</th><th>In window?</th>'
        '<th>Next open</th><th>Active decision</th><th></th>'
        '</tr></thead><tbody>'
    )
    for r in rows:
        label, color = _STATE_LABELS.get(
            r["current_state"], (r["current_state"], "var(--muted)"),
        )
        in_win = (
            '<span style="color:var(--accent)">yes</span>'
            if r["in_window_now"] else
            '<span style="color:var(--error)">no</span>'
        )
        ad = r["active_decision"]
        active_str = (
            f'<code>{e(ad.decision)}</code>'
            + (
                f' <span class="ara-sub">(expires {e(ad.expires_at)})</span>'
                if ad.expires_at else ""
            )
        ) if ad is not None else "—"
        encoded = urllib.parse.quote(r["engagement_id"], safe="")
        parts.append(
            f'<tr>'
            f'<td><code>{e(r["engagement_id"])}</code></td>'
            f'<td><code>{e(r["armor"])}</code> / '
            f'<code>{e(r["trim_id"])}</code></td>'
            f'<td><span style="color:{color}">{e(label)}</span></td>'
            f'<td>{in_win}</td>'
            f'<td style="font-size:11px">'
            f'{e(_fmt_epoch(r["next_open_epoch"]))}</td>'
            f'<td>{active_str}</td>'
            f'<td><a class="ara-btn ara-btn-ghost" '
            f'style="padding:2px 8px;font-size:11px" '
            f'href="/tod/{encoded}?t={e(token)}">open</a></td>'
            f'</tr>'
        )
    parts.append('</tbody></table>')
    parts.append('</section>')
    return base_views._page("TOD", "tod", "".join(parts), token)


# ---------------------------------------------------------------------------
# Per-engagement modal page
# ---------------------------------------------------------------------------

def render_tod_modal(
    state_root: Path, engagement_id: str, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    # Find the trim for context.
    matching_trim = None
    for trim in trims_mod.list_all_trims(state_root):
        if trim.engagement_id == engagement_id and trim.tod_allowed:
            matching_trim = trim
            break

    if matching_trim is None:
        body = (
            f'<section><h2 class="ara-h">No TOD-restricted trim found</h2>'
            f'<p class="ara-sub">Task <code>{e(engagement_id)}</code>'
            f' has no applied trim with a declared <code>tod_allowed</code>'
            f' window. <a href="/tod?t={e(token)}">Back</a>.</p></section>'
        )
        return base_views._page("TOD", "tod", body, token)

    state = tod_mod.load_state(state_root, engagement_id)
    in_win = tod_mod.in_any_window(matching_trim.tod_allowed, _now_epoch())
    next_open = tod_mod.next_window_open(
        matching_trim.tod_allowed, _now_epoch(),
    )
    encoded = urllib.parse.quote(engagement_id, safe="")
    label, color = _STATE_LABELS.get(
        state.current_state, (state.current_state, "var(--muted)"),
    )

    parts: list[str] = [
        '<section>',
        '<div class="ara-eyebrow">'
        f'TOD · <code>{e(matching_trim.armor)}</code> / '
        f'<code>{e(matching_trim.trim_id)}</code></div>',
        f'<h2 class="ara-h" style="margin-bottom:4px">{e(engagement_id)}</h2>',
        f'<p class="ara-sub">Current state: '
        f'<span style="color:{color}">{e(label)}</span> · '
        f'In window now: '
        + ('<b style="color:var(--accent)">yes</b>' if in_win
           else '<b style="color:var(--error)">no</b>')
        + (f' · Next open: {e(_fmt_epoch(next_open))}' if next_open else '')
        + '</p>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};'
            f'margin:8px 0 16px">{e(flash)}</div>'
        )

    # Configured windows
    parts.append('<details open><summary>Configured windows</summary>')
    parts.append('<ul>')
    for w in matching_trim.tod_allowed:
        days = "/".join(w.get("days") or [])
        tz = w.get("tz") or "UTC"
        parts.append(
            f'<li><code>{e(days)}</code>'
            f' &nbsp; {e(w.get("start") or "?")} – {e(w.get("end") or "?")}'
            f' &nbsp; <span class="ara-sub">{e(tz)}</span></li>'
        )
    parts.append('</ul></details>')

    # Active decision (if any)
    if state.active_decision is not None:
        ad = state.active_decision
        parts.append(
            '<details open style="margin-top:16px">'
            '<summary>Active decision</summary>'
        )
        parts.append(
            '<table class="ara-table" style="font-size:12px">'
            f'<tr><th>Decision</th><td><code>{e(ad.decision)}</code></td></tr>'
            f'<tr><th>Granted at</th><td>{e(ad.granted_at)}</td></tr>'
            f'<tr><th>Granted by</th><td>{e(ad.granted_by) or "—"}</td></tr>'
            f'<tr><th>Expires at</th><td>{e(ad.expires_at) or "—"}</td></tr>'
            f'<tr><th>Reason</th><td>{e(ad.reason) or "—"}</td></tr>'
            '</table>'
        )
        parts.append('</details>')

    # ── Decision form (4-way modal) ──────────────────────────────
    parts.append(
        '<h3 style="margin-top:24px">Operator decision</h3>'
        '<p class="ara-sub">Pick one of the four options. Stop and '
        'interactive don\'t need a TTL; extend and pivot do.</p>'
    )
    parts.append(
        f'<form class="ara-form" method="post" '
        f'action="/tod/{encoded}/grant?t={e(token)}" '
        'style="display:grid;gap:16px;max-width:640px">'
    )
    # Decision radios
    radio_rows = [
        (tod_mod.DECISION_STOP, "Stop until next window",
         "Agent suspends. Auto-resume at next window-open."),
        (tod_mod.DECISION_EXTEND, "Continue for TTL",
         "Run at full posture for the TTL below; auto-stop at expiry."),
        (tod_mod.DECISION_PIVOT, "Pivot to artifact review (read-only)",
         "Strip active testing; allow oc:read on task mats only."),
        (tod_mod.DECISION_INTERACTIVE, "Decide with agent",
         "Each tool call needs explicit operator approval."),
    ]
    for value, head, desc in radio_rows:
        parts.append(
            '<label style="display:block;padding:8px;'
            'border:1px solid var(--border);border-radius:4px">'
            f'<input type="radio" name="decision" value="{value}" required> '
            f'<b>{e(head)}</b><br>'
            f'<span class="ara-sub">{e(desc)}</span></label>'
        )
    # TTL field (only used for extend/pivot)
    parts.append(
        '<label>TTL (seconds — only used for extend / pivot)'
        '<input type="number" name="ttl_seconds" min="0" step="60" '
        'placeholder="3600 (1h)"></label>'
    )
    # Granted-by + reason
    parts.append(
        '<label>granted by'
        '<input type="text" name="granted_by" placeholder="ops@acme.com"></label>'
    )
    parts.append(
        '<label>reason'
        '<input type="text" name="reason" '
        'placeholder="active exploit chain mid-test"></label>'
    )
    parts.append(
        '<div style="display:flex;gap:8px">'
        '<button class="ara-btn ara-btn-primary" type="submit">'
        'Grant decision</button>'
        f'<a class="ara-btn ara-btn-ghost" href="/tod?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        'Back to list</a>'
        '</div>'
    )
    parts.append('</form>')

    # History
    if state.history:
        parts.append(
            '<details style="margin-top:24px">'
            '<summary>Decision history</summary>'
        )
        parts.append(
            '<pre style="background:var(--surface);padding:12px;'
            'border-radius:4px;overflow:auto;font-size:11px">'
            f'{e(json.dumps(state.history, indent=2))}</pre>'
        )
        parts.append('</details>')

    parts.append('</section>')
    return base_views._page(
        f"TOD · {engagement_id}", "tod", "".join(parts), token,
    )


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def handle_tod_grant(
    state_root: Path, engagement_id: str, body: dict, token: str,
) -> str:
    """Returns a redirect Location."""
    encoded = urllib.parse.quote(engagement_id, safe="")
    decision = (body.get("decision") or "").strip()
    if decision not in tod_mod.VALID_DECISIONS:
        return (
            f"/tod/{encoded}?t={token}"
            f"&flash={urllib.parse.quote('pick a decision')}"
            f"&flash_level=bad"
        )
    ttl_raw = (body.get("ttl_seconds") or "").strip()
    ttl: int | None = None
    if ttl_raw:
        try:
            ttl = int(ttl_raw)
            if ttl <= 0:
                ttl = None
        except ValueError:
            ttl = None
    granted_by = body.get("granted_by") or ""
    reason = body.get("reason") or ""
    try:
        tod_mod.grant_decision(
            state_root, engagement_id, decision,
            granted_by=granted_by, ttl_seconds=ttl, reason=reason,
        )
    except ValueError as exc:
        return (
            f"/tod/{encoded}?t={token}"
            f"&flash={urllib.parse.quote(str(exc))}"
            f"&flash_level=bad"
        )
    return (
        f"/tod/{encoded}?t={token}"
        f"&flash={urllib.parse.quote(f'granted: {decision}')}"
    )


__all__ = [
    "render_tod_index", "render_tod_modal", "handle_tod_grant",
]
