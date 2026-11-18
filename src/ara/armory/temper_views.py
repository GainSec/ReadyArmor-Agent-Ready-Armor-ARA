"""Server-rendered views for Armor Tempering."""
from __future__ import annotations

import html
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

from ara import armors as armors_mod
from ara import temper as temper_mod
from ara import temper_controls
from ara import temper_ingest
from ara import temper_lifecycle
from ara import tempers as tempers_mod
from ara import seed_temper as seed_temper_mod
from ara import trims as trims_mod
from . import views as base_views
from .views import _render_armor_info_card, _static_css_href


def e(s: object) -> str:
    return html.escape(str(s)) if s is not None else ""


def render_temper_index(
    state_root: Path,
    token: str,
    *,
    armor: str | None = None,
    trim: str | None = None,
    forge: str | None = None,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    return render_temper_review(
        state_root,
        token,
        armor=armor,
        trim=trim,
        forge=forge,
        flash=flash,
        flash_level=flash_level,
    )


def render_temper_create(
    state_root: Path,
    token: str,
    *,
    armor: str | None = None,
    trim: str | None = None,
    forge: str | None = None,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    """Curated-temper page. Temper FORGING is retired: a temper is a small
    authored set of cross-cutting agent-hardening doctrines, not per-element
    forged output. This page installs/equips the baseline hardening temper;
    tool permissions live in the weapon (allowlist) scoped by the trim."""
    baseline_id = seed_temper_mod.BASELINE_TEMPER_ID
    meta = tempers_mod.load_meta(state_root, baseline_id) or {}
    installed = bool(meta)
    equipped = tempers_mod.is_equipped(state_root, baseline_id)
    n_doctrines = len(seed_temper_mod.BASELINE_DOCTRINES)

    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section class="ara-armor-focus temper-hero" aria-label="Curated tempers">',
        '<div class="ara-armor-focus-card">',
        '<div class="ara-armor-focus-copy">',
        '<span class="ara-eyebrow">Temper</span>',
        '<h2 class="ara-h">Curated hardening</h2>',
        '<p>A temper is a small, authored set of <b>cross-cutting</b> agent-hardening '
        "doctrines &mdash; output hygiene (don't leak secrets), standing safety "
        'principles, and destructive-action gates &mdash; that hold regardless of the '
        'task. <b>Which tools an agent may run is not a temper</b>: that is the '
        "weapon's allowlist scoped by the trim.</p>",
        '</div>',
        '<div class="ara-armor-info-grid">',
        _render_armor_info_card(
            "Baseline hardening",
            f"{n_doctrines} doctrines",
            "Credential/key/cloud-secret canaries, safety doctrines, and "
            "destructive-action approval gates.",
            [("installed", "yes" if installed else "no"),
             ("equipped", "yes" if equipped else "no"),
             ("enforced", ", ".join(meta.get("enforced") or []) or "—"),
             ("bulwark rules", str(meta.get("bulwark_rules", 0)))],
            glyph="H",
        ),
        '</div>',
        '</div>',
        '</section>',
        '<section>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};margin-bottom:16px">{e(flash)}</div>'
        )
    parts.append('<div class="temper-create-queue" style="display:flex;gap:8px;flex-wrap:wrap">')
    if not installed:
        parts.append(
            f'<form method="post" action="/temper/baseline/install?t={e(token)}" style="margin:0">'
            '<button class="ara-btn ara-btn-primary" type="submit">Install baseline hardening temper</button></form>')
    else:
        if not equipped:
            parts.append(
                f'<form method="post" action="/tempers/{e(baseline_id)}/equip?t={e(token)}" style="margin:0">'
                '<button class="ara-btn ara-btn-primary" type="submit">Equip baseline hardening</button></form>')
        else:
            parts.append(
                f'<form method="post" action="/tempers/{e(baseline_id)}/unequip?t={e(token)}" style="margin:0">'
                '<button class="ara-btn ara-btn-ghost" type="submit">Unequip baseline hardening</button></form>')
        parts.append(
            f'<form method="post" action="/temper/baseline/install?t={e(token)}" style="margin:0">'
            '<button class="ara-btn ara-btn-ghost" type="submit">Reinstall (refresh doctrines)</button></form>')
    parts.append(
        f'<a class="ara-btn ara-btn-ghost" href="/temper?t={e(token)}">Open temper slot</a>')
    parts.append('</div>')
    parts.append(
        '<p class="ara-sub" style="margin-top:14px">Tempers are curated, not forged. '
        'Security-source knowledge belongs in the <b>weapon</b> forge; engagement '
        'scope and tool permissions belong in the <b>trim</b>.</p>')
    parts.append('</section>')

    return base_views._page("Temper", "temper_create", "".join(parts), token)


def _render_temper_equipment_panel(state_root: Path, token: str) -> str:
    """The temper equipment slot: every ingested temper, its enforce summary,
    and an equip/unequip toggle. Tempers stack — any subset can be active."""
    ids = tempers_mod.list_tempers(state_root)
    if not ids:
        return ""
    active = set(tempers_mod.active_tempers(state_root))
    cards: list[str] = []
    for tid in ids:
        meta = tempers_mod.load_meta(state_root, tid) or {}
        equipped = tid in active
        enforced = ", ".join(meta.get("enforced") or []) or "none"
        action = "unequip" if equipped else "equip"
        label = "Unequip" if equipped else "Equip"
        btn_cls = "ara-btn-ghost" if equipped else "ara-btn-primary"
        badge = (
            '<span class="rh-badge" style="color:var(--accent)">equipped</span>'
            if equipped else '<span class="rh-badge">available</span>'
        )
        cards.append(
            '<article class="ara-kit-card">'
            f'<div class="ara-kit-card-top"><span class="ara-kit-kind">temper</span>{badge}</div>'
            f'<h3>{e(str(meta.get("name") or tid))}</h3>'
            f'<p class="ara-sub"><code>{e(tid)}</code></p>'
            '<div class="ara-kit-meta">'
            f'<div><span>bulwark rules</span><strong>{e(str(meta.get("bulwark_rules", 0)))}</strong></div>'
            f'<div><span>enforced</span><strong>{e(enforced)}</strong></div>'
            f'<div><span>source forge</span><strong>{e(str(meta.get("source_forge") or "none"))}</strong></div>'
            '</div>'
            f'<form method="post" action="/tempers/{e(tid)}/{action}?t={e(token)}" style="margin-top:8px">'
            f'<button class="ara-btn {btn_cls}" type="submit">{label}</button></form>'
            '</article>'
        )
    return (
        '<section><div class="ara-eyebrow">Equipment</div>'
        '<h3 class="ara-h" style="font-size:20px">Temper slot · hot-swappable · stackable</h3>'
        '<p class="ara-sub" style="margin:0 0 12px">A temper is its own equipment: equip any '
        'set at once; each merges its own policy overlay into live Bulwark, independent of '
        'armor or weapon. Unequip strips it cleanly.</p>'
        '<div class="ara-kit-grid">' + "".join(cards) + '</div></section>'
    )


def render_temper_review(
    state_root: Path,
    token: str,
    *,
    armor: str | None = None,
    trim: str | None = None,
    forge: str | None = None,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    proposals = temper_mod.list_proposals(state_root)
    grouped: dict[str, list[temper_mod.TemperProposal]] = defaultdict(list)
    for proposal in proposals:
        grouped[proposal.status].append(proposal)

    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section class="ara-armor-focus temper-hero" aria-label="Temper review">',
        '<div class="ara-armor-focus-card">',
        '<div class="ara-armor-focus-copy">',
        '<span class="ara-eyebrow">Barracks</span>',
        '<h2 class="ara-h">Tempers</h2>',
        '<p>Review Tailor proposals before they become a sealed Temper. '
        'Cards show the actual entry text proposed for armor, Rhema, Bulwark, canaries, alerts, and related controls.</p>',
        '<div class="ara-gateway-row" style="justify-content:flex-start">',
        f'<a class="ara-btn ara-btn-primary" href="/temper/create?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Curated tempers</a>',
        f'<span class="rh-badge">pending {len(grouped.get("pending", []))}</span>',
        f'<span class="rh-badge">sealed {len(grouped.get("sealed", []))}</span>',
        f'<span class="rh-badge">applied {len(grouped.get("applied", []))}</span>',
        '</div>',
        '</div>',
        '<div class="ara-armor-info-grid">',
        _render_armor_info_card(
            "Review queue",
            f"{len(proposals)} proposals",
            "Pending proposals are sealed only after an operator chooses the useful entries.",
            [("pending", str(len(grouped.get("pending", [])))),
             ("sealed", str(len(grouped.get("sealed", [])))),
             ("applied", str(len(grouped.get("applied", [])))),
             ("rejected", str(len(grouped.get("rejected", []))))],
            glyph="T",
        ),
        '</div>',
        '</div>',
        '</section>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<section><div class="ara-empty" style="border-left:3px solid {cls};margin-bottom:16px">{e(flash)}</div></section>'
        )
    parts.append(_render_temper_equipment_panel(state_root, token))
    for status, label in [
        ("pending", "Pending"),
        ("sealed", "Sealed"),
        ("applied", "Applied"),
        ("rejected", "Rejected"),
    ]:
        rows = grouped.get(status, [])
        parts.append(
            f'<section><div class="ara-eyebrow">{e(label)}</div>'
            f'<h3 class="ara-h" style="font-size:20px">{e(label)} proposals</h3>'
        )
        if not rows:
            parts.append('<div class="ara-empty">None.</div>')
        else:
            parts.append('<div class="ara-kit-grid temper-proposal-grid">')
            for proposal in rows:
                parts.append(_proposal_card(proposal, token))
            parts.append('</div>')
        parts.append('</section>')

    return base_views._page("Temper", "temper", "".join(parts), token)


def _render_tailor_panel(
    state_root: Path,
    token: str,
    selected_armor: str,
    forge: str | None,
) -> str:
    session = temper_lifecycle.load_tailor_session(state_root)
    if not session or session.get("status") != "running":
        return ""

    candidates = list(session.get("candidates") or [])
    rejected = list(session.get("rejected_candidates") or [])
    source_request = dict(session.get("source_request") or {})
    target_armor = source_request.get("armor") or selected_armor or "none"
    source_trims = ", ".join(
        str(trim) for trim in list(source_request.get("source_trims") or [])
    ) or "none"
    source_forge = source_request.get("source_forge") or forge or "none"
    parts = [
        '<section>',
        '<div class="ara-armor-detail-inline temper-tailor-panel">',
        '<div class="temper-select-toolbar">',
        '<div><span class="ara-eyebrow">Generation</span>'
        '<strong style="display:block">Temper generation is running</strong>'
        '<p class="ara-sub" style="margin:4px 0 0">Candidates collected here can be turned into a pending proposal for review.</p>'
        '</div>',
        '<div class="temper-select-actions">',
        '<form method="post" '
        f'action="/temper/tailor/finish?t={e(token)}" style="margin:0">',
        '<button class="ara-btn ara-btn-primary" type="submit">Create proposal from candidates</button>',
        '</form>',
        '<form method="post" '
        f'action="/temper/tailor/abort?t={e(token)}" style="margin:0">',
        '<button class="ara-btn ara-btn-ghost" type="submit">Abort generation</button>',
        '</form>',
        '</div></div>',
        '<div class="ara-armor-info-grid" style="margin-top:14px">',
        _render_armor_info_card(
            "Candidate queue",
            f"{len(candidates)} candidates",
            "Validated suggestions waiting to become a reviewable Temper proposal.",
            [
                ("target armor", target_armor),
                ("source trim", source_trims),
                ("source weapon", source_request.get("source_weapon") or "none"),
                ("source forge", source_forge),
                ("rejected attempts", str(len(rejected))),
            ],
            glyph="T",
        ),
        '</div>',
    ]
    if rejected:
        parts.extend([
            '<details class="ara-armor-detail-inline" style="margin-top:16px">',
            '<summary><span class="ara-eyebrow">Repair queue</span>'
            '<strong>Rejected attempts</strong></summary>',
            '<div class="ara-kit-grid" style="margin-top:14px">',
        ])
        for item in rejected[-6:]:
            attempt = dict(item.get("attempt") or {})
            correction = dict(item.get("correction") or {})
            fields = correction.get("rejected_fields") or []
            field_text = ", ".join(str(field) for field in fields) or "unknown"
            parts.append(
                '<article class="ara-kit-card">'
                f'<span class="ara-chip">{e(item.get("destination") or "candidate")}</span>'
                f'<h4>{e(attempt.get("title") or item.get("rejection_id"))}</h4>'
                '<dl>'
                f'<dt>error</dt><dd>{e(item.get("error"))}</dd>'
                f'<dt>fields</dt><dd>{e(field_text)}</dd>'
                f'<dt>correction</dt><dd>{e(correction.get("instruction"))}</dd>'
                '</dl>'
                '</article>'
            )
        parts.extend(['</div>', '</details>'])
    parts.extend([
        '</div>',
        '</section>',
    ])
    return "".join(parts)


def render_temper_detail(
    state_root: Path,
    armor: str,
    proposal_id: str,
    token: str,
    *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    proposal = temper_mod.load_proposal(state_root, armor, proposal_id)
    if proposal is None:
        body = (
            '<section><h2 class="ara-h">Temper proposal not found</h2>'
            f'<p class="ara-sub"><code>{e(proposal_id)}</code> under <code>{e(armor)}</code>.</p>'
            f'<a class="ara-btn ara-btn-ghost" href="/temper?t={e(token)}">Back to Temper</a>'
            '</section>'
        )
        return base_views._page("Temper", "temper", body, token)

    active_temper = temper_mod.active_temper_id(state_root, armor)
    active_trim = trims_mod.active_trim_id(state_root, armor)
    is_active_temper = active_temper == proposal.proposal_id
    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section class="ara-armor-focus" aria-label="Temper detail">',
        '<div class="ara-armor-focus-card">',
        '<div class="ara-armor-focus-copy">',
        '<span class="ara-eyebrow">Temper review</span>',
        f'<h2 class="ara-h"><code>{e(proposal.proposal_id)}</code></h2>',
        f'<p>{e(proposal.operator_notes or "Review proposed armor and trim-fit changes before sealing.")}</p>',
        '<div class="ara-gateway-row" style="justify-content:flex-start">',
        f'<span class="rh-badge">{e(proposal.status)}</span>',
        f'<span class="rh-badge">armor {e(proposal.target_armor)}</span>',
        f'<span class="rh-badge">mode {e(proposal.mode)}</span>',
        '</div>',
        '</div>',
        '<div class="ara-armor-info-grid">',
        _render_armor_info_card(
            "Sources",
            proposal.target_armor,
            "Source trim, weapon, and forge context recorded in this proposal.",
            [
                ("trims", ", ".join(proposal.source_trims) or "none"),
                ("weapon", proposal.source_weapon or "none"),
                ("forge", proposal.source_forge or "none"),
            ],
            glyph="S",
        ),
        '</div>',
        '</div>',
        '</section>',
        '<section>',
    ]
    if flash:
        cls = "var(--error)" if flash_level == "bad" else "var(--accent)"
        parts.append(
            f'<div class="ara-empty" style="border-left:3px solid {cls};margin-bottom:16px">{e(flash)}</div>'
        )
    parts.append('<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">')
    parts.append(
        f'<a class="ara-btn ara-btn-ghost" href="/temper?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Back to Temper</a>'
    )
    if proposal.status == "pending":
        parts.append(
            f'<form method="post" action="/temper/{e(armor)}/{e(proposal.proposal_id)}/reject?t={e(token)}" style="margin:0">'
            '<button class="ara-btn ara-btn-ghost" type="submit">Reject</button></form>'
        )
    elif proposal.status == "sealed":
        parts.append(
            f'<form method="post" action="/temper/{e(armor)}/{e(proposal.proposal_id)}/apply?t={e(token)}" style="margin:0">'
            '<input type="hidden" name="apply_mode" value="apply_next_task">'
            '<button class="ara-btn ara-btn-primary" type="submit">Apply Temper</button></form>'
        )
        if is_active_temper:
            parts.append(
                f'<form method="post" action="/temper/{e(armor)}/{e(proposal.proposal_id)}/release?t={e(token)}" style="margin:0">'
                '<button class="ara-btn ara-btn-ghost" type="submit">Release Temper slot</button></form>'
            )
        else:
            parts.append(
                f'<form method="post" action="/temper/{e(armor)}/{e(proposal.proposal_id)}/activate?t={e(token)}" style="margin:0">'
                '<button class="ara-btn ara-btn-primary" type="submit">Activate Temper slot</button></form>'
            )
    elif proposal.status == "applied":
        if is_active_temper:
            parts.append(
                f'<form method="post" action="/temper/{e(armor)}/{e(proposal.proposal_id)}/release?t={e(token)}" style="margin:0">'
                '<button class="ara-btn ara-btn-ghost" type="submit">Release Temper slot</button></form>'
            )
        else:
            parts.append(
                f'<form method="post" action="/temper/{e(armor)}/{e(proposal.proposal_id)}/activate?t={e(token)}" style="margin:0">'
                '<button class="ara-btn ara-btn-primary" type="submit">Activate Temper slot</button></form>'
            )
    parts.append('</div>')
    parts.append(
        '<div class="ara-empty" style="margin-bottom:16px">'
        f'active trim <code>{e(active_trim or "none")}</code> · '
        f'active Temper <code>{e(active_temper or "none")}</code>'
        '</div>'
    )
    parts.append(_detail_digest(proposal, active_trim, active_temper))
    if proposal.status == "pending":
        all_indexes = ",".join(str(i) for i, _ in enumerate(proposal.items))
        destinations = sorted({item.destination for item in proposal.items})
        destination_counts = Counter(item.destination for item in proposal.items)
        default_name = (proposal.operator_notes or "").strip() or f"temper {proposal.proposal_id}"
        parts.append(
            '<form class="temper-select-form" method="post" '
            f'action="/temper/{e(armor)}/{e(proposal.proposal_id)}/ingest-selected?t={e(token)}">'
            f'<input type="hidden" name="selected_indexes" value="{e(all_indexes)}" '
            'data-temper-selected-indexes>'
            '<div class="ara-armor-detail-inline temper-selection-panel">'
            '<div class="temper-select-toolbar">'
            '<div><span class="ara-eyebrow">Review &amp; ingest</span>'
            '<strong style="display:block">Ingest chosen entries into a new temper</strong>'
            '<p class="ara-sub" style="margin:4px 0 0">Each card shows where it lands '
            '(<code>bulwark.md</code> / <code>absolute.md</code>) and whether it enforces. '
            'Ingest mints a new, named temper from the checked entries and equips it — '
            'hot-swappable and stackable like any other equipment.</p>'
            '<label class="ara-sub" style="display:block;margin:8px 0 0">Temper name'
            f'<input class="ara-input" type="text" name="temper_name" value="{e(default_name)}" '
            'style="display:block;width:100%;max-width:420px;margin-top:4px"></label>'
            '</div>'
            '<div class="temper-select-actions">'
            '<button class="ara-btn ara-btn-ghost" type="button" data-temper-select-all>Select all</button>'
            '<button class="ara-btn ara-btn-ghost" type="button" data-temper-clear>Clear</button>'
            '<button class="ara-btn ara-btn-primary" type="submit">'
            'Ingest checked &rarr; new temper</button>'
            '</div></div>'
            '<div class="ara-gateway-row temper-filter-row">'
            '<span class="rh-badge" data-temper-count>'
            f'{len(proposal.items)} selected</span>'
            '<button class="rh-badge" type="button" data-temper-filter="all">all</button>'
            + "".join(
                '<button class="rh-badge" type="button" '
                f'data-temper-filter="{e(destination)}">'
                f'{e(destination)} <strong>{destination_counts[destination]}</strong>'
                '</button>'
                for destination in destinations
            )
            + '</div></div>'
            '<div class="temper-select-grid">'
        )
        for index, item in enumerate(proposal.items):
            checked = " checked"
            parts.append(_temper_item_card(item, index=index, checked=checked))
        parts.append('</div></form>')
        parts.append(_temper_selection_script())
    else:
        parts.append('<div class="temper-select-grid">')
        for item in proposal.items:
            parts.append(_temper_item_card(item))
        parts.append('</div>')
    parts.append('<details class="ara-armor-detail-inline" style="margin-top:18px">')
    parts.append('<summary><span class="ara-eyebrow">Diff</span><strong>Readable proposed text</strong></summary>')
    for item in proposal.items:
        parts.append(
            f'<details><summary>{e(item.destination)} · {e(item.title)}</summary>'
            f'<pre class="ara-code">{e(item.proposed_text)}</pre></details>'
        )
    parts.append('</details></section>')
    return base_views._page(f"Temper · {proposal_id}", "temper", "".join(parts), token)


def handle_temper_create(state_root: Path, body: dict, token: str) -> str:
    armor = (body.get("armor") or "").strip()
    trim = (body.get("source_trim") or "").strip()
    source_trims = [
        item.strip()
        for item in (body.get("source_trims") or trim).split(",")
        if item.strip()
    ]
    try:
        proposal = temper_mod.create_proposal(
            state_root,
            armor,
            source_trims=source_trims,
            source_weapon=(body.get("source_weapon") or "").strip() or None,
            source_forge=(body.get("source_forge") or "").strip() or None,
            mode=(body.get("mode") or "full_temper").strip() or "full_temper",
            operator_notes=body.get("operator_notes") or "",
        )
    except (FileNotFoundError, ValueError) as exc:
        return (
            f"/temper/create?t={token}&flash={urllib.parse.quote(str(exc))}"
            "&flash_level=bad"
        )
    return (
        f"/temper/{urllib.parse.quote(proposal.target_armor)}/"
        f"{urllib.parse.quote(proposal.proposal_id)}?t={token}"
    )


def handle_temper_install_baseline(state_root: Path, token: str) -> str:
    """Install (materialize) the curated baseline hardening temper."""
    try:
        meta = seed_temper_mod.install_baseline_temper(state_root)
    except Exception as exc:  # noqa: BLE001
        return f"/temper/create?t={token}&flash={urllib.parse.quote(str(exc))}&flash_level=bad"
    msg = (
        f"installed baseline hardening temper "
        f"({meta.get('bulwark_rules', 0)} enforced rules + doctrine) — equip it below"
    )
    return f"/temper/create?t={token}&flash={urllib.parse.quote(msg)}&flash_level=info"


# Temper FORGING is retired — tempers are curated, not forged. These handlers
# remain as safe no-ops so any stale bookmark/route returns a message instead of
# forging. See render_temper_create / seed_temper for the curated path.
_FORGE_RETIRED_MSG = (
    "Temper forging is retired — tempers are curated hardening, not forged. "
    "Install the baseline hardening temper instead."
)


def handle_temper_tailor_start(state_root: Path, body: dict, token: str) -> str:
    return f"/temper/create?t={token}&flash={urllib.parse.quote(_FORGE_RETIRED_MSG)}&flash_level=bad"


def _retired_handle_temper_tailor_start(state_root: Path, body: dict, token: str) -> str:
    armor = (body.get("armor") or "").strip()
    trim = (body.get("source_trim") or "").strip()
    source_trims = [
        item.strip()
        for item in (body.get("source_trims") or trim).split(",")
        if item.strip()
    ]
    source_request = {
        "armor": armor,
        "source_trims": source_trims,
        "source_weapon": (body.get("source_weapon") or "").strip(),
        "source_forge": (body.get("source_forge") or "").strip(),
        "mode": (body.get("mode") or "full_temper").strip() or "full_temper",
        "operator_notes": body.get("operator_notes") or "",
    }
    try:
        record = temper_lifecycle.start_tailor_session(
            state_root,
            source_request=source_request,
        )
    except temper_lifecycle.TailorLifecycleError as exc:
        return _index_redirect(token, str(exc), "bad")
    return _index_redirect(
        token,
        "Temper generation started.",
        "info",
    )


def handle_temper_tailor_finish(
    state_root: Path, body: dict, token: str,
) -> str:
    record = temper_lifecycle.load_tailor_session(state_root)
    if record is None:
        return _index_redirect(token, "no tailor session record found", "bad")
    source_request = dict(record.get("source_request") or {})
    target_armor = (
        (body.get("armor") or "").strip()
        or source_request.get("armor")
        or record.get("prior_armor")
        or ""
    )
    try:
        proposal = None
        if record.get("candidates"):
            proposal = temper_mod.create_proposal_from_tailor_session(
                state_root,
                target_armor,
                session_id=record.get("session_id"),
                operator_notes=source_request.get("operator_notes") or "",
            )
        finished = temper_lifecycle.finish_tailor_session(state_root)
    except (FileNotFoundError, ValueError, temper_mod.TemperStateError,
            temper_lifecycle.TailorLifecycleError) as exc:
        return _index_redirect(token, str(exc), "bad")
    if proposal is not None:
        return _detail_redirect(
            proposal.target_armor,
            proposal.proposal_id,
            token,
            "Tailor proposal created and session restored.",
            "info",
        )
    return _index_redirect(
        token,
        "Temper generation finished with no candidates.",
        "info",
    )


def handle_temper_tailor_abort(state_root: Path, token: str) -> str:
    try:
        record = temper_lifecycle.abort_tailor_session(state_root)
    except temper_lifecycle.TailorLifecycleError as exc:
        return _index_redirect(token, str(exc), "bad")
    return _index_redirect(
        token,
        "Temper generation aborted.",
        "info",
    )


def handle_temper_seal(
    state_root: Path, armor: str, proposal_id: str, token: str,
) -> str:
    try:
        proposal = temper_mod.seal_proposal(state_root, armor, proposal_id)
    except (FileNotFoundError, temper_mod.TemperStateError) as exc:
        return _detail_redirect(armor, proposal_id, token, str(exc), "bad")
    return _detail_redirect(
        proposal.target_armor, proposal.proposal_id, token, "sealed", "info",
    )


def handle_temper_ingest_selected(
    state_root: Path, armor: str, proposal_id: str, body: dict, token: str,
) -> str:
    """Seal the checked entries and immediately activate them — writing each
    to its enforced home (bulwark.md / absolute) in one operator action."""
    raw = str(body.get("selected_indexes") or "").strip()
    selected: list[int] = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            selected.append(int(value))
        except ValueError:
            return _detail_redirect(
                armor, proposal_id, token, f"invalid selected index: {value}", "bad")
    if not selected:
        return _detail_redirect(
            armor, proposal_id, token, "select at least one entry to ingest", "bad")
    proposal = temper_mod.load_proposal(state_root, armor, proposal_id)
    if proposal is None:
        return _detail_redirect(armor, proposal_id, token, "proposal not found", "bad")
    try:
        chosen = [proposal.items[i] for i in selected]
    except IndexError:
        return _detail_redirect(
            armor, proposal_id, token, "selection out of range", "bad")
    # A temper is its own hot-swappable equipment slot. Like every other
    # equipment, each ingest mints a NEW, distinctly-named temper: the chosen
    # entries go into its OWN policy files and it's equipped (overlay-merged
    # into live policy, independent of armor/weapon).
    name = (str(body.get("temper_name") or "").strip()
            or proposal.operator_notes or f"temper {proposal_id}")
    temper_id = tempers_mod.new_temper_id(state_root, name)
    meta = tempers_mod.ingest(
        state_root, temper_id, chosen,
        name=name,
        source_forge=proposal.source_forge,
        source_armor=proposal.target_armor,
    )
    tempers_mod.equip(state_root, temper_id)
    enforced = ", ".join(meta.get("enforced") or []) or "none"
    advisory = ", ".join(meta.get("advisory") or []) or "none"
    return _detail_redirect(
        armor, proposal_id, token,
        f"ingested {len(chosen)} entries into temper '{temper_id}' and equipped it · "
        f"enforced: {enforced} · advisory: {advisory}",
        "info")


def _temper_index_redirect(token: str, flash: str, level: str) -> str:
    return (
        f"/temper?t={token}&flash={urllib.parse.quote(flash)}"
        f"&flash_level={urllib.parse.quote(level)}"
    )


def handle_temper_equip(state_root: Path, temper_id: str, token: str) -> str:
    try:
        result = tempers_mod.equip(state_root, temper_id)
    except FileNotFoundError as exc:
        return _temper_index_redirect(token, str(exc), "bad")
    active = ", ".join(result.get("active_tempers") or []) or "none"
    return _temper_index_redirect(
        token, f"equipped temper '{temper_id}' · active: {active}", "info")


def handle_temper_unequip(state_root: Path, temper_id: str, token: str) -> str:
    result = tempers_mod.unequip(state_root, temper_id)
    active = ", ".join(result.get("active_tempers") or []) or "none"
    return _temper_index_redirect(
        token, f"unequipped temper '{temper_id}' · active: {active}", "info")


def handle_temper_reject(
    state_root: Path, armor: str, proposal_id: str, token: str,
) -> str:
    try:
        proposal = temper_mod.reject_proposal(state_root, armor, proposal_id)
    except (FileNotFoundError, temper_mod.TemperStateError) as exc:
        return _detail_redirect(armor, proposal_id, token, str(exc), "bad")
    return _detail_redirect(
        proposal.target_armor, proposal.proposal_id, token, "rejected", "info",
    )


def handle_temper_apply(
    state_root: Path, armor: str, proposal_id: str, token: str, body: dict,
) -> str:
    try:
        proposal = temper_mod.apply_proposal(
            state_root,
            armor,
            proposal_id,
            apply_mode=(body.get("apply_mode") or "apply_next_task"),
        )
    except (FileNotFoundError, temper_mod.TemperStateError) as exc:
        return _detail_redirect(armor, proposal_id, token, str(exc), "bad")
    return _detail_redirect(
        proposal.target_armor, proposal.proposal_id, token, "applied", "info",
    )


def handle_temper_activate(
    state_root: Path, armor: str, proposal_id: str, token: str,
) -> str:
    try:
        temper_mod.activate_temper(state_root, armor, proposal_id)
    except (FileNotFoundError, temper_mod.TemperStateError) as exc:
        return _detail_redirect(armor, proposal_id, token, str(exc), "bad")
    return _detail_redirect(
        armor, proposal_id, token, "active Temper slot updated", "info",
    )


def handle_temper_release(
    state_root: Path, armor: str, proposal_id: str, token: str,
) -> str:
    temper_mod.release_temper(state_root, armor, proposal_id)
    return _detail_redirect(
        armor, proposal_id, token, "Temper slot released", "info",
    )


def _temper_selection_script() -> str:
    return """
<script>
(function(){
  const form = document.querySelector('.temper-select-form');
  if (!form) return;
  const hidden = form.querySelector('[data-temper-selected-indexes]');
  const count = form.querySelector('[data-temper-count]');
  const boxes = Array.from(form.querySelectorAll('[data-temper-index]'));
  const cards = Array.from(form.querySelectorAll('.temper-select-card'));
  function sync(){
    const selected = boxes.filter(cb => cb.checked).map(cb => cb.dataset.temperIndex);
    hidden.value = selected.join(',');
    count.textContent = selected.length + ' selected';
  }
  boxes.forEach(cb => cb.addEventListener('change', sync));
  const all = form.querySelector('[data-temper-select-all]');
  if (all) all.addEventListener('click', () => { boxes.forEach(cb => cb.checked = true); sync(); });
  const clear = form.querySelector('[data-temper-clear]');
  if (clear) clear.addEventListener('click', () => { boxes.forEach(cb => cb.checked = false); sync(); });
  form.querySelectorAll('[data-temper-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      const value = btn.dataset.temperFilter;
      form.querySelectorAll('[data-temper-filter]').forEach(other => {
        other.classList.toggle('active', other === btn);
      });
      cards.forEach(card => {
        card.style.display = (value === 'all' || card.dataset.temperDestination === value) ? '' : 'none';
      });
    });
  });
  form.addEventListener('submit', sync);
  sync();
})();
</script>
"""


def _entry_preview(text: str, limit: int = 260) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _chip_list(label: str, values: object, *, limit: int = 6) -> str:
    if not isinstance(values, list):
        return ""
    items = [
        str(value).strip()
        for value in values
        if str(value).strip()
    ]
    if not items:
        return ""
    visible = items[:limit]
    overflow = len(items) - len(visible)
    chips = "".join(
        f'<span class="temper-context-chip">{e(item)}</span>'
        for item in visible
    )
    if overflow > 0:
        chips += f'<span class="temper-context-chip">+{overflow}</span>'
    return (
        '<div class="temper-context-row">'
        f'<b>{e(label)}</b>'
        f'<div>{chips}</div>'
        '</div>'
    )


def _temper_context_block(item: temper_mod.TemperItem) -> str:
    methodology = dict(item.methodology_context or {})
    control = dict(item.control or {})
    safety = dict(item.safety_constraints or {})
    rows: list[str] = []
    task_family = str(methodology.get("task_family") or "").strip()
    if task_family:
        rows.append(
            '<div class="temper-context-row temper-context-applies">'
            '<b>Applies when</b>'
            f'<div><span class="temper-context-chip">{e(task_family)}</span></div>'
            '</div>'
        )
    rows.append(_chip_list("Expected here", methodology.get("expected")))
    rows.append(_chip_list("Unexpected here", methodology.get("unexpected")))
    rows.append(_chip_list("Allowed here", control.get("allowed") or safety.get("allowed")))
    rows.append(_chip_list(
        "Restricted in this context",
        control.get("restricted") or safety.get("restricted"),
    ))
    rows.append(_chip_list(
        "Needs context",
        methodology.get("requires_context") or safety.get("requires_context"),
    ))
    rows = [row for row in rows if row]
    if not rows:
        return ""
    return '<div class="temper-context-block">' + "".join(rows) + '</div>'


def _render_entry_text(item: temper_mod.TemperItem) -> str:
    if item.control:
        try:
            return temper_controls.render_control(
                item.destination,
                dict(item.control),
                citations=item.citations,
                fallback_text=item.proposed_text,
            ).strip()
        except Exception:
            pass
    return item.proposed_text or item.rationale or ""


def _temper_item_card(
    item: temper_mod.TemperItem,
    *,
    index: int | None = None,
    checked: str = "",
) -> str:
    input_html = ""
    if index is not None:
        input_html = (
            f'<input type="checkbox"{checked} data-temper-index="{index}" '
            f'data-temper-destination="{e(item.destination)}">'
        )
    entry_text = _render_entry_text(item)
    context_html = _temper_context_block(item)
    lands_html = _temper_ingest_target_block(item)
    return (
        '<article class="ara-armor-info-card temper-select-card temper-entry-card" '
        f'data-temper-destination="{e(item.destination)}">'
        '<label class="temper-card-checkline">'
        f'{input_html}'
        '<span>'
        f'<span class="ara-chip">{e(item.destination)}</span>'
        f'<strong>{e(item.title)}</strong>'
        f'<small>{e(item.rationale)}</small>'
        '</span></label>'
        '<dl>'
        f'<dt>source</dt><dd>{e(item.source)}</dd>'
        f'<dt>risk</dt><dd>{e(item.risk)}</dd>'
        '</dl>'
        f'{context_html}'
        '<div class="temper-entry-full">'
        '<span>Proposed entry</span>'
        f'<pre>{e(entry_text)}</pre>'
        '</div>'
        f'{lands_html}'
        '</article>'
    )


def _temper_ingest_target_block(item: temper_mod.TemperItem) -> str:
    """Show where this entry lands on Ingest and whether it enforces."""
    try:
        plan = temper_ingest.plan_item(item)
    except Exception:  # noqa: BLE001 — never let a preview break the page
        return ""
    if plan.enforced:
        badge, colour = "enforced", "var(--accent)"
    else:
        badge, colour = "advisory", "var(--wb-warn, #c07b00)"
    note = (
        f'<small class="ara-sub" style="display:block;margin-top:4px">{e(plan.note)}</small>'
        if plan.note else ""
    )
    return (
        '<div class="temper-entry-full temper-ingest-target">'
        f'<span>Lands in <code>{e(plan.home)}</code> · '
        f'<b style="color:{colour}">{e(badge)}</b> · {e(plan.summary)}</span>'
        f'<pre>{e(plan.rule_text)}</pre>'
        f'{note}'
        '</div>'
    )


def _proposal_card(proposal: temper_mod.TemperProposal, token: str) -> str:
    href = (
        f"/temper/{urllib.parse.quote(proposal.target_armor)}/"
        f"{urllib.parse.quote(proposal.proposal_id)}?t={e(token)}"
    )
    destinations = Counter(item.destination for item in proposal.items)
    top_destinations = ", ".join(
        f"{name} {count}" for name, count in destinations.most_common(3)
    ) or "none"
    trims = ", ".join(proposal.source_trims) or "none"
    status_class = e(proposal.status.replace("_", "-"))
    sample_item = next((item for item in proposal.items if item.proposed_text), None)
    sample_entry = _entry_preview(
        sample_item.proposed_text if sample_item else proposal.operator_notes or proposal.mode,
        limit=220,
    )
    sample_label = sample_item.destination if sample_item else "proposal"
    return (
        f'<article class="ara-kit-card temper-proposal-card temper-status-{status_class}">'
        '<div class="ara-kit-card-top">'
        f'<span class="ara-kit-kind">{e(proposal.status)}</span>'
        f'<span class="ara-kit-glyph">{len(proposal.items)}</span>'
        '</div>'
        f'<h3>{e(proposal.proposal_id)}</h3>'
        f'<p>{e(proposal.operator_notes or proposal.mode)}</p>'
        '<div class="ara-kit-meta">'
        f'<div><span>armor</span><strong>{e(proposal.target_armor)}</strong></div>'
        f'<div><span>trims</span><strong>{e(trims)}</strong></div>'
        f'<div><span>forge</span><strong>{e(proposal.source_forge or "none")}</strong></div>'
        f'<div><span>destinations</span><strong>{e(top_destinations)}</strong></div>'
        '</div>'
        '<div class="temper-card-entry">'
        f'<span>sample entry · {e(sample_label)}</span>'
        f'<pre>{e(sample_entry)}</pre>'
        '</div>'
        '<div class="temper-card-foot">'
        f'<span>{len(proposal.items)} items</span>'
        f'<a class="ara-btn ara-btn-ghost" href="{href}">Review</a>'
        '</div>'
        '</article>'
    )


def _detail_digest(
    proposal: temper_mod.TemperProposal,
    active_trim: str | None,
    active_temper: str | None,
) -> str:
    counts = Counter(item.destination for item in proposal.items)
    destination_rows = "".join(
        f'<span><b>{e(name)}</b><strong>{count}</strong></span>'
        for name, count in counts.most_common()
    )
    if not destination_rows:
        destination_rows = '<span><b>none</b><strong>0</strong></span>'
    return (
        '<section class="temper-digest" aria-label="Temper proposal digest">'
        '<div class="temper-digest-card">'
        '<span class="ara-eyebrow">Proposal shape</span>'
        f'<strong>{len(proposal.items)} cards</strong>'
        f'<p>{len(counts)} destinations · status {e(proposal.status)}</p>'
        '</div>'
        '<div class="temper-digest-card">'
        '<span class="ara-eyebrow">Active slots</span>'
        f'<strong>{e(active_temper or "no Temper")}</strong>'
        f'<p>Trim: <code>{e(active_trim or "none")}</code></p>'
        '</div>'
        f'<div class="temper-destination-counts">{destination_rows}</div>'
        '</section>'
    )


def _detail_redirect(
    armor: str, proposal_id: str, token: str, flash: str, level: str,
) -> str:
    return (
        f"/temper/{urllib.parse.quote(armor)}/{urllib.parse.quote(proposal_id)}"
        f"?t={token}&flash={urllib.parse.quote(flash)}"
        f"&flash_level={urllib.parse.quote(level)}"
    )


def _index_redirect(token: str, flash: str, level: str) -> str:
    return (
        f"/temper/create?t={token}&flash={urllib.parse.quote(flash)}"
        f"&flash_level={urllib.parse.quote(level)}"
    )
