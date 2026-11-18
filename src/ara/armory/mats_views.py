from __future__ import annotations

import html
import json
import urllib.parse
from collections import defaultdict
from pathlib import Path

from .. import engagement_mats as em
from .. import trims as trims_mod
from . import views as base_views


def e(s: object) -> str:
    return html.escape(str(s)) if s is not None else ""


def _list_engagement_ids(state_root: Path) -> list[str]:
    eids: set[str] = set()
    root = em.engagements_root(state_root)
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir():
                eids.add(d.name)
    for trim in trims_mod.list_all_trims(state_root):
        if trim.engagement_id:
            eids.add(trim.engagement_id)
    return sorted(eids)


def _posture_for(state_root: Path, eid: str) -> str:
    for trim in trims_mod.list_all_trims(state_root):
        if trim.engagement_id == eid:
            return trim.posture
    return "unknown"


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def _count_by_class(records: list[em.MatRecord]) -> dict[str, int]:
    counts = {cls: 0 for cls in em.MAT_CLASSES}
    for rec in records:
        counts[rec.mat_class] = counts.get(rec.mat_class, 0) + 1
    return counts


def _bytes_by_class(records: list[em.MatRecord]) -> dict[str, int]:
    sizes = {cls: 0 for cls in em.MAT_CLASSES}
    for rec in records:
        sizes[rec.mat_class] = sizes.get(rec.mat_class, 0) + rec.bytes
    return sizes


def _group_by_class(records: list[em.MatRecord]) -> dict[str, list[em.MatRecord]]:
    grouped: dict[str, list[em.MatRecord]] = defaultdict(list)
    for rec in records:
        grouped[rec.mat_class].append(rec)
    for cls in list(grouped):
        grouped[cls].sort(key=lambda rec: rec.path)
    return grouped


def _file_type_label(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".md": "Markdown",
        ".markdown": "Markdown",
        ".json": "JSON",
        ".yaml": "YAML",
        ".yml": "YAML",
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".html": "HTML",
        ".css": "CSS",
        ".txt": "Text",
        ".pdf": "PDF",
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".csv": "CSV",
        ".xml": "XML",
    }.get(suffix, suffix[1:].upper() if suffix else "File")


def _flash_html(flash: str | None, flash_level: str) -> str:
    if not flash:
        return ""
    cls = "bad" if flash_level == "bad" else "ok"
    return f'<div class="ara-empty keep-flash keep-flash-{cls}">{e(flash)}</div>'


def _class_card(cls: str, count: int, size: int, *, detail: bool = False) -> str:
    state = "populated" if count else "empty"
    label = "ready" if count else "empty"
    return (
        f'<div class="keep-class-card keep-class-{e(state)}">'
        f'<span>{e(cls)}</span>'
        f'<strong>{count}</strong>'
        f'<small>{e(_fmt_bytes(size))}</small>'
        f'<em>{e(label)}</em>'
        f'{"<b>readable by gray/white trims when populated</b>" if detail else ""}'
        '</div>'
    )


def render_mats_index(
    state_root: Path, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    eids = _list_engagement_ids(state_root)
    rows: list[tuple[str, str, list[em.MatRecord], list[str]]] = []
    total_mats = 0
    total_bytes = 0
    aggregate = {cls: 0 for cls in em.MAT_CLASSES}
    aggregate_bytes = {cls: 0 for cls in em.MAT_CLASSES}
    for eid in eids:
        mats = em.list_mats(state_root, eid)
        classes = em.populated_classes(state_root, eid)
        rows.append((eid, _posture_for(state_root, eid), mats, classes))
        total_mats += len(mats)
        total_bytes += sum(rec.bytes for rec in mats)
        counts = _count_by_class(mats)
        sizes = _bytes_by_class(mats)
        for cls in em.MAT_CLASSES:
            aggregate[cls] += counts.get(cls, 0)
            aggregate_bytes[cls] += sizes.get(cls, 0)

    parts: list[str] = [
        '<section class="keep-hero">',
        '<div class="keep-hero-copy">',
        '<div class="ara-eyebrow">Keep</div>',
        '<h2 class="ara-h">KEEP</h2>',
        '<p class="ara-sub">Manage task materials used by trims, '
        'preflight checks, Bulwark read rules, and forge deposit bridging.</p>',
        _flash_html(flash, flash_level),
        '<form class="keep-open-form" method="get" action="/mats">',
        f'<input type="hidden" name="t" value="{e(token)}">',
        '<label>Open or create task'
        '<input type="text" name="eid" placeholder="acme-task" required>'
        '</label>',
        '<button class="ara-btn ara-btn-primary" type="submit">Open keep</button>',
        '</form>',
        '</div>',
        '<div class="keep-hero-stats">',
        f'<div><span>Tasks</span><strong>{len(eids)}</strong></div>',
        f'<div><span>MATS</span><strong>{total_mats}</strong></div>',
        f'<div><span>Indexed size</span><strong>{e(_fmt_bytes(total_bytes))}</strong></div>',
        '</div>',
        '</section>',
        '<section class="keep-flow-grid">',
        '<div><b>Paste upload</b><span>Text, markdown, JSON, notes, and specs land directly in the selected class.</span></div>',
        '<div><b>Disk drop + rescan</b><span>Binary files and trees are copied into the class folders, then indexed from disk.</span></div>',
        '<div><b>Trim policy</b><span>Only populated classes become readable to graybox and whitebox trims.</span></div>',
        '<div><b>Forge bridge</b><span>Forge deposits tied to a task are linked into source with forge/link tags.</span></div>',
        '</section>',
        '<section>',
        '<div class="keep-section-head">',
        '<div><div class="ara-eyebrow">Classes</div><h3>Material organization</h3></div>',
        '</div>',
        '<div class="keep-class-grid">',
    ]
    for cls in em.MAT_CLASSES:
        parts.append(_class_card(cls, aggregate[cls], aggregate_bytes[cls]))
    parts.extend([
        '</div>',
        '</section>',
    ])
    return base_views._page("KEEP", "keep", "".join(parts), token)


def render_mats_inventory(
    state_root: Path, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    eids = _list_engagement_ids(state_root)
    rows: list[tuple[str, str, list[em.MatRecord], list[str]]] = []
    total_mats = 0
    total_bytes = 0
    for eid in eids:
        mats = em.list_mats(state_root, eid)
        classes = em.populated_classes(state_root, eid)
        rows.append((eid, _posture_for(state_root, eid), mats, classes))
        total_mats += len(mats)
        total_bytes += sum(rec.bytes for rec in mats)

    parts: list[str] = [
        '<section>',
        '<div class="keep-section-head">',
        '<div><div class="ara-eyebrow">Barracks</div><h3>Known keeps</h3></div>',
        f'<a class="ara-btn ara-btn-ghost" href="/mats?t={e(token)}">Open KEEP</a>',
        '</div>',
        _flash_html(flash, flash_level),
        '<div class="keep-hero-stats" style="grid-template-columns:repeat(3,minmax(0,1fr));margin-bottom:16px">',
        f'<div><span>Tasks</span><strong>{len(eids)}</strong></div>',
        f'<div><span>MATS</span><strong>{total_mats}</strong></div>',
        f'<div><span>Indexed size</span><strong>{e(_fmt_bytes(total_bytes))}</strong></div>',
        '</div>',
    ]
    if not rows:
        parts.append(
            '<div class="ara-empty">No task keeps exist yet. '
            f'<a href="/mats?t={e(token)}">Open KEEP</a> to create one.</div>'
        )
    else:
        parts.append('<div class="keep-engagement-grid">')
        for eid, posture, mats, classes in rows:
            encoded = urllib.parse.quote(eid, safe="")
            parts.append(
                '<article class="keep-engagement-card">'
                f'<div><span>{e(posture)}</span><strong>{e(eid)}</strong></div>'
                f'<p>{len(mats)} mats · {e(_fmt_bytes(sum(rec.bytes for rec in mats)))}</p>'
                f'<small>{e(", ".join(classes) or "no populated classes")}</small>'
                f'<a class="ara-btn ara-btn-ghost" href="/mats/{encoded}?t={e(token)}">Open</a>'
                '</article>'
            )
        parts.append('</div>')
    parts.append('</section>')
    return base_views._page("MATS", "mats", "".join(parts), token)


def render_mats_detail(
    state_root: Path, eid: str, token: str, *,
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    eid = eid.strip()
    try:
        em.ensure_engagement(state_root, eid)
    except em.InvalidEngagementId as exc:
        return render_mats_index(
            state_root, token, flash=str(exc), flash_level="bad",
        )

    mats = em.list_mats(state_root, eid)
    counts = _count_by_class(mats)
    sizes = _bytes_by_class(mats)
    grouped = _group_by_class(mats)
    posture = _posture_for(state_root, eid)
    encoded = urllib.parse.quote(eid, safe="")
    mat_root = em.mats_root(state_root, eid)
    total_bytes = sum(rec.bytes for rec in mats)

    parts: list[str] = [
        '<section class="keep-hero">',
        '<div class="keep-hero-copy">',
        '<div class="ara-eyebrow">Keep</div>',
        f'<h2 class="ara-h">{e(eid)}</h2>',
        f'<p class="ara-sub">Posture: <code>{e(posture)}</code>. '
        'Organize task material by class, upload text directly, '
        'rescan disk drops, and remove stale entries.</p>',
        _flash_html(flash, flash_level),
        '<div class="keep-action-strip">',
        f'<form method="post" action="/mats/{encoded}/rescan?t={e(token)}">',
        '<button class="ara-btn ara-btn-primary" type="submit">Rescan disk</button>',
        '</form>',
        f'<a class="ara-btn ara-btn-ghost" href="/mats?t={e(token)}">Back to KEEP</a>',
        '</div>',
        '</div>',
        '<div class="keep-hero-stats">',
        f'<div><span>MATS</span><strong>{len(mats)}</strong></div>',
        f'<div><span>Classes</span><strong>{len([cls for cls, n in counts.items() if n])}</strong></div>',
        f'<div><span>Indexed size</span><strong>{e(_fmt_bytes(total_bytes))}</strong></div>',
        '</div>',
        '</section>',
        '<section>',
        '<div class="keep-section-head">',
        '<div><div class="ara-eyebrow">Classes</div><h3>Class inventory</h3></div>',
        '</div>',
        '<div class="keep-class-grid">',
    ]
    for cls in em.MAT_CLASSES:
        parts.append(_class_card(cls, counts[cls], sizes[cls], detail=True))
    parts.extend([
        '</div>',
        '</section>',
        '<section class="keep-layout">',
        '<div class="keep-upload-panel">',
        '<div class="keep-section-head compact">',
        '<div><div class="ara-eyebrow">Upload</div><h3>File upload</h3></div>',
        '</div>',
        f'<form class="ara-form" method="post" enctype="multipart/form-data" action="/mats/{encoded}/upload?t={e(token)}">',
        '<label>Class<select name="mat_class" required>',
    ])
    for cls in em.MAT_CLASSES:
        parts.append(f'<option value="{e(cls)}">{e(cls)}</option>')
    parts.extend([
        '</select></label>',
        '<label>File<input type="file" name="file" required></label>',
        '<label>Uploaded by<input type="text" name="uploaded_by" placeholder="operator"></label>',
        '<label>Note<input type="text" name="note" placeholder="source and handling context"></label>',
        '<button class="ara-btn ara-btn-primary" type="submit">Upload mat</button>',
        '</form>',
        '</div>',
        '<div class="keep-upload-panel">',
        '<div class="keep-section-head compact">',
        '<div><div class="ara-eyebrow">Paste</div><h3>Paste upload</h3></div>',
        '</div>',
        f'<form class="ara-form" method="post" action="/mats/{encoded}/paste?t={e(token)}">',
        '<label>Class<select name="mat_class" required>',
    ])
    for cls in em.MAT_CLASSES:
        parts.append(f'<option value="{e(cls)}">{e(cls)}</option>')
    parts.extend([
        '</select></label>',
        '<label>Filename<input type="text" name="filename" required placeholder="design-overview.md"></label>',
        '<label>Content<textarea name="content" rows="9" required placeholder="paste markdown, JSON, source snippets, notes, or specs"></textarea></label>',
        '<label>Uploaded by<input type="text" name="uploaded_by" placeholder="operator"></label>',
        '<label>Note<input type="text" name="note" placeholder="source and handling context"></label>',
        '<button class="ara-btn ara-btn-primary" type="submit">Add mat</button>',
        '</form>',
        '</div>',
        '<div class="keep-upload-panel">',
        '<div class="keep-section-head compact">',
        '<div><div class="ara-eyebrow">Disk</div><h3>Disk organization</h3></div>',
        '</div>',
        '<p class="ara-sub">Drop larger files into the class folders, then rescan.</p>',
        f'<code class="keep-path">{e(mat_root)}</code>',
        '<div class="keep-folder-grid">',
    ])
    for cls in em.MAT_CLASSES:
        parts.append(f'<code>{e(cls)}/</code>')
    parts.extend([
        '</div>',
        f'<form method="post" action="/mats/{encoded}/rescan?t={e(token)}">',
        '<button class="ara-btn ara-btn-primary" type="submit">Index disk changes</button>',
        '</form>',
        '<p class="keep-note">Rescan adds new files, updates changed hashes, and removes manifest entries for deleted files.</p>',
        '</div>',
        '</section>',
        '<section>',
        '<div class="keep-section-head">',
        '<div><div class="ara-eyebrow">Inventory</div><h3>Indexed MATS</h3></div>',
        '</div>',
    ])

    if not mats:
        parts.append(
            '<div class="ara-empty">No mats indexed yet. Use paste upload '
            'or disk organization above, then rescan.</div>'
        )
    for cls in em.MAT_CLASSES:
        records = grouped.get(cls, [])
        parts.append(
            f'<details class="keep-class-section" {"open" if records else ""}>'
            f'<summary><span>{e(cls)}</span><b>{len(records)}</b></summary>'
        )
        if not records:
            parts.append('<p class="ara-sub">No files indexed for this class.</p>')
        else:
            parts.append('<div class="keep-mat-list">')
            for rec in records:
                relencoded = urllib.parse.quote(rec.path, safe="")
                selected = em.mat_is_selected(rec)
                selection_label = "used" if selected else "excluded"
                selection_class = "ok" if selected else "warn"
                toggle_action = "exclude" if selected else "include"
                toggle_label = (
                    "Exclude from generation" if selected
                    else "Use for generation"
                )
                tags = "".join(
                    f'<span class="ara-pill">{e(tag)}</span>'
                    for tag in rec.tags
                ) or '<span class="ara-pill">untagged</span>'
                parts.append(
                    '<article class="keep-mat-row">'
                    '<div class="keep-mat-main">'
                    '<div class="keep-file-title">'
                    f'<code>{e(rec.path)}</code>'
                    '<div class="keep-file-meta">'
                    '<span class="ara-pill ok">indexed</span>'
                    f'<span class="ara-pill {selection_class}">{selection_label}</span>'
                    f'<span class="ara-pill">{e(_fmt_bytes(rec.bytes))}</span>'
                    f'<span class="ara-pill">{e(_file_type_label(rec.path))}</span>'
                    '</div>'
                    '</div>'
                    f'<span>sha256 {e(rec.sha256[:12])}</span>'
                    f'<small>{e(rec.uploaded_at)} · {e(rec.uploaded_by) or "unknown uploader"}</small>'
                    f'{"<p>" + e(rec.note) + "</p>" if rec.note else ""}'
                    '</div>'
                    f'<div class="keep-mat-tags">{tags}</div>'
                    '<div class="keep-mat-actions">'
                    f'<form method="post" action="/mats/{encoded}/{relencoded}/{toggle_action}?t={e(token)}">'
                    f'<button class="ara-btn ara-btn-ghost" type="submit">{toggle_label}</button>'
                    '</form>'
                    f'<form method="post" action="/mats/{encoded}/{relencoded}/delete?t={e(token)}" '
                    f'onsubmit="return confirm(\'Delete {e(rec.path)}?\');">'
                    '<button class="ara-btn ara-btn-ghost keep-delete" type="submit">Delete file</button>'
                    '</form>'
                    '</div>'
                    '</article>'
                )
            parts.append('</div>')
        parts.append('</details>')

    parts.extend([
        '<details class="keep-manifest">',
        '<summary>Raw manifest</summary>',
        '<pre>',
        e(json.dumps(em.load_manifest(state_root, eid), indent=2, sort_keys=True)),
        '</pre>',
        '</details>',
        '</section>',
    ])
    return base_views._page(f"KEEP · {eid}", "keep", "".join(parts), token)


def _back_to_eid(eid: str, token: str, *, flash: str | None = None,
                 level: str = "info") -> str:
    encoded = urllib.parse.quote(eid, safe="")
    out = f"/mats/{encoded}?t={token}"
    if flash:
        out += f"&flash={urllib.parse.quote(flash)}"
    if level != "info":
        out += f"&flash_level={level}"
    return out


def handle_mats_paste(
    state_root: Path, eid: str, body: dict, token: str,
) -> str:
    mat_class = (body.get("mat_class") or "").strip()
    filename = (body.get("filename") or "").strip()
    content = body.get("content") or ""
    if not mat_class or not filename or not content:
        return _back_to_eid(eid, token,
                            flash="class, filename, and content all required",
                            level="bad")
    try:
        rec = em.add_mat_bytes(
            state_root, eid, mat_class, filename,
            content.encode("utf-8"),
            uploaded_by=body.get("uploaded_by") or "",
            note=body.get("note") or "",
        )
    except (em.InvalidMatClass, em.InvalidEngagementId) as exc:
        return _back_to_eid(eid, token, flash=str(exc), level="bad")
    return _back_to_eid(eid, token, flash=f"added {rec.path}")


def handle_mats_upload(
    state_root: Path, eid: str, body: dict, token: str,
) -> str:
    mat_class = (body.get("mat_class") or "").strip()
    filename = (body.get("file_filename") or body.get("filename") or "").strip()
    blob = body.get("file")
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    if not mat_class or not filename or not isinstance(blob, (bytes, bytearray)):
        return _back_to_eid(eid, token,
                            flash="class and file are required",
                            level="bad")
    if not blob:
        return _back_to_eid(eid, token, flash="file is empty", level="bad")
    try:
        rec = em.add_mat_bytes(
            state_root, eid, mat_class, filename, bytes(blob),
            uploaded_by=str(body.get("uploaded_by") or ""),
            note=str(body.get("note") or ""),
        )
    except (em.InvalidMatClass, em.InvalidEngagementId) as exc:
        return _back_to_eid(eid, token, flash=str(exc), level="bad")
    return _back_to_eid(eid, token, flash=f"added {rec.path}")


def handle_mats_rescan(
    state_root: Path, eid: str, token: str,
) -> str:
    try:
        res = em.rescan_disk(state_root, eid)
    except em.InvalidEngagementId as exc:
        return _back_to_eid(eid, token, flash=str(exc), level="bad")
    return _back_to_eid(eid, token, flash=(
        f'rescan: +{res["added"]} added, '
        f'~{res["updated"]} updated, '
        f'-{res["removed"]} removed'
    ))


def handle_mats_delete(
    state_root: Path, eid: str, relpath: str, token: str,
) -> str:
    gone = em.remove_mat(state_root, eid, relpath)
    if not gone:
        return _back_to_eid(eid, token, flash="mat not found", level="bad")
    return _back_to_eid(eid, token, flash=f"deleted {relpath}")


def handle_mats_selection(
    state_root: Path, eid: str, relpath: str, token: str, *,
    selected: bool,
) -> str:
    changed = em.set_mat_selected(state_root, eid, relpath, selected)
    if not changed:
        return _back_to_eid(eid, token, flash="mat not found", level="bad")
    verb = "using" if selected else "excluded"
    return _back_to_eid(eid, token, flash=f"{verb} {relpath}")


__all__ = [
    "render_mats_index", "render_mats_inventory", "render_mats_detail",
    "handle_mats_paste", "handle_mats_upload",
    "handle_mats_rescan", "handle_mats_delete", "handle_mats_selection",
]
