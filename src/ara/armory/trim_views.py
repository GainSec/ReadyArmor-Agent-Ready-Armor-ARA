"""Server-rendered views for the trim management wizard.

Implements spec ``armor-trim-v0.md`` §16 — "Trim should be UI from v1".
Operator inspiration from BRA scope intake / ROE / governance tokens.

Pages:

- ``GET  /trims``                        — cross-armor list (Q2: armory
  entry gate lists all trims, current armor as filter).
- ``GET  /trims/new``                    — manual create form.
- ``POST /trims/new``                    — create + redirect to detail.
- ``GET  /trims/derive``                 — engagement-intake wizard.
- ``POST /trims/derive``                 — derive + (optional) apply.
- ``GET  /trims/<armor>/<id>``           — detail (meta + previews).
- ``POST /trims/<armor>/<id>/apply``     — apply overlays.
- ``POST /trims/<armor>/<id>/release``   — strip overlays.
- ``POST /trims/<armor>/<id>/delete``    — delete (must not be applied).
"""
from __future__ import annotations

import html
import json
import urllib.parse
from pathlib import Path

from .. import armors as armors_mod
from .. import trims as trims_mod
from . import views as base_views
from .views import _render_armor_info_card, _render_loadout_card, _static_css_href


def e(s: object) -> str:
    return html.escape(str(s)) if s is not None else ""


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def render_trims_index(
    state_root: Path, token: str, *,
    armor_filter: str | None = None,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    armors_list = armors_mod.list_armors(state_root)
    rows = trims_mod.list_all_trims(state_root)
    if armor_filter:
        rows = [t for t in rows if t.armor == armor_filter]

    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section>',
        '<div class="ara-eyebrow">Tasks</div>',
        '<h2 class="ara-h" style="margin-bottom:4px">Armor Trims</h2>',
        '<p class="ara-sub">Task-scoped overlays that pin scope, '
        'OOS, ROE, test window, and operator prefs onto an equipped armor. '
        'Auto-released when the parent armor is unequipped.</p>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};'
            f'margin:8px 0 16px">{e(flash)}</div>'
        )

    # Filter strip + create buttons
    parts.append(
        '<div style="display:flex;gap:12px;align-items:center;'
        'margin-bottom:16px;flex-wrap:wrap">'
    )
    parts.append(
        f'<form method="get" action="/trims" '
        f'style="display:inline-flex;gap:8px;align-items:center;margin:0">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        f'<label style="font-size:12px;color:var(--muted)">Filter armor:</label>'
        f'<select name="armor" onchange="this.form.submit()">'
        f'<option value="">— all —</option>'
    )
    for a in armors_list:
        sel = " selected" if a.name == armor_filter else ""
        parts.append(f'<option value="{e(a.name)}"{sel}>{e(a.name)}</option>')
    parts.append('</select></form>')

    parts.append(
        f'<a class="ara-btn ara-btn-primary" '
        f'href="/trims/derive?t={e(token)}" '
        f'style="text-decoration:none;display:inline-flex;align-items:center">'
        f'+ Create from task</a>'
    )
    parts.append(
        f'<a class="ara-btn ara-btn-ghost" '
        f'href="/trims/new?t={e(token)}" '
        f'style="text-decoration:none;display:inline-flex;align-items:center">'
        f'+ New (manual)</a>'
    )
    parts.append('</div>')

    if not rows:
        parts.append(
            '<div class="ara-empty">No trims yet. '
            '“Create from task” spins one up from a scope/task-rules form '
            '(no LLM); “New (manual)” gives you an empty trim to edit.</div>'
        )
        parts.append('</section>')
        return base_views._page("Trims", "trims", "".join(parts), token)

    def _is_active(t) -> bool:
        return trims_mod.active_trim_id(state_root, t.armor) == t.trim_id

    hero = next((t for t in rows if _is_active(t)), rows[0])
    rest = [t for t in rows if not (t.armor == hero.armor and t.trim_id == hero.trim_id)]
    is_active = _is_active(hero)
    bw_path = trims_mod.trim_bulwark_section_path(state_root, hero.armor, hero.trim_id)
    abs_path = trims_mod.trim_absolute_path(state_root, hero.armor, hero.trim_id)
    bw_body = bw_path.read_text() if bw_path.is_file() else ""
    abs_body = abs_path.read_text() if abs_path.is_file() else ""
    src = "engagement" if hero.derived_from else "manual"
    action = (
        f'<form method="post" action="/trims/{e(hero.armor)}/{e(hero.trim_id)}/release?t={e(token)}" style="margin:0">'
        '<button class="ara-btn ara-btn-primary" type="submit">Release</button></form>'
        if is_active else
        f'<form method="post" action="/trims/{e(hero.armor)}/{e(hero.trim_id)}/apply?t={e(token)}" style="margin:0">'
        '<button class="ara-btn ara-btn-primary" type="submit">Apply</button></form>'
    )
    parts.append(
        '<section class="ara-armor-focus ara-trim-focus" aria-label="Active trim">'
        '<div class="ara-armor-focus-card">'
        '<div>'
        '<div class="ara-eyebrow">Active trim</div>'
        + _render_loadout_card(
            "trim", "Trim", hero.trim_id,
            hero.target or hero.engagement_id or "Task overlay.",
            f"/trims/{e(hero.armor)}/{e(hero.trim_id)}", token, glyph="T",
            meta_rows=[
                ("env", hero.env or "nonprod"),
                ("depth", hero.depth or "recon"),
                ("state", "applied" if is_active else "standby"),
            ],
        )
        + '</div>'
        '<div class="ara-armor-focus-copy">'
        '<span class="ara-eyebrow">Task overlay</span>'
        f'<h3>{e(hero.trim_id)}</h3>'
        '<p>Trims pin the where, when, and task rules onto a '
        'parent armor without replacing that armor.</p>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">'
        + action
        + f'<a class="ara-btn ara-btn-ghost" href="/trims/{e(hero.armor)}/{e(hero.trim_id)}?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">View</a>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="ara-armor-info-grid">'
        + _render_armor_info_card(
            "Scope",
            hero.env or "nonprod",
            "Environment, target, and parent armor binding.",
            [
                ("armor", f'<code>{e(hero.armor)}</code>'),
                ("target", f'<code>{e(hero.target or "—")}</code>'),
                ("source", e(src)),
            ],
            glyph="S",
        )
        + _render_armor_info_card(
            "ROE",
            hero.depth or "recon",
            "Testing posture, active flag, and declared tokens.",
            [
                ("posture", e(hero.posture or "blackbox")),
                ("active", "yes" if hero.active_testing else "no"),
                ("tokens", str(len(hero.declared_tokens))),
            ],
            glyph="R",
        )
        + _render_armor_info_card(
            "Envelope",
            f"{len(abs_body.splitlines())} lines",
            "Absolute and Bulwark fragments applied by this trim.",
            [
                ("bulwark", f"{len(bw_body):,} chars"),
                ("absolute", f"{len(abs_body):,} chars"),
                ("channels", str(len(hero.channels))),
            ],
            glyph="E",
        )
        + '</div>'
        '<details class="ara-armor-detail-inline">'
        '<summary><span class="ara-eyebrow">Detail</span>'
        '<strong>View trim internals</strong></summary>'
        f'<pre class="ara-code">{e(json.dumps(hero.to_dict(), indent=2, sort_keys=True))}</pre>'
        '<details><summary>Bulwark section</summary>'
        f'<pre class="ara-code">{e(bw_body) or "(empty)"}</pre></details>'
        '<details><summary>Absolute trim</summary>'
        f'<pre class="ara-code">{e(abs_body) or "(empty)"}</pre></details>'
        '</details>'
        '</section>'
    )
    if rest:
        parts.append('<div class="ara-eyebrow" style="margin-top:32px">Other trims</div>')
        parts.append(
            '<div class="ara-kit-grid" style="margin-top:8px">'
            + "".join(
                _render_loadout_card(
                    "trim", "Trim", t.trim_id,
                    t.target or t.engagement_id or "Task overlay.",
                    f"/trims/{e(t.armor)}/{e(t.trim_id)}", token, glyph="T",
                    meta_rows=[
                        ("armor", t.armor),
                        ("env", t.env or "nonprod"),
                        ("state", "applied" if _is_active(t) else "standby"),
                    ],
                )
                for t in rest
            )
            + '</div>'
        )
    parts.append('</section>')
    return base_views._page("Trims", "trims", "".join(parts), token)


# ---------------------------------------------------------------------------
# Manual new
# ---------------------------------------------------------------------------

def render_trim_new(
    state_root: Path, token: str, *,
    flash: str | None = None,
) -> str:
    armors_list = armors_mod.list_armors(state_root)
    if not armors_list:
        body = (
            '<section><h2 class="ara-h">New Trim</h2>'
            '<p class="ara-sub">No armors yet — '
            f'<a href="/armors/new?t={e(token)}">create one</a> first.</p>'
            '</section>'
        )
        return base_views._page("New Trim", "sew", body, token)

    armor_options = "".join(
        f'<option value="{e(a.name)}">{e(a.name)}</option>'
        for a in armors_list
    )
    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section>',
        '<div class="ara-eyebrow">New</div>',
        '<h2 class="ara-h">New Trim (manual)</h2>',
        '<p class="ara-sub">Creates an empty trim under the chosen armor. '
        'You\'ll author <code>bulwark-section.md</code> and '
        '<code>absolute-trim.md</code> by hand. For a guided form, use '
        '<a href="/trims/derive?t=' + e(token) + '">Create from task</a>.</p>',
        base_views._render_creation_preview(
            "trim",
            "Create trim preview",
            "new-trim-preview",
            "The saved trim card is generated from the final trim id.",
            glyph="T",
            rows=[("seed", "trim id"), ("state", "draft")],
        ),
    ]
    if flash:
        parts.append(
            '<div class="ara-empty" style="border-left:3px solid var(--error);'
            f'margin-bottom:14px">{e(flash)}</div>'
        )
    parts.extend([
        f'<form class="ara-form" method="post" action="/trims/new?t={e(token)}">',
        f'<label>armor<select name="armor" required>{armor_options}</select></label>',
        '<label>trim id<input type="text" name="trim_id" required '
        'pattern="[a-zA-Z0-9][a-zA-Z0-9_.\\-]{0,63}" '
        'placeholder="acme-portal-2026-04"></label>',
        '<label>target (URL or host, optional)'
        '<input type="text" name="target" placeholder="https://portal.acme.com">'
        '</label>',
        '<label>operator id (optional)'
        '<input type="text" name="operator" placeholder="ops@acme.com"></label>',
        '<label>weapon ref (optional)'
        '<input type="text" name="weapon_ref" placeholder="wstg-flocker-v0"></label>',
        '<label>task id (optional)'
        '<input type="text" name="engagement_id"></label>',
        '<button class="ara-btn ara-btn-primary" type="submit">Create</button>',
        f'<a class="ara-btn ara-btn-ghost" href="/trims?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Cancel</a>',
        '</form>',
        '</section>',
    ])
    return base_views._page("New Trim", "sew", "".join(parts), token)


# ---------------------------------------------------------------------------
# Derive (engagement-intake wizard) — BRA-inspired
# ---------------------------------------------------------------------------

_BUILTIN_CHANNELS = ["http", "tls", "dns", "smtp", "mqtt", "ssh", "rdp"]


def render_trim_derive(
    state_root: Path, token: str, *,
    flash: str | None = None,
    saved: dict | None = None,
) -> str:
    """Task-intake wizard. Per Q4 (back-button approved): values
    survive a re-render so the operator can iterate without losing input.
    The ``saved`` dict carries previously-submitted values."""
    armors_list = armors_mod.list_armors(state_root)
    if not armors_list:
        body = (
            '<section><h2 class="ara-h">Create Trim</h2>'
            '<p class="ara-sub">No armors yet — '
            f'<a href="/armors/new?t={e(token)}">create one</a> first.</p>'
            '</section>'
        )
        return base_views._page("Create Trim", "sew", body, token)

    s = saved or {}

    def v(key: str, default: str = "") -> str:
        return e(s.get(key, default) or "")

    armor_options = "".join(
        f'<option value="{e(a.name)}"'
        f'{" selected" if s.get("armor") == a.name else ""}>{e(a.name)}</option>'
        for a in armors_list
    )

    sel_chans = set((s.get("channels") or "").split(","))
    chan_checkboxes = "".join(
        f'<label style="display:inline-flex;gap:4px;margin-right:12px">'
        f'<input type="checkbox" name="channel_{c}" value="1"'
        f'{" checked" if c in sel_chans else ""}> {e(c)}</label>'
        for c in _BUILTIN_CHANNELS
    )

    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section>',
        '<div class="ara-eyebrow">New</div>',
        '<h2 class="ara-h">Create Trim from Task</h2>',
        '<p class="ara-sub">Create a task trim from scope, out-of-scope '
        'targets, ROE, test windows, MATS posture, and operator controls. '
        'This writes the trim fragments used by the active armor.</p>',
        base_views._render_creation_preview(
            "trim",
            "Create trim preview",
            str(s.get("engagement_id") or s.get("trim_id") or "new-trim-preview"),
            "The saved trim card is generated from task id and trim metadata.",
            glyph="T",
            rows=[
                ("seed", "task"),
                ("armor", str(s.get("armor") or armors_list[0].name)),
                ("state", "draft"),
            ],
        ),
        '<div class="ara-toolbar" style="margin:14px 0 16px">'
        f'<a class="ara-btn ara-btn-ghost" href="/trims?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        'View trims</a>'
        '</div>',
    ]
    if flash:
        parts.append(
            '<div class="ara-empty" style="border-left:3px solid var(--error);'
            f'margin:8px 0 16px">{e(flash)}</div>'
        )
    parts.extend([
        f'<form id="sew-trim-form" class="ara-form" method="post" action="/trims/derive?t={e(token)}" '
        'style="display:grid;grid-template-columns:1fr;gap:16px">',
        # Task basics
        '<details class="sew-form-section"><summary>Task</summary>',
        f'<label>armor<select name="armor" required>{armor_options}</select></label>',
        f'<label>task id<input type="text" name="engagement_id" '
        f'value="{v("engagement_id")}" '
        f'placeholder="acme-portal-2026-04" required></label>',
        f'<label>operator id<input type="text" name="operator_id" '
        f'value="{v("operator_id")}" '
        f'placeholder="ops@acme.com"></label>',
        f'<label>weapon ref (optional)<input type="text" name="weapon_ref" '
        f'value="{v("weapon_ref")}" placeholder="wstg-flocker-v0"></label>',
        '</details>',
        # Target / scope
        '<details class="sew-form-section"><summary>Target &amp; Scope</summary>',
        f'<label>primary target<input type="text" name="target_primary" '
        f'value="{v("target_primary")}" '
        f'placeholder="https://portal.acme.com" required></label>',
        f'<label>scope allows (one host per line)<textarea name="scope_allows" '
        f'rows="4" placeholder="portal.acme.com&#10;api.acme.com">'
        f'{v("scope_allows")}</textarea></label>',
        f'<label>out of scope (one host per line)<textarea name="oos" '
        f'rows="3" placeholder="billing.acme.com">{v("oos")}</textarea></label>',
        '</details>',
        # ── v0.1: Environment (prod / nonprod / mixed) ────────────────
        '<details class="sew-form-section"><summary>Environment</summary>',
        '<label style="display:block">'
        f'<input type="radio" name="env" value="nonprod"'
        f'{" checked" if (s.get("env","nonprod") == "nonprod") else ""}> '
        '<b>Non-prod</b> — staging / QA / dev / isolated lab; destructive'
        ' ops permitted within scope.</label>',
        '<label style="display:block">'
        f'<input type="radio" name="env" value="prod"'
        f'{" checked" if s.get("env") == "prod" else ""}> '
        '<b>Prod</b> — read-only by default; state-changing ops require '
        '<code>PROD-DESTRUCTIVE-APPROVAL</code> per action.</label>',
        '<label style="display:block">'
        f'<input type="radio" name="env" value="mixed"'
        f'{" checked" if s.get("env") == "mixed" else ""}> '
        '<b>Mixed</b> — classify per host below.</label>',
        '<label>per-host classification (only used when env=mixed; '
        'one per line as <code>host:env</code>)<textarea name="assets" '
        f'rows="3" placeholder="portal.acme.com:prod&#10;'
        f'staging.acme.com:nonprod">{v("assets")}</textarea></label>',
        '</details>',
        # ── v0.1: Posture (knowledge level) ──────────────────────────
        '<details class="sew-form-section"><summary>Posture (knowledge level)</summary>',
        '<label style="display:block">'
        f'<input type="radio" name="posture" value="blackbox"'
        f'{" checked" if (s.get("posture","blackbox") == "blackbox") else ""}> '
        '<b>Black-box</b> — no provided mats; agent reads denied.</label>',
        '<label style="display:block">'
        f'<input type="radio" name="posture" value="graybox"'
        f'{" checked" if s.get("posture") == "graybox" else ""}> '
        '<b>Gray-box</b> — docs / architecture / api-specs typical.</label>',
        '<label style="display:block">'
        f'<input type="radio" name="posture" value="whitebox"'
        f'{" checked" if s.get("posture") == "whitebox" else ""}> '
        '<b>White-box</b> — source / artifacts available.</label>',
        '<p class="ara-sub" style="margin-top:8px">For gray/white-box, '
        'place mats under <code>&lt;state&gt;/engagements/&lt;task-id&gt;'
        '/mats/{source,architecture,api-specs,docs,artifacts,creds}/</code> '
        'before creating — populated classes auto-emit <code>oc:read</code>'
        ' allows.</p>',
        '</details>',
        # ── v0.1: Activity (passive / active) ────────────────────────
        '<details class="sew-form-section"><summary>Activity</summary>',
        '<label style="display:flex;align-items:center;gap:8px">'
        f'<input type="checkbox" name="active_testing" value="1"'
        f'{" checked" if s.get("active_testing") else ""}> '
        '<b>Active testing</b> — declares <code>ACTIVE-TESTING-APPROVAL</code>.'
        ' Required for any outbound request to scope hosts.</label>',
        '</details>',
        # ── v0.1: Depth tier (informational only) ────────────────────
        '<details class="sew-form-section"><summary>Depth tier '
        '<span class="ara-pill">informational</span></summary>',
        '<p class="ara-sub">Recon is gated by knowledge-gap detection '
        '(facts from mats), not depth tier. This declaration appears in '
        '<code>absolute-trim.md</code> so the LLM understands task '
        'contour.</p>',
    ])
    for d, label in [
        ("recon", "Recon"), ("scan", "Scan"),
        ("vc", "Vulnerability confirmation"),
        ("exploitation", "Exploitation"),
        ("post_exploit", "Post-exploit"),
    ]:
        sel = " checked" if s.get("depth", "recon") == d else ""
        parts.append(
            f'<label style="display:inline-flex;gap:4px;margin-right:12px">'
            f'<input type="radio" name="depth" value="{d}"{sel}> {e(label)}</label>'
        )
    parts.extend([
        '</details>',
        # Test window — extended with TOD section
        '<details class="sew-form-section"><summary>Test Window</summary>',
        f'<label>start (UTC)<input type="datetime-local" name="window_start" '
        f'value="{v("window_start")}"></label>',
        f'<label>end (UTC)<input type="datetime-local" name="window_end" '
        f'value="{v("window_end")}"></label>',
        '<label>time-of-day windows (one per line, format: '
        '<code>days|HH:MM-HH:MM|tz</code>)<textarea name="tod_allowed" rows="3" '
        f'placeholder="mon,tue,wed,thu,fri|09:00-17:00|America/New_York">'
        f'{v("tod_allowed")}</textarea></label>',
        '<p class="ara-sub">Outside any window, active operations halt; '
        'operator gets a 4-way modal (stop / extend / pivot to read-only / '
        'interactive). Empty = unrestricted.</p>',
        '</details>',
        # ROE attestation
        '<details class="sew-form-section"><summary>ROE Attestation</summary>',
        f'<label>signed by<input type="text" name="signed_by" '
        f'value="{v("signed_by")}" placeholder="ops@acme.com"></label>',
        f'<label>signed at<input type="datetime-local" name="signed_at" '
        f'value="{v("signed_at")}"></label>',
        '</details>',
        # Channels
        '<details class="sew-form-section"><summary>Delivery Channels</summary>',
        f'<div>{chan_checkboxes}</div>',
        '</details>',
        # ── v0.1: Rate limits ─────────────────────────────────────────
        '<details class="sew-form-section"><summary>Rate limits</summary>',
        '<label>global RPS (0 = unlimited)<input type="number" '
        'name="rate_global_rps" min="0" step="1" '
        f'value="{v("rate_global_rps")}" placeholder="10"></label>',
        '<label>per-host (one per line, format: <code>host:rps</code>)'
        '<textarea name="rate_per_host" rows="2" '
        f'placeholder="portal.acme.com:5">{v("rate_per_host")}'
        '</textarea></label>',
        '<label>overflow behavior</label>',
        '<label style="display:inline-flex;gap:4px;margin-right:12px">'
        f'<input type="radio" name="rate_overflow" value="queue"'
        f'{" checked" if s.get("rate_overflow", "queue") == "queue" else ""}> '
        'queue (delay)</label>',
        '<label style="display:inline-flex;gap:4px;margin-right:12px">'
        f'<input type="radio" name="rate_overflow" value="deny"'
        f'{" checked" if s.get("rate_overflow") == "deny" else ""}> '
        'deny (hard fail)</label>',
        '</details>',
        # ── v0.1: Stop conditions ─────────────────────────────────────
        '<details class="sew-form-section"><summary>Stop conditions</summary>',
        '<label style="display:flex;align-items:center;gap:8px">'
        f'<input type="checkbox" name="stop_first_critical" value="1"'
        f'{" checked" if s.get("stop_first_critical") else ""}> '
        '<b>first_critical</b> — halt on first severity=critical finding'
        '</label>',
        '<label style="display:flex;align-items:center;gap:8px">'
        f'<input type="checkbox" name="stop_first_p1" value="1"'
        f'{" checked" if s.get("stop_first_p1") else ""}> '
        '<b>first_p1</b> — halt on first severity=p1 finding</label>',
        '<label>max_findings:N<input type="number" name="stop_max_findings" '
        f'min="0" step="1" value="{v("stop_max_findings")}" placeholder="50">'
        '</label>',
        '<label>duration_exceeded (e.g. <code>8h</code>, <code>1d</code>) '
        '<input type="text" name="stop_duration_exceeded" '
        f'value="{v("stop_duration_exceeded")}" placeholder="8h"></label>',
        '<p class="ara-sub">Tripping any condition halts new crumb '
        'dispatch; in-flight crumbs finish; operator gets a notification '
        'with finding context. Resume requires <code>RESUME-AFTER-STOP</code>'
        ' token.</p>',
        '</details>',
        # Operator prefs / governance tokens
        '<details class="sew-form-section"><summary>Operator Preferences</summary>',
        '<label style="display:flex;align-items:center;gap:8px">'
        f'<input type="checkbox" name="approve_high_risk" value="1"'
        f'{" checked" if s.get("approve_high_risk") else ""}> '
        'approve high-risk patterns (gates oc:bash matches)'
        '</label>',
        f'<label>high-risk patterns (one per line)<textarea name="high_risk_patterns" '
        f'rows="2" placeholder="* delete *&#10;* drop *">'
        f'{v("high_risk_patterns")}</textarea></label>',
        '<label style="display:flex;align-items:center;gap:8px">'
        f'<input type="checkbox" name="absolute_enforce" value="1"'
        f'{" checked" if s.get("absolute_enforce", "1") else ""}> '
        'enforce absolute (default)'
        '</label>',
        '<label style="display:flex;align-items:center;gap:8px">'
        f'<input type="checkbox" name="release_on_armor_unequip" value="1"'
        f'{" checked" if s.get("release_on_armor_unequip", "1") else ""}> '
        'auto-release when parent armor is unequipped (default)'
        '</label>',
        '</details>',
        # Misc (per intake revision)
        '<details class="sew-form-section"><summary>Misc</summary>',
        f'<label>operator notes / freeform<textarea name="misc" rows="4" '
        f'placeholder="Single-tenant. Do not test billing flows.">'
        f'{v("misc")}</textarea></label>',
        '</details>',
        # Apply
        '<label style="display:flex;align-items:center;gap:8px">'
        f'<input type="checkbox" name="apply" value="1"'
        f'{" checked" if s.get("apply") else ""}> '
        'apply this trim immediately after creating'
        '</label>',
        '<div style="display:flex;gap:8px;margin-top:8px">',
        '<button class="ara-btn ara-btn-primary" type="submit">Create trim</button>',
        f'<a class="ara-btn ara-btn-ghost" href="/trims?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Cancel</a>',
        '</div>',
        '</form>',
        '</section>',
    ])
    return base_views._page("Create Trim", "sew", "".join(parts), token)


def parse_derive_form(body: dict) -> dict:
    """Convert the derive wizard form-body into a trims.derive_trim
    engagement dict + extra-fields for re-render. Pure helper.

    v0.1 fields handled: target.env (+ assets[] for mixed), posture,
    active_testing (bool), depth, test_window.tod_allowed[], rate_limits,
    stop_conditions.
    """
    def _lines(key: str) -> list[str]:
        raw = (body.get(key) or "").strip()
        return [ln.strip() for ln in raw.splitlines() if ln.strip()]

    channels = [
        c for c in _BUILTIN_CHANNELS if body.get(f"channel_{c}")
    ]
    auth: dict = {}
    if body.get("signed_by"):
        auth["signed_by"] = body["signed_by"]
    if body.get("signed_at"):
        auth["signed_at"] = body["signed_at"]

    def _iso(dt_local: str) -> str:
        # datetime-local form values look like "2026-04-27T13:00".
        # Append :00Z for ISO-8601 UTC normalization.
        if not dt_local:
            return ""
        if len(dt_local) == 16:  # no seconds
            dt_local += ":00"
        return dt_local + "Z"

    # ── v0.1: env + per-host assets (only used when env=mixed) ──────
    env = (body.get("env") or "nonprod").strip().lower()
    if env not in ("prod", "nonprod", "mixed"):
        env = "nonprod"
    assets: list[dict] = []
    if env == "mixed":
        # Operator authors lines like "host:env" e.g. "portal.acme.com:prod"
        for line in _lines("assets"):
            if ":" in line:
                host, raw_env = line.rsplit(":", 1)
                e = raw_env.strip().lower()
                if e in ("prod", "nonprod"):
                    assets.append({"host": host.strip(), "env": e})

    # ── v0.1: TOD windows (operator authors one per line: "days|HH:MM-HH:MM|tz")
    tod_allowed: list[dict] = []
    for line in _lines("tod_allowed"):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        days_str, time_str = parts[0], parts[1]
        tz = parts[2] if len(parts) >= 3 else "UTC"
        days = [d.strip().lower() for d in days_str.split(",") if d.strip()]
        if "-" not in time_str:
            continue
        start_t, _, end_t = time_str.partition("-")
        tod_allowed.append({
            "days": days,
            "start": start_t.strip(),
            "end": end_t.strip(),
            "tz": tz,
        })

    # ── v0.1: rate limits ────────────────────────────────────────────
    rate_limits: dict = {}
    try:
        gl = float(body.get("rate_global_rps") or 0)
        if gl > 0:
            rate_limits["global_rps"] = gl
    except ValueError:
        pass
    per_host: dict = {}
    for line in _lines("rate_per_host"):
        if ":" in line:
            host, raw_rps = line.rsplit(":", 1)
            try:
                per_host[host.strip()] = float(raw_rps.strip())
            except ValueError:
                continue
    if per_host:
        rate_limits["per_host"] = per_host
    overflow = (body.get("rate_overflow") or "queue").strip().lower()
    if overflow in ("queue", "deny"):
        rate_limits["overflow"] = overflow

    # ── v0.1: stop conditions ───────────────────────────────────────
    stop: list[str] = []
    if body.get("stop_first_critical"):
        stop.append("first_critical")
    if body.get("stop_first_p1"):
        stop.append("first_p1")
    if body.get("stop_max_findings"):
        try:
            n = int(body["stop_max_findings"])
            if n > 0:
                stop.append(f"max_findings:{n}")
        except ValueError:
            pass
    if body.get("stop_duration_exceeded"):
        dur = str(body["stop_duration_exceeded"]).strip()
        if dur:
            stop.append(f"duration_exceeded:{dur}")

    posture = (body.get("posture") or "blackbox").strip().lower()
    if posture not in ("blackbox", "graybox", "whitebox"):
        posture = "blackbox"
    depth = (body.get("depth") or "recon").strip().lower()
    if depth not in ("recon", "scan", "vc", "exploitation", "post_exploit"):
        depth = "recon"

    engagement = {
        "engagement_id": body.get("engagement_id") or "",
        "operator_id": body.get("operator_id") or "",
        "target": {
            "primary": body.get("target_primary") or "",
            "scope_allows": _lines("scope_allows"),
            "oos": _lines("oos"),
            "env": env,
            "assets": assets,
        },
        "test_window": {
            "start": _iso(body.get("window_start") or ""),
            "end": _iso(body.get("window_end") or ""),
            "tod_allowed": tod_allowed,
        },
        "authorization": auth,
        "delivery_channels": channels,
        "operator_prefs": {
            "approve_high_risk": bool(body.get("approve_high_risk")),
            "high_risk_patterns": _lines("high_risk_patterns"),
            "absolute_enforce": bool(body.get("absolute_enforce")),
            "release_on_armor_unequip": bool(body.get("release_on_armor_unequip")),
        },
        # v0.1 first-class:
        "posture": posture,
        "active_testing": bool(body.get("active_testing")),
        "depth": depth,
        "rate_limits": rate_limits,
        "stop_conditions": stop,
        "misc": body.get("misc") or "",
    }
    extras = {
        "armor": body.get("armor") or "",
        "weapon_ref": body.get("weapon_ref") or None,
        "apply": bool(body.get("apply")),
    }
    return {"engagement": engagement, **extras}


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def render_trim_detail(
    state_root: Path, armor: str, trim_id: str, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    meta = trims_mod.load_trim(state_root, armor, trim_id)
    if meta is None:
        body = (
            f'<section><h2 class="ara-h">Trim not found</h2>'
            f'<p class="ara-sub">No trim <code>{e(trim_id)}</code> '
            f'under armor <code>{e(armor)}</code>. '
            f'<a href="/trims?t={e(token)}">Back to list</a>.</p></section>'
        )
        return base_views._page("Trim", "trims", body, token)

    is_active = trims_mod.active_trim_id(state_root, armor) == trim_id
    bw_path = trims_mod.trim_bulwark_section_path(state_root, armor, trim_id)
    abs_path = trims_mod.trim_absolute_path(state_root, armor, trim_id)
    bw_body = bw_path.read_text() if bw_path.is_file() else ""
    abs_body = abs_path.read_text() if abs_path.is_file() else ""
    if is_active:
        visual_action = (
            f'<form method="post" '
            f'action="/trims/{e(armor)}/{e(trim_id)}/release?t={e(token)}" '
            f'style="margin:0">'
            f'<button class="ara-btn ara-btn-primary" type="submit">Release</button>'
            f'</form>'
        )
    else:
        visual_action = (
            f'<form method="post" '
            f'action="/trims/{e(armor)}/{e(trim_id)}/apply?t={e(token)}" '
            f'style="margin:0">'
            f'<button class="ara-btn ara-btn-primary" type="submit">Apply</button>'
            f'</form>'
            f'<form method="post" '
            f'action="/trims/{e(armor)}/{e(trim_id)}/delete?t={e(token)}" '
            f'style="margin:0" '
            f'onsubmit="return confirm(\'Delete trim {e(trim_id)}?\');">'
            f'<button class="ara-btn ara-btn-ghost" type="submit" '
            f'style="border-color:var(--error);color:var(--error)">Delete</button>'
            f'</form>'
        )

    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section class="ara-armor-focus ara-trim-detail-focus" aria-label="Trim visual identity">'
        '<div class="ara-armor-focus-card">'
        '<div>'
        '<div class="ara-eyebrow">Trim identity</div>'
        + _render_loadout_card(
            "trim",
            "Trim",
            trim_id,
            meta.target or meta.engagement_id or "Task overlay.",
            None,
            token,
            glyph="T",
            meta_rows=[
                ("env", meta.env or "nonprod"),
                ("depth", meta.depth or "recon"),
                ("state", "applied" if is_active else "standby"),
            ],
        )
        + '</div>'
        '<div class="ara-armor-focus-copy">'
        '<span class="ara-eyebrow">Manage task overlay</span>'
        f'<h3>{e(trim_id)}</h3>'
        '<p>Generated visual identity is deterministic from the trim id, '
        'so the same task overlay keeps the same silhouette anywhere it appears.</p>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">'
        + visual_action
        + f'<a class="ara-btn ara-btn-ghost" href="/trims?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Back to trims</a>'
        + f'<a class="ara-btn ara-btn-ghost" href="/temper/create?t={e(token)}&amp;armor={e(armor)}&amp;trim={e(trim_id)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Promote to Armor</a>'
        '</div>'
        '<div class="ara-gateway-row" style="justify-content:flex-start">'
        f'<span class="rh-badge">armor {e(armor)}</span>'
        f'<span class="rh-badge">target {e(meta.target or "—")}</span>'
        '</div>'
        '</div>'
        '</div>'
        '</section>',
        '<section>',
        '<div class="ara-eyebrow">'
        f'Trim · <code>{e(armor)}</code></div>',
        f'<h2 class="ara-h" style="margin-bottom:4px">{e(trim_id)}'
        + (' <span class="ara-pill" style="background:var(--accent);'
           'color:#000;margin-left:8px">applied</span>' if is_active else '')
        + '</h2>',
        f'<p class="ara-sub">{e(meta.target or "(no target)")}'
        + (f' · expires {e(meta.expires_at)}' if meta.expires_at else '')
        + '</p>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};'
            f'margin:8px 0 16px">{e(flash)}</div>'
        )

    # Action buttons
    parts.append('<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">')
    if is_active:
        parts.append(
            f'<form method="post" '
            f'action="/trims/{e(armor)}/{e(trim_id)}/release?t={e(token)}" '
            f'style="margin:0">'
            f'<button class="ara-btn ara-btn-primary" type="submit">Release</button>'
            f'</form>'
        )
    else:
        parts.append(
            f'<form method="post" '
            f'action="/trims/{e(armor)}/{e(trim_id)}/apply?t={e(token)}" '
            f'style="margin:0">'
            f'<button class="ara-btn ara-btn-primary" type="submit">Apply</button>'
            f'</form>'
        )
        parts.append(
            f'<form method="post" '
            f'action="/trims/{e(armor)}/{e(trim_id)}/delete?t={e(token)}" '
            f'style="margin:0" '
            f'onsubmit="return confirm(\'Delete trim {e(trim_id)}?\');">'
            f'<button class="ara-btn ara-btn-ghost" type="submit" '
            f'style="border-color:var(--error);color:var(--error)">Delete</button>'
            f'</form>'
        )
    parts.append(
        f'<a class="ara-btn ara-btn-ghost" href="/trims?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        '← Back to list</a>'
    )
    parts.append(
        f'<a class="ara-btn ara-btn-ghost" href="/temper/create?t={e(token)}&amp;armor={e(armor)}&amp;trim={e(trim_id)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        'Promote to Armor</a>'
    )
    parts.append('</div>')

    # Meta JSON
    parts.append('<details open><summary>Meta</summary>')
    parts.append(
        '<pre style="background:var(--surface);padding:12px;'
        'border-radius:4px;overflow:auto;font-size:11px">'
        f'{e(json.dumps(meta.to_dict(), indent=2, sort_keys=True))}</pre>'
    )
    parts.append('</details>')

    # Bulwark section preview
    parts.append('<details open style="margin-top:16px">'
                 '<summary>Bulwark section</summary>')
    if bw_body.strip():
        parts.append(
            '<pre style="background:var(--surface);padding:12px;'
            'border-radius:4px;overflow:auto;font-size:11px">'
            f'{e(bw_body)}</pre>'
        )
    else:
        parts.append('<p class="ara-sub">(empty)</p>')
    parts.append('</details>')

    # Absolute section preview
    parts.append('<details open style="margin-top:16px">'
                 '<summary>Absolute trim</summary>')
    if abs_body.strip():
        parts.append(
            '<pre style="background:var(--surface);padding:12px;'
            'border-radius:4px;overflow:auto;font-size:11px">'
            f'{e(abs_body)}</pre>'
        )
    else:
        parts.append('<p class="ara-sub">(empty)</p>')
    parts.append('</details>')

    parts.append('</section>')
    return base_views._page(
        f"Trim · {trim_id}", "trims", "".join(parts), token,
    )


# ---------------------------------------------------------------------------
# POST handlers (return (redirect_path, None) or (None, error_html))
# ---------------------------------------------------------------------------

def handle_trim_new(state_root: Path, body: dict, token: str) -> str:
    """Returns the redirect target (Location)."""
    armor = (body.get("armor") or "").strip()
    trim_id = (body.get("trim_id") or "").strip()
    target = body.get("target") or ""
    operator = body.get("operator") or ""
    weapon_ref = body.get("weapon_ref") or None
    engagement_id = body.get("engagement_id") or None
    if not armor or not trim_id:
        return (
            f"/trims/new?t={token}"
            f"&flash={urllib.parse.quote('armor and trim id are required')}"
        )
    try:
        trims_mod.new_trim(
            state_root, armor, trim_id,
            target=target, operator_id=operator,
            weapon_ref=weapon_ref, engagement_id=engagement_id,
        )
    except (
        trims_mod.InvalidTrimId,
        FileNotFoundError, FileExistsError,
    ) as exc:
        return (
            f"/trims/new?t={token}"
            f"&flash={urllib.parse.quote(str(exc))}"
        )
    return (
        f"/trims/{urllib.parse.quote(armor)}/"
        f"{urllib.parse.quote(trim_id)}?t={token}"
    )


def handle_trim_derive(state_root: Path, body: dict, token: str) -> str:
    parsed = parse_derive_form(body)
    armor = parsed["armor"]
    if not armor:
        return (
            f"/trims/derive?t={token}"
            f"&flash={urllib.parse.quote('armor is required')}"
        )
    eng = parsed["engagement"]
    if not eng.get("engagement_id") or not eng["target"]["primary"]:
        return (
            f"/trims/derive?t={token}"
            f"&flash={urllib.parse.quote('task id and primary target are required')}"
        )
    try:
        meta = trims_mod.derive_trim(
            state_root, armor,
            weapon_id=parsed["weapon_ref"],
            engagement=eng,
            operator_id=eng.get("operator_id") or "",
        )
    except (
        trims_mod.InvalidTrimId, FileNotFoundError,
    ) as exc:
        return (
            f"/trims/derive?t={token}"
            f"&flash={urllib.parse.quote(str(exc))}"
        )
    if parsed["apply"]:
        try:
            trims_mod.apply_trim(state_root, armor, meta.trim_id)
        except (
            trims_mod.TrimNotFound,
            trims_mod.TrimCurrentlyApplied,
        ) as exc:
            return (
                f"/trims/{urllib.parse.quote(armor)}/"
                f"{urllib.parse.quote(meta.trim_id)}?t={token}"
                f"&flash={urllib.parse.quote('created but apply failed: ' + str(exc))}"
                f"&flash_level=bad"
            )
    return (
        f"/trims/{urllib.parse.quote(armor)}/"
        f"{urllib.parse.quote(meta.trim_id)}?t={token}"
    )


def handle_trim_apply(
    state_root: Path, armor: str, trim_id: str, token: str,
) -> str:
    try:
        trims_mod.apply_trim(state_root, armor, trim_id)
    except (
        trims_mod.TrimNotFound,
        trims_mod.TrimCurrentlyApplied,
    ) as exc:
        return (
            f"/trims/{urllib.parse.quote(armor)}/"
            f"{urllib.parse.quote(trim_id)}?t={token}"
            f"&flash={urllib.parse.quote(str(exc))}"
            f"&flash_level=bad"
        )
    return (
        f"/trims/{urllib.parse.quote(armor)}/"
        f"{urllib.parse.quote(trim_id)}?t={token}"
        f"&flash={urllib.parse.quote('applied')}"
    )


def handle_trim_release(
    state_root: Path, armor: str, trim_id: str, token: str,
) -> str:
    trims_mod.release_trim(state_root, armor, trim_id)
    return (
        f"/trims/{urllib.parse.quote(armor)}/"
        f"{urllib.parse.quote(trim_id)}?t={token}"
        f"&flash={urllib.parse.quote('released')}"
    )


def handle_trim_delete(
    state_root: Path, armor: str, trim_id: str, token: str,
) -> str:
    try:
        gone = trims_mod.delete_trim(state_root, armor, trim_id)
    except trims_mod.TrimCurrentlyApplied as exc:
        return (
            f"/trims/{urllib.parse.quote(armor)}/"
            f"{urllib.parse.quote(trim_id)}?t={token}"
            f"&flash={urllib.parse.quote(str(exc))}&flash_level=bad"
        )
    if not gone:
        return (
            f"/trims?t={token}"
            f"&flash={urllib.parse.quote('trim not found')}"
            f"&flash_level=bad"
        )
    return (
        f"/trims?t={token}"
        f"&flash={urllib.parse.quote(f'deleted {trim_id}')}"
    )
