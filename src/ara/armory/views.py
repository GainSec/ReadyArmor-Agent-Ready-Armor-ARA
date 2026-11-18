"""Server-rendered HTML views. Intentionally unstyled — structure only.

Every function returns a complete HTML document string. Keeping the HTML
minimal and semantic so a themer can attach CSS (later) without wrestling
with structural choices. No classes beyond `page-<name>` on <body> for
per-page hooks.
"""

from __future__ import annotations

import html
import hashlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Iterable

from .. import approvals as approvals_mod
from .. import armors as armors_mod
from .. import overrides as overrides_mod
from .. import process_state as process_state_mod
from .. import temper as temper_mod
from .. import trims as trims_mod
from .. import weapons as weapons_mod
from ..walls import Walls, parse_file, validate_file
from . import docs_content


# ---------- helpers ----------

def e(s: object) -> str:
    return html.escape(str(s)) if s is not None else ""


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_ago(ts: float | None) -> str:
    if not ts:
        return ""
    dt = max(0, int(time.time() - ts))
    if dt < 60:
        return f"{dt}s"
    if dt < 3600:
        return f"{dt // 60}m"
    return f"{dt // 3600}h"


def _fmt_latency(seconds: float | None) -> str:
    """Compact duration: sub-second as ms, then s/m/h."""
    if seconds is None or seconds < 0:
        return ""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60)}m"


def _age_attention(age_s: float) -> str:
    """CSS class for pending-age urgency bucket."""
    if age_s >= 60:
        return "ara-row-attention ara-row-hot"
    if age_s >= 15:
        return "ara-row-attention ara-row-warm"
    return "ara-row-attention"


_RHEMA_LIVE_MAX_TURNS = 200
_RHEMA_LIVE_SESSION_SCAN_LIMIT = 12
_RHEMA_TRANSCRIPT_TAIL_LINES = 320
_RHEMA_TRANSCRIPT_TAIL_BYTES = 1024 * 1024
_RHEMA_INLINE_DETAIL_MAX_BYTES = 32 * 1024


def _json_size_hint(obj: object) -> int:
    try:
        return len(json.dumps(obj, default=str))
    except Exception:
        return len(str(obj))


def _read_jsonl_tail_with_offsets(
    path: Path,
    *,
    max_lines: int = _RHEMA_TRANSCRIPT_TAIL_LINES,
    max_bytes: int = _RHEMA_TRANSCRIPT_TAIL_BYTES,
) -> list[tuple[int, dict]]:
    """Read a bounded JSONL tail and preserve byte offsets for lazy detail.

    Rhema transcripts can be hundreds of MB. The live page only needs recent
    turns, so initial render must not read every historical transcript byte.
    """
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    rows: deque[tuple[int, dict]] = deque(maxlen=max_lines)
    try:
        with path.open("rb") as f:
            start = max(0, size - max_bytes)
            f.seek(start)
            if start:
                f.readline()
            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rows.append((offset, rec))
    except OSError:
        return []
    return list(rows)


def _read_jsonl_at_offset(path: Path, offset: int) -> dict | None:
    if offset < 0 or not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            f.seek(offset)
            line = f.readline()
    except OSError:
        return None
    try:
        rec = json.loads(line.decode("utf-8", "replace"))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def _read_jsonl_line(path: Path, line_idx: int) -> dict | None:
    if line_idx < 0 or not path.is_file():
        return None
    try:
        with path.open() as f:
            for idx, line in enumerate(f):
                if idx != line_idx:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    return None
                return rec if isinstance(rec, dict) else None
    except OSError:
        return None
    return None


# Nav organized by role. Each tuple: (group_label, [(key, label, href), ...]).
# Group labels are ARA-flavoured one-word roles; page names keep their
# existing identities. Adding/reordering a page = one list edit.
_NAV_GROUPS = [
    ("Live", [
        ("status",      "Status",      "/"),
        ("courtyard",   "Courtyard",   "/courtyard"),
        ("engagement",  "Task",        "/engagement"),
    ]),
    ("Create", [
        ("forge",       "Forge",       "/forge"),
        ("sew",        "Sew",        "/trims/derive"),
        ("temper_create", "Tailor",   "/temper/create"),
        ("keep",       "KEEP",       "/mats"),
    ]),
    ("Barracks", [
        ("weapons",     "Weapons",     "/weapons"),
        ("armors",     "Armor",      "/armors"),
        ("packs",      "Packs",      "/forge/packs"),
        ("trims",      "Trims",      "/trims"),
        ("temper",     "Tempers",    "/temper"),
        ("mats",       "MATS",       "/mats/inventory"),
        ("tools",      "Tools",      "/tools"),
        ("tod",        "TOD",        "/tod"),
        ("bulwark",    "Bulwark",    "/bulwark"),
        ("danger",     "Danger",     "/danger"),
        ("rhema",      "Rhema",      "/rhema"),
    ]),
    ("Evidence", [
        ("spoils",     "Spoils",     "/engagement/spoils"),
        ("debug",      "Debug",      "/debug"),
        ("settings",   "Settings",   "/settings"),
        ("docs",       "Docs",       "/docs"),
    ]),
]

# Flat list derived from groups — kept for any callsite that still wants
# a linear iteration (e.g. active-key lookup, backwards compat).
_NAV_ITEMS = [item for _, items in _NAV_GROUPS for item in items]

_SIGIL_SVG = (
    '<svg class="sigil" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2 L20 6 V13 C20 18 16 21 12 22 C8 21 4 18 4 13 V6 Z"/>'
    '<path d="M12 8 V16 M8 12 H16"/></svg>'
)

_STATIC_REV = "20260617-tailor-create-1"


def _static_css_href(name: str, token: str) -> str:
    return f"/static/{e(name)}.css?t={e(token)}&v={_STATIC_REV}"


def _nav(active: str, token: str) -> str:
    """Render the sidebar with role-grouped headers.

    Groups come from ``_NAV_GROUPS``; each gets a small section header
    (``.ara-side-group``) so operators know which tool does what at a
    glance. On mobile (< 900px) groups collapse into a single horizontal
    scroll strip — the group headers hide, the links stay (CSS-driven).
    """
    out = ['<aside class="ara-side"><nav>',
           '<div class="heading">Armory</div>']
    for group_label, items in _NAV_GROUPS:
        out.append(
            f'<div class="ara-side-group">{e(group_label)}</div>'
        )
        for key, label, href in items:
            cls = ' class="active"' if key == active else ""
            out.append(f'<a href="{href}?t={e(token)}"{cls}>{e(label)}</a>')
    out.append('</nav></aside>')
    return "".join(out)


def _page(title: str, active: str, body: str, token: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>ARA · {e(title)}</title>"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">'
        f'<link rel="stylesheet" href="{_static_css_href("armory", token)}">'
        "</head>"
        f'<body class="page-{e(active)}">'
        '<header class="ara-top">'
        f'<div class="brand">{_SIGIL_SVG}ARA</div>'
        '<div class="status-cluster">'
        '<span><span class="dot-ok"></span>armed</span>'
        '<form method="post" action="/logout" '
        'style="display:inline;margin:0">'
        '<button type="submit" class="ara-btn ara-btn-ghost" '
        'title="Sign out — clears the armory_token cookie" '
        'style="padding:2px 8px;font-size:10px">sign out</button>'
        '</form>'
        '</div>'
        '</header>'
        f'{_nav(active, token)}'
        f'<main class="ara-main">{body}</main>'
        "</body></html>"
    )


def _render_tab_bar(
    tabs: Iterable[tuple[str, str, str]],
    active_key: str,
    *,
    label: str,
) -> str:
    out = [f'<nav class="ara-tab-bar" aria-label="{e(label)}">']
    for key, text, href in tabs:
        cls = "ara-tab ara-tab-active" if key == active_key else "ara-tab"
        current = ' aria-current="page"' if key == active_key else ""
        out.append(
            f'<a class="{cls}" href="{href}"{current}>{e(text)}</a>'
        )
    out.append("</nav>")
    return "".join(out)


# ---------- status ----------

def _top_n(
    hits: list[dict], *, key_fn, predicate=None, n: int = 5,
) -> list[tuple[str, dict]]:
    """Bucket hits by key_fn, count verdicts, return top N by total count."""
    out: dict[str, dict] = {}
    for r in hits:
        if predicate and not predicate(r):
            continue
        k = key_fn(r)
        if not k:
            continue
        e_ = out.setdefault(k, {"count": 0, "allow": 0, "deny": 0, "approval": 0,
                                "last_ts": 0.0, "sample_rule": ""})
        e_["count"] += 1
        v = r.get("verdict") or ""
        if v in ("allow", "deny", "approval"):
            e_[v] += 1
        ts = r.get("ts") or 0
        if ts > e_["last_ts"]:
            e_["last_ts"] = ts
        if not e_["sample_rule"] and r.get("rule_id"):
            e_["sample_rule"] = r["rule_id"]
    ranked = sorted(out.items(), key=lambda kv: kv[1]["count"], reverse=True)
    return ranked[:n]


def _render_top_n_table(
    heading: str, subtitle: str, rows: list[tuple[str, dict]],
    *, key_label: str,
) -> str:
    if not rows:
        return (
            f'<h3 class="ara-eyebrow" style="margin-top:22px">{e(heading)}</h3>'
            f'<p class="ara-sub">{e(subtitle)}</p>'
            '<div class="ara-empty">No activity in the last hour.</div>'
        )
    body_rows = []
    for key, stats in rows:
        # Tiny allow/deny breakdown bar.
        total = stats["count"] or 1
        allow_pct = stats["allow"] / total * 100
        deny_pct = stats["deny"] / total * 100
        appr_pct = stats["approval"] / total * 100
        bar = (
            '<div class="ara-breakdown-bar" style="width:100%;height:6px;'
            'background:color-mix(in srgb,var(--outline-variant) 25%,transparent);'
            'border-radius:3px;overflow:hidden;display:flex">'
            f'<div style="width:{deny_pct:.1f}%;background:var(--error)"></div>'
            f'<div style="width:{appr_pct:.1f}%;background:#d4a53a"></div>'
            f'<div style="width:{allow_pct:.1f}%;background:var(--tertiary)"></div>'
            '</div>'
        )
        body_rows.append(
            '<tr>'
            f'<td class="mono" data-label="{e(key_label)}" style="max-width:40ch;overflow:hidden;'
            f'text-overflow:ellipsis;white-space:nowrap">{e(key)}</td>'
            f'<td class="num" data-label="hits">{stats["count"]}</td>'
            f'<td style="min-width:120px" data-label="mix">{bar}</td>'
            f'<td class="num verdict-deny" data-label="deny">{stats["deny"]}</td>'
            f'<td class="num verdict-approval" data-label="appr">{stats["approval"]}</td>'
            f'<td class="num verdict-allow" data-label="allow">{stats["allow"]}</td>'
            f'<td class="mono ara-sub" data-label="last">{e(_fmt_ago(stats["last_ts"]))}</td>'
            '</tr>'
        )
    return (
        f'<h3 class="ara-eyebrow" style="margin-top:22px">{e(heading)}</h3>'
        f'<p class="ara-sub">{e(subtitle)}</p>'
        '<table class="ara-table">'
        f'<thead><tr><th>{e(key_label)}</th><th class="num">hits</th>'
        '<th>mix</th><th class="num">deny</th><th class="num">appr</th>'
        '<th class="num">allow</th><th>last</th></tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table>'
    )


def _render_incident_feed(
    hits: list[dict], *, n: int = 30, token: str | None = None,
) -> str:
    """Timeline strip of recent deny+approval events, newest first."""
    incidents = [
        r for r in hits
        if r.get("verdict") in ("deny", "approval")
    ]
    incidents.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    incidents = incidents[:n]
    if not incidents:
        return (
            '<h3 class="ara-eyebrow">Incidents (1h)</h3>'
            '<div class="ara-empty">Quiet. No denies or approval prompts in the last hour.</div>'
        )
    # Render as a dot-timeline: one row of colored dots, time ascending left→right.
    ordered = list(reversed(incidents))
    now = time.time()
    dots: list[str] = []
    for r in ordered:
        ts = r.get("ts") or 0
        age = int(now - ts)
        verdict = r.get("verdict") or ""
        color = "var(--error)" if verdict == "deny" else "#d4a53a"
        target = r.get("path") or r.get("command") or r.get("touched") or ""
        rule = r.get("rule_id") or ""
        tip = f"{verdict.upper()} · {rule} · {target} · {_fmt_ts(ts)} ({age}s ago)"
        dots.append(
            '<span class="ara-incident-dot" '
            f'style="background:{color}" title="{e(tip)}"></span>'
        )
    # Text log: last 8 entries fully rendered.
    feed_rows = []
    for r in incidents[:8]:
        verdict = r.get("verdict") or ""
        cls = "verdict-deny" if verdict == "deny" else "verdict-approval"
        feed_rows.append(
            '<li class="ara-incident-row">'
            f'<span class="mono ara-sub" style="width:6ch;display:inline-block">'
            f'{e(_fmt_ago(r.get("ts")))}</span>'
            f'<span class="{cls}" style="width:10ch;display:inline-block;'
            'font-family:var(--font-mono);font-size:11px">'
            f'{e(verdict.upper())}</span>'
            + (
                f'<a class="mono" href="/rules/{__import__("urllib").parse.quote(r.get("rule_id") or "", safe="")}?t={e(token)}" '
                f'style="font-size:12px;color:inherit">{e(r.get("rule_id") or "")}</a>'
                if token and r.get("rule_id")
                else f'<span class="mono" style="font-size:12px">{e(r.get("rule_id") or "")}</span>'
            )
            +
            f'<span class="mono" style="opacity:0.75;font-size:12px;margin-left:8px">'
            f'{e(r.get("path") or r.get("command") or r.get("touched") or "")}</span>'
            '</li>'
        )
    return (
        '<h3 class="ara-eyebrow">Incidents (1h)</h3>'
        '<div class="ara-incident-strip">'
        + "".join(dots)
        + '</div>'
        '<ul class="ara-incident-feed">'
        + "".join(feed_rows)
        + '</ul>'
    )


def _render_status_overview(hits: list[dict], token: str) -> str:
    # --- verdict counts over the window ---
    counts = {"allow": 0, "approval": 0, "deny": 0}
    for r in hits:
        v = r.get("verdict") or ""
        if v in counts:
            counts[v] += 1
    summary = (
        '<dl class="ara-dl ara-overview-counts">'
        f'<dt>allow</dt><dd class="verdict-allow">{counts["allow"]}</dd>'
        f'<dt>approval</dt><dd class="verdict-approval">{counts["approval"]}</dd>'
        f'<dt>deny</dt><dd class="verdict-deny">{counts["deny"]}</dd>'
        f'<dt>total</dt><dd>{sum(counts.values())}</dd>'
        '</dl>'
    )

    top_paths = _top_n(
        hits, n=5,
        key_fn=lambda r: r.get("path") or r.get("touched") or "",
        predicate=lambda r: (r.get("op") or "").startswith("fs."),
    )
    top_cmds = _top_n(
        hits, n=5,
        key_fn=lambda r: (r.get("command") or "").split("/")[-1] or r.get("command") or "",
        predicate=lambda r: r.get("op") == "exec.spawn" and r.get("verdict") == "deny",
    )
    top_nets = _top_n(
        hits, n=5,
        key_fn=lambda r: (
            f"{r.get('host','')}:{r.get('port','')}" if r.get("host")
            else r.get("touched") or ""
        ),
        predicate=lambda r: (r.get("op") or "") == "net.connect",
    )

    return (
        '<section>'
        '<div class="ara-eyebrow">Last hour</div>'
        '<h2 class="ara-h">Overview</h2>'
        + summary
        + _render_incident_feed(hits, token=token)
        + _render_top_n_table(
            "Top touched paths", "File paths hit by hooks.", top_paths,
            key_label="path",
        )
        + _render_top_n_table(
            "Top denied commands", "Spawn attempts blocked in the last hour.", top_cmds,
            key_label="command",
        )
        + _render_top_n_table(
            "Top connect targets", "Outbound host:port attempts.", top_nets,
            key_label="host:port",
        )
        + '</section>'
    )


_GRAPH_JS = """
<script>
(function() {
  const host = document.getElementById('ara-graph');
  if (!host) return;
  const token = new URLSearchParams(location.search).get('t') || '';
  let data;
  try { data = JSON.parse(host.dataset.graph); } catch (e) { return; }

  const W = host.clientWidth || 960;
  const H = 600;
  const SVGNS = 'http://www.w3.org/2000/svg';

  const nodes = [];
  const idToIdx = new Map();
  for (const r of data.rules) {
    idToIdx.set('r:' + r.id, nodes.length);
    nodes.push({
      kind: 'rule',
      id: r.id,
      label: r.short_label || r.id,
      pattern: r.pattern || '',
      count: r.count,
      last_ts: r.last_ts || 0,
      verdict: r.top_verdict,
      verdict_counts: r.verdict_counts || {},
      shape: r.shape || 'circle',
      color: r.color || '',
      kind_bucket: r.kind || 'other',
      x: W * 0.5 + (Math.random() - 0.5) * 60,
      y: H * 0.5 + (Math.random() - 0.5) * 60,
      vx: 0, vy: 0, pinned: false,
    });
  }
  for (const t of data.targets) {
    idToIdx.set('t:' + t.id, nodes.length);
    const a = Math.random() * Math.PI * 2;
    const r = 180 + Math.random() * 180;
    nodes.push({
      kind: 'target',
      id: t.id,
      label: t.label,
      full: t.full || t.id,
      count: t.count,
      last_ts: t.last_ts || 0,
      rules: t.rules || [],
      verdict_counts: t.verdict_counts || {},
      x: W * 0.5 + Math.cos(a) * r,
      y: H * 0.5 + Math.sin(a) * r,
      vx: 0, vy: 0, pinned: false,
    });
  }
  const edges = data.edges.map(e => ({
    a: idToIdx.get('r:' + e.rule),
    b: idToIdx.get('t:' + e.target),
    w: e.count,
    verdict: e.verdict,
  })).filter(e => e.a !== undefined && e.b !== undefined);

  // Adjacency for neighbor-filtering on click.
  const neighbors = new Map();
  for (const e of edges) {
    if (!neighbors.has(e.a)) neighbors.set(e.a, new Set());
    if (!neighbors.has(e.b)) neighbors.set(e.b, new Set());
    neighbors.get(e.a).add(e.b);
    neighbors.get(e.b).add(e.a);
  }

  const COLOR = {
    deny: '#dd6054',
    approval: '#d4a53a',
    allow: '#4edea3',
    mixed: '#8a98a8',
  };

  // --- Viewport state (zoom/pan) and selection ---
  const view = { scale: 1, tx: 0, ty: 0 };
  let selected = null;   // node index or null
  let hover = null;      // node index or null
  let dragNode = null;   // drag individual node
  let panning = false;
  let panStart = { x: 0, y: 0, tx: 0, ty: 0 };
  let needsRender = true;

  // Build persistent SVG skeleton so updates are cheap (DOM, not innerHTML).
  host.innerHTML = '';
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', String(H));
  svg.setAttribute('class', 'ara-graph-svg');
  const gAll = document.createElementNS(SVGNS, 'g');
  svg.appendChild(gAll);
  const gEdges = document.createElementNS(SVGNS, 'g');
  const gNodes = document.createElementNS(SVGNS, 'g');
  gAll.appendChild(gEdges);
  gAll.appendChild(gNodes);
  host.appendChild(svg);

  // Pre-create one <line> per edge and one <g> per node.
  const edgeEls = edges.map((e) => {
    const l = document.createElementNS(SVGNS, 'line');
    const t = document.createElementNS(SVGNS, 'title');
    const sids = (e.sessions || []).map(s => String(s).slice(0, 10)).join(', ');
    t.textContent =
      `${nodes[e.a].label} → ${nodes[e.b].label}\n`
      + `${e.count} hits · ${e.verdict}`
      + (sids ? `\nsessions: ${sids}` : '');
    l.appendChild(t);
    gEdges.appendChild(l);
    return l;
  });
  const nodeEls = nodes.map((n, idx) => {
    const g = document.createElementNS(SVGNS, 'g');
    g.setAttribute('class', 'ara-node');
    g.dataset.idx = String(idx);
    // Shape-based rule glyph; targets stay as circles.
    const shape = n.kind === 'rule' ? (n.shape || 'circle') : 'circle';
    let c;
    if (shape === 'square')   c = document.createElementNS(SVGNS, 'rect');
    else if (shape === 'triangle') c = document.createElementNS(SVGNS, 'polygon');
    else                      c = document.createElementNS(SVGNS, 'circle');
    g.appendChild(c);
    const title = document.createElementNS(SVGNS, 'title');
    g.appendChild(title);
    // Target nodes keep their label text; rule labels move to the side panel
    // per operator request (shapes + color carry the identity at-a-glance).
    let text = null;
    if (n.kind !== 'rule') {
      text = document.createElementNS(SVGNS, 'text');
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('font-family', 'var(--font-mono)');
      text.setAttribute('font-size', '9');
      text.setAttribute('fill', 'currentColor');
      text.setAttribute('pointer-events', 'none');
      text.textContent = n.label;
      g.appendChild(text);
    }
    gNodes.appendChild(g);
    return { g, c, title, text, shape };
  });

  function applyTransform() {
    gAll.setAttribute(
      'transform',
      `translate(${view.tx} ${view.ty}) scale(${view.scale})`,
    );
  }

  // --- Filter state (client-side) ---
  const filterState = {
    verdicts: new Set(['deny', 'approval', 'allow']),
    kinds:    new Set(['path', 'command', 'net', 'other']),
  };

  function passesFilter(idx) {
    const n = nodes[idx];
    if (n.kind === 'rule') {
      return filterState.kinds.has(n.kind_bucket || 'other');
    }
    // target node: visible if at least one connected edge passes filters.
    const nb = neighbors.get(idx);
    if (!nb) return true;
    for (const e of edges) {
      if ((e.a === idx || e.b === idx) && edgeFilterPass(e)) return true;
    }
    return false;
  }

  function edgeFilterPass(e) {
    if (!filterState.verdicts.has(e.verdict)) return false;
    const rNode = nodes[e.a].kind === 'rule' ? nodes[e.a] : nodes[e.b];
    if (!filterState.kinds.has(rNode.kind_bucket || 'other')) return false;
    return true;
  }

  function isVisible(nIdx) {
    if (!passesFilter(nIdx)) return false;
    if (selected == null) return true;
    if (selected === nIdx) return true;
    const nb = neighbors.get(selected);
    return nb ? nb.has(nIdx) : false;
  }

  function isEdgeVisible(e) {
    if (!edgeFilterPass(e)) return false;
    return isVisible(e.a) && isVisible(e.b);
  }

  function render() {
    // Edges
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i];
      const a = nodes[e.a], b = nodes[e.b];
      const el = edgeEls[i];
      el.setAttribute('x1', a.x.toFixed(1));
      el.setAttribute('y1', a.y.toFixed(1));
      el.setAttribute('x2', b.x.toFixed(1));
      el.setAttribute('y2', b.y.toFixed(1));
      el.setAttribute('stroke', COLOR[e.verdict] || COLOR.mixed);
      el.setAttribute('stroke-width',
        Math.max(0.6, Math.log(1 + e.w) * 0.9).toFixed(1));
      const vis = isEdgeVisible(e);
      el.setAttribute('stroke-opacity', vis ? '0.38' : '0.06');
    }
    // Nodes
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const els = nodeEls[i];
      const { g, c, title, text } = els;
      const vis = isVisible(i);
      const isHover = hover === i;
      const isSel = selected === i;
      g.setAttribute('opacity', vis ? (isHover || isSel ? '1' : '0.95') : '0.15');
      if (n.kind === 'rule') {
        const r = Math.max(7, Math.min(22, 4 + Math.log(1 + n.count) * 3));
        const size = isSel ? r + 2 : r;
        const col = n.color || COLOR[n.verdict] || COLOR.mixed;
        const shape = els.shape;
        if (shape === 'square') {
          c.setAttribute('x', (n.x - size).toFixed(1));
          c.setAttribute('y', (n.y - size).toFixed(1));
          c.setAttribute('width',  (size * 2).toFixed(1));
          c.setAttribute('height', (size * 2).toFixed(1));
          c.setAttribute('rx', '2');
        } else if (shape === 'triangle') {
          const h = size * 1.15;
          const pts = [
            [n.x,          n.y - h],
            [n.x - h * 0.95, n.y + h * 0.75],
            [n.x + h * 0.95, n.y + h * 0.75],
          ].map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
          c.setAttribute('points', pts);
        } else {
          c.setAttribute('cx', n.x.toFixed(1));
          c.setAttribute('cy', n.y.toFixed(1));
          c.setAttribute('r', size.toFixed(1));
        }
        c.setAttribute('fill', col);
        c.setAttribute('fill-opacity', isHover || isSel ? '1' : '0.88');
        c.setAttribute('stroke', col);
        c.setAttribute('stroke-width', isSel ? '2.4' : '1.2');
      } else {
        const r = Math.max(2.5, Math.min(8, 2 + Math.log(1 + n.count)));
        c.setAttribute('cx', n.x.toFixed(1));
        c.setAttribute('cy', n.y.toFixed(1));
        c.setAttribute('r', r.toFixed(1));
        c.setAttribute('fill', 'currentColor');
        c.setAttribute('fill-opacity', isHover || isSel ? '0.78' : '0.32');
        c.setAttribute('stroke', 'none');
        // Target labels: always shown for small graphs, else only hover/sel.
        const showLabel = (data.targets.length < 30) || isHover || isSel ||
                          (selected != null && isVisible(i));
        text.setAttribute('x', n.x.toFixed(1));
        text.setAttribute('y', (n.y + r + 11).toFixed(1));
        text.setAttribute('opacity', showLabel ? '0.85' : '0');
      }
      title.textContent = n.kind === 'target'
        ? `${n.full || n.label} (${n.count} hits)`
        : `${n.label} · ${n.pattern || '(no pattern)'} (${n.count} hits)`;
    }
    renderPanel();
    needsRender = false;
  }

  function fmtTs(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function fmtAgo(ts) {
    if (!ts) return '—';
    const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s/60) + 'm';
    return Math.floor(s/3600) + 'h';
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
  }
  function verdictBar(vc) {
    const a = (vc && vc.allow) || 0;
    const d = (vc && vc.deny) || 0;
    const p = (vc && vc.approval) || 0;
    const total = Math.max(1, a + d + p);
    const pctD = (d/total*100).toFixed(1);
    const pctP = (p/total*100).toFixed(1);
    const pctA = (a/total*100).toFixed(1);
    return (
      '<div class="ara-graph-vbar">'
      + `<div style="width:${pctD}%;background:${COLOR.deny}"></div>`
      + `<div style="width:${pctP}%;background:${COLOR.approval}"></div>`
      + `<div style="width:${pctA}%;background:${COLOR.allow}"></div>`
      + '</div>'
      + '<span class="ara-sub" style="font-size:11px">'
      + `deny ${d} · appr ${p} · allow ${a}</span>`
    );
  }

  const panel = document.getElementById('ara-graph-panel');
  function renderPanel() {
    if (!panel) return;
    if (selected == null) {
      panel.innerHTML =
        '<div class="ara-sub">Click a node to see details. Scroll to zoom, '
        + 'drag empty space to pan, drag a node to move it, double-click a rule '
        + 'to drill in.</div>';
      return;
    }
    const n = nodes[selected];
    if (n.kind === 'rule') {
      const nbrs = Array.from(neighbors.get(selected) || []);
      const targetRows = nbrs
        .map(i => nodes[i])
        .filter(nn => nn.kind === 'target')
        .sort((a, b) => b.count - a.count)
        .slice(0, 15)
        .map(t =>
          '<tr>'
          + `<td class="mono" style="max-width:46ch;overflow:hidden;`
          + `text-overflow:ellipsis;white-space:nowrap">${esc(t.full)}</td>`
          + `<td class="num">${t.count}</td>`
          + `<td class="mono ara-sub">${fmtAgo(t.last_ts)}</td>`
          + '</tr>'
        ).join('');
      panel.innerHTML =
        `<div class="ara-graph-panel-head">`
        + `<span class="ara-eyebrow" style="font-size:10px">Rule</span>`
        + `<h3 class="mono" style="margin:2px 0 0 0">${esc(n.id)}</h3>`
        + (n.pattern ? `<p class="mono ara-sub" style="margin:2px 0 0 0">${esc(n.pattern)}</p>` : '')
        + `<p class="ara-sub" style="margin:6px 0 0 0">`
        + `Last hit ${fmtAgo(n.last_ts)} ago · ${n.count} hits · `
        + `${nbrs.length} targets`
        + `</p>`
        + `<div style="margin:6px 0 12px 0">${verdictBar(n.verdict_counts)}</div>`
        + `<a class="ara-btn ara-btn-ghost" href="/rules/${encodeURIComponent(n.id)}?t=${encodeURIComponent(token)}" `
        + `style="text-decoration:none;font-size:11px">Open rule detail →</a>`
        + `</div>`
        + (targetRows
          ? `<table class="ara-table" style="margin-top:10px">`
            + `<thead><tr><th>target</th><th class="num">hits</th><th>last</th></tr></thead>`
            + `<tbody>${targetRows}</tbody></table>`
          : '<div class="ara-empty" style="margin-top:10px">No targets in window.</div>');
    } else {
      // target node
      const rid = Array.from(n.rules || []);
      const ruleChips = rid.map(r =>
        `<a class="ara-chip" href="/rules/${encodeURIComponent(r)}?t=${encodeURIComponent(token)}">${esc(r)}</a>`
      ).join('');
      panel.innerHTML =
        `<div class="ara-graph-panel-head">`
        + `<span class="ara-eyebrow" style="font-size:10px">Target</span>`
        + `<h3 class="mono" style="margin:2px 0 0 0;word-break:break-all">${esc(n.full)}</h3>`
        + `<p class="ara-sub" style="margin:6px 0 0 0">`
        + `Last hit ${fmtAgo(n.last_ts)} ago · ${n.count} hits · `
        + `${rid.length} rules`
        + `</p>`
        + `<div style="margin:6px 0 10px 0">${verdictBar(n.verdict_counts)}</div>`
        + (ruleChips
          ? `<div class="ara-sub" style="margin-top:4px">Rules that hit this target:</div>`
            + `<div class="ara-chips" style="margin-top:4px">${ruleChips}</div>`
          : '')
        + `</div>`;
    }
  }

  // --- Force layout ---
  function step(iters) {
    const k = 65;
    for (let it = 0; it < iters; it++) {
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx*dx + dy*dy;
          if (d2 < 1) d2 = 1;
          const f = (k * k) / d2;
          const d = Math.sqrt(d2);
          const fx = (dx / d) * f, fy = (dy / d) * f;
          a.vx += fx; a.vy += fy;
          b.vx -= fx; b.vy -= fy;
        }
      }
      for (const e of edges) {
        const a = nodes[e.a], b = nodes[e.b];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d = Math.max(1, Math.sqrt(dx*dx + dy*dy));
        const f = (d * d) / k;
        const w = 1 + Math.log(1 + e.w);
        const fx = (dx / d) * f * 0.01 * w;
        const fy = (dy / d) * f * 0.01 * w;
        a.vx -= fx; a.vy -= fy;
        b.vx += fx; b.vy += fy;
      }
      for (const n of nodes) {
        if (n.pinned) { n.vx = 0; n.vy = 0; continue; }
        n.x += Math.max(-10, Math.min(10, n.vx * 0.08));
        n.y += Math.max(-10, Math.min(10, n.vy * 0.08));
        n.vx *= 0.7; n.vy *= 0.7;
        n.x = Math.max(20, Math.min(W - 20, n.x));
        n.y = Math.max(20, Math.min(H - 20, n.y));
      }
    }
    needsRender = true;
  }

  let settleBudget = 240;
  function frame() {
    if (settleBudget > 0) {
      step(12);
      settleBudget -= 12;
    }
    if (needsRender) render();
    requestAnimationFrame(frame);
  }
  render();
  frame();

  // --- Interactivity: zoom, pan, drag, click-to-filter ---
  function clientToSvgPoint(evt) {
    const rect = svg.getBoundingClientRect();
    const sx = (evt.clientX - rect.left) * (W / rect.width);
    const sy = (evt.clientY - rect.top) * (H / rect.height);
    return { sx, sy };
  }
  function svgToWorld(p) {
    return { x: (p.sx - view.tx) / view.scale, y: (p.sy - view.ty) / view.scale };
  }

  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const p = clientToSvgPoint(ev);
    const world = svgToWorld(p);
    const factor = Math.exp(-ev.deltaY * 0.0015);
    const next = Math.min(6, Math.max(0.25, view.scale * factor));
    // Keep the world point under the cursor stable.
    view.tx = p.sx - world.x * next;
    view.ty = p.sy - world.y * next;
    view.scale = next;
    applyTransform();
  }, { passive: false });

  svg.addEventListener('mousedown', (ev) => {
    const p = clientToSvgPoint(ev);
    const target = ev.target.closest('.ara-node');
    if (target) {
      const idx = parseInt(target.dataset.idx, 10);
      dragNode = idx;
      nodes[idx].pinned = true;
      // Track world offset so drag feels locked to cursor.
      const world = svgToWorld(p);
      dragOffset.x = nodes[idx].x - world.x;
      dragOffset.y = nodes[idx].y - world.y;
      dragMoved = false;
      dragStart = { idx, sx: p.sx, sy: p.sy };
    } else {
      panning = true;
      panStart = { x: p.sx, y: p.sy, tx: view.tx, ty: view.ty };
    }
  });
  const dragOffset = { x: 0, y: 0 };
  let dragStart = null;
  let dragMoved = false;
  window.addEventListener('mousemove', (ev) => {
    if (dragNode != null) {
      const p = clientToSvgPoint(ev);
      const world = svgToWorld(p);
      nodes[dragNode].x = world.x + dragOffset.x;
      nodes[dragNode].y = world.y + dragOffset.y;
      if (dragStart && (Math.abs(p.sx - dragStart.sx) > 3 ||
                        Math.abs(p.sy - dragStart.sy) > 3)) {
        dragMoved = true;
      }
      needsRender = true;
    } else if (panning) {
      const p = clientToSvgPoint(ev);
      view.tx = panStart.tx + (p.sx - panStart.x);
      view.ty = panStart.ty + (p.sy - panStart.y);
      applyTransform();
    } else {
      // Hover.
      const t = ev.target.closest ? ev.target.closest('.ara-node') : null;
      const nextHover = t ? parseInt(t.dataset.idx, 10) : null;
      if (nextHover !== hover) { hover = nextHover; needsRender = true; }
    }
  });
  window.addEventListener('mouseup', (ev) => {
    if (dragNode != null && !dragMoved) {
      // It was a click, not a drag — toggle filter selection.
      selected = selected === dragNode ? null : dragNode;
      nodes[dragNode].pinned = false;
      needsRender = true;
    } else if (dragNode != null) {
      // Genuine drag: keep the node where the user dropped it but unpin
      // so simulation can still nudge surrounding layout.
      nodes[dragNode].pinned = false;
      needsRender = true;
    } else if (panning) {
      // nothing — pan release
    }
    dragNode = null;
    dragStart = null;
    panning = false;
  });
  svg.addEventListener('click', (ev) => {
    // Click on empty space with no prior drag: clear selection.
    if (!ev.target.closest('.ara-node') && selected != null && !dragMoved) {
      selected = null;
      needsRender = true;
    }
  });
  svg.addEventListener('dblclick', (ev) => {
    const t = ev.target.closest('.ara-node');
    if (!t) return;
    const n = nodes[parseInt(t.dataset.idx, 10)];
    if (n.kind === 'rule') {
      window.location.href = `/rules/${encodeURIComponent(n.id)}?t=${encodeURIComponent(token)}`;
    }
  });

  // Buttons
  document.getElementById('ara-graph-reset')?.addEventListener('click', () => {
    view.scale = 1; view.tx = 0; view.ty = 0; applyTransform();
    selected = null; settleBudget = 120; needsRender = true;
  });
  document.getElementById('ara-graph-in')?.addEventListener('click', () => {
    view.scale = Math.min(6, view.scale * 1.25); applyTransform();
  });
  document.getElementById('ara-graph-out')?.addEventListener('click', () => {
    view.scale = Math.max(0.25, view.scale / 1.25); applyTransform();
  });
  window.addEventListener('resize', () => { needsRender = true; });

  // Filter bar checkboxes.
  document.querySelectorAll('#ara-graph-filter input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      const kind = cb.dataset.filter;
      const val = cb.value;
      const set = kind === 'verdict' ? filterState.verdicts : filterState.kinds;
      if (cb.checked) set.add(val); else set.delete(val);
      needsRender = true;
    });
  });
})();
</script>
<style>
  #ara-graph {
    width: 100%;
    margin: 10px 0 6px 0;
    background: color-mix(in srgb, var(--surface-container-lowest) 80%, transparent);
    border-radius: var(--radius-md);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline-variant) 18%, transparent);
    overflow: hidden;
    cursor: grab;
    user-select: none;
  }
  #ara-graph:active { cursor: grabbing; }
  .ara-graph-svg { display: block; color: var(--on-surface); }
  .ara-node { cursor: pointer; }
  .ara-graph-legend {
    display: flex; gap: 16px; align-items: center;
    padding: 6px 10px; font-family: var(--font-mono); font-size: 11px;
  }
  .ara-graph-legend i {
    width: 10px; height: 10px;
    display: inline-block; margin-right: 6px; vertical-align: -1px;
  }
  .ara-graph-legend .leg-dir    { background:#dd6054; border-radius:50%; }
  .ara-graph-legend .leg-file   { background:#a877ff; border-radius:2px; }
  .ara-graph-legend .leg-net    {
    width:0; height:0;
    background:transparent;
    border-left:6px solid transparent;
    border-right:6px solid transparent;
    border-bottom:10px solid #ff66aa;
  }
  .ara-graph-legend .leg-other  { background:#8a98a8; border-radius:50%; }
  .ara-graph-legend .leg-target {
    background: currentColor; opacity:0.5; border-radius:50%;
    width:6px; height:6px;
  }
  .ara-graph-controls {
    display: flex; gap: 6px; margin: 0 0 10px 0;
  }
  .ara-graph-controls button {
    font-family: var(--font-mono); font-size: 11px;
    padding: 4px 10px;
    background: var(--surface-container-lowest);
    color: var(--on-surface);
    border: 1px solid color-mix(in srgb, var(--outline-variant) 40%, transparent);
    border-radius: 4px;
    cursor: pointer;
  }
  .ara-graph-controls button:hover {
    background: color-mix(in srgb, var(--primary) 14%, transparent);
  }
  .ara-graph-panel {
    margin: 10px 0 22px 0;
    padding: 14px 18px;
    background: color-mix(in srgb, var(--surface-container-lowest) 80%, transparent);
    border-radius: var(--radius-md);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline-variant) 22%, transparent);
    min-height: 64px;
    font-size: 12px;
  }
  .ara-graph-panel h3 { font-size: 14px; font-weight: 600; }
  .ara-graph-panel .ara-table { margin-top: 6px; }
  .ara-graph-vbar {
    width: 100%;
    height: 6px;
    background: color-mix(in srgb, var(--outline-variant) 22%, transparent);
    border-radius: 3px;
    overflow: hidden;
    display: flex;
    margin-bottom: 4px;
  }
  .ara-graph-vbar > div { height: 100%; }
  .ara-chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .ara-chip {
    font-family: var(--font-mono); font-size: 11px;
    padding: 3px 8px;
    background: color-mix(in srgb, var(--primary) 10%, transparent);
    color: var(--on-surface);
    border-radius: 99px;
    text-decoration: none;
    border: 1px solid color-mix(in srgb, var(--outline-variant) 25%, transparent);
  }
  .ara-chip:hover { background: color-mix(in srgb, var(--primary) 22%, transparent); }
  .ara-graph-filter {
    display: flex; flex-wrap: wrap; gap: 12px;
    align-items: center;
    margin: 6px 0 10px 0;
    padding: 8px 10px;
    background: color-mix(in srgb, var(--surface-container-lowest) 60%, transparent);
    border-radius: var(--radius-md);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline-variant) 18%, transparent);
    font-family: var(--font-mono); font-size: 11px;
  }
  .ara-graph-filter label {
    display: inline-flex; gap: 4px; align-items: center; cursor: pointer;
  }
  .ara-graph-filter select {
    font-family: var(--font-mono); font-size: 11px;
    padding: 3px 6px;
    background: var(--surface-container-lowest);
    color: var(--on-surface);
    border: 1px solid color-mix(in srgb, var(--outline-variant) 40%, transparent);
    border-radius: 4px;
  }
  .ara-graph-filter-sep {
    width: 1px; height: 18px;
    background: color-mix(in srgb, var(--outline-variant) 40%, transparent);
  }
  .ara-graph-filter-label {
    font-size: 10px; text-transform: uppercase;
    opacity: 0.6; margin-right: 2px;
  }
</style>
"""


def _rule_shape(rule_id: str, pattern: str) -> tuple[str, str]:
    """Return (shape, color) for a rule graph node.

    Shapes:
      - directory deny → red circle
      - file deny      → purple square
      - net deny       → pink triangle
      - everything else falls back to a verdict-colored circle (same color
        palette the legend uses).
    """
    kind = rule_id.split(":", 1)[0] if ":" in rule_id else ""
    if kind == "deny_net":
        return ("triangle", "#ff66aa")
    if kind == "deny_path":
        p = pattern or ""
        # Directory-shaped: ends in /** or /* or /, or is a bare dir-looking path.
        is_dir = (
            p.endswith("/**") or p.endswith("/*") or p.endswith("/")
            or ("**" in p)
        )
        if is_dir:
            return ("circle", "#dd6054")
        return ("square", "#a877ff")
    # Fallthrough: let the caller pick a color by top_verdict.
    return ("circle", "")


def _rule_short_label(rule_id: str, pattern: str, fallback_target: str = "") -> str:
    """Human-legible label for a graph node.

    Priority:
      1. Known WALLS pattern → e.g. "deny ~/.ssh/**".
      2. No pattern (rule deleted, audit history only) → fall back to the
         rule kind + sample touched target so the node still tells you
         something.
      3. Nothing known → raw rule_id.
    """
    kind = rule_id.split(":", 1)[0] if ":" in rule_id else ""
    kind_tags = {
        "deny_path":        "deny",
        "deny_command":     "deny",
        "deny_net":         "deny",
        "approval_path":    "appr",
        "approval_command": "appr",
        "approval_net":     "appr",
    }
    tag = kind_tags.get(kind, "")

    def _shorten(p: str) -> str:
        if len(p) > 32:
            if "/" in p:
                parts = p.split("/")
                tail = "/".join(parts[-2:]) if len(parts) > 2 else p
                p = "…/" + tail if len(tail) < len(p) else ("…" + p[-30:])
            else:
                p = "…" + p[-30:]
        return p

    if pattern:
        body = _shorten(pattern)
    elif fallback_target:
        # Indicate that we're guessing from audit history, not current rules.
        body = f"~ {_shorten(fallback_target)}"
    else:
        return rule_id

    return f"{tag} {body}".strip() if tag else body


def _build_graph_data(
    hits: list[dict],
    *,
    max_targets_per_rule: int = 12,
    rule_patterns: dict[str, str] | None = None,
    session_label_by_id: dict[str, str] | None = None,
) -> dict:
    """Bipartite rule→target graph from policy hits over the window.
    ``rule_patterns`` maps rule_id → human pattern for the detail panel."""
    rules: dict[str, dict] = {}   # rule_id -> stats
    targets: dict[str, dict] = {} # target -> stats
    edges: dict[tuple[str, str], dict] = {}
    patterns = rule_patterns or {}

    for r in hits:
        rid = r.get("rule_id")
        tgt = r.get("path") or r.get("command") or r.get("touched") or ""
        if not rid or not tgt:
            continue
        verdict = r.get("verdict") or ""
        ts = r.get("ts") or 0.0
        ri = rules.setdefault(rid, {
            "count": 0, "verdict_counts": {}, "last_ts": 0.0,
        })
        ri["count"] += 1
        ri["verdict_counts"][verdict] = ri["verdict_counts"].get(verdict, 0) + 1
        if ts > ri["last_ts"]: ri["last_ts"] = ts

        ti = targets.setdefault(tgt, {
            "count": 0, "last_ts": 0.0,
            "rules": set(), "verdict_counts": {},
        })
        ti["count"] += 1
        if ts > ti["last_ts"]: ti["last_ts"] = ts
        ti["rules"].add(rid)
        ti["verdict_counts"][verdict] = ti["verdict_counts"].get(verdict, 0) + 1

        ek = (rid, tgt)
        ei = edges.setdefault(ek, {
            "count": 0, "verdict": verdict, "last_ts": 0.0,
            "sessions": set(),
        })
        ei["count"] += 1
        if ts > ei["last_ts"]: ei["last_ts"] = ts
        sid = r.get("_session") or ""
        if sid:
            ei["sessions"].add(sid)
        priority = {"deny": 3, "approval": 2, "allow": 1}
        if priority.get(verdict, 0) > priority.get(ei["verdict"], 0):
            ei["verdict"] = verdict

    kept_targets: set[str] = set()
    edges_out = []
    by_rule: dict[str, list] = {}
    for (rid, tgt), ei in edges.items():
        by_rule.setdefault(rid, []).append((rid, tgt, ei))
    for rid, group in by_rule.items():
        group.sort(key=lambda x: x[2]["count"], reverse=True)
        for rid, tgt, ei in group[:max_targets_per_rule]:
            kept_targets.add(tgt)
            edges_out.append({
                "rule": rid, "target": tgt,
                "count": ei["count"], "verdict": ei["verdict"],
                "last_ts": ei["last_ts"],
                "sessions": sorted(ei["sessions"]),
            })

    # Per-rule top target (used as a fallback label when the WALLS pattern
    # is no longer available — e.g. the rule was deleted but audit hits remain).
    rule_top_target: dict[str, str] = {}
    for rid, group in by_rule.items():
        if group:
            rule_top_target[rid] = group[0][1]

    rule_nodes = []
    for rid, ri in rules.items():
        vc = ri["verdict_counts"]
        top_verdict = max(vc, key=lambda k: vc[k]) if vc else ""
        pattern = patterns.get(rid, "")
        shape, color = _rule_shape(rid, pattern)
        rule_kind_prefix = rid.split(":", 1)[0] if ":" in rid else ""
        # Collapse into a coarse bucket for the filter bar.
        if "_net" in rule_kind_prefix:
            rule_kind = "net"
        elif "_command" in rule_kind_prefix:
            rule_kind = "command"
        elif "_path" in rule_kind_prefix:
            rule_kind = "path"
        else:
            rule_kind = "other"
        rule_nodes.append({
            "id": rid,
            "label": rid,
            "short_label": _rule_short_label(rid, pattern, rule_top_target.get(rid, "")),
            "pattern": pattern,
            "count": ri["count"],
            "last_ts": ri["last_ts"],
            "verdict_counts": vc,
            "top_verdict": top_verdict,
            "shape": shape,
            "color": color,
            "kind": rule_kind,
        })

    target_nodes = []
    for tgt in kept_targets:
        ti = targets[tgt]
        label = tgt
        if len(label) > 44:
            label = "…" + label[-41:]
        target_nodes.append({
            "id": tgt,
            "label": label,
            "full": tgt,
            "count": ti["count"],
            "last_ts": ti["last_ts"],
            "rules": sorted(ti["rules"]),
            "verdict_counts": ti["verdict_counts"],
        })

    return {
        "rules": rule_nodes,
        "targets": target_nodes,
        "edges": edges_out,
    }


def render_graph(
    state_root: Path, token: str,
    *, window_s: float = 3600.0,
    session_filter: str | None = None,
) -> str:
    """Legacy wrapper — /graph now folds into /courtyard?tab=graph."""
    return _page(
        "Courtyard · Graph", "courtyard",
        _render_graph_body(state_root, token, window_s=window_s,
                           session_filter=session_filter),
        token,
    )


def _render_graph_body(
    state_root: Path, token: str,
    *, window_s: float = 3600.0,
    session_filter: str | None = None,
) -> str:
    """Body fragment for the rule→target graph. Used by /courtyard's
    Graph tab and the legacy /graph redirect."""
    hits = _load_policy_hits(state_root, window_s=window_s, session_limit=50)
    if session_filter:
        hits = [r for r in hits if (r.get("_session") or "").startswith(session_filter[:10])]
    rule_patterns: dict[str, str] = {}
    # Bulwark is now the policy source — label every rule with its
    # <action> <layer> <match> line so graph nodes are self-explanatory.
    try:
        from .. import bulwark as _bw
        bw_path = state_root / "bulwark.md"
        if bw_path.exists():
            bw = _bw.parse_file(bw_path)
            for r in bw.rules:
                rule_patterns[r.rule_id] = (
                    f"{r.action} {r.layer} {r.match.primary}"
                )
    except Exception:  # noqa: BLE001
        pass

    graph = _build_graph_data(hits, rule_patterns=rule_patterns)
    import html as _html
    graph_attr = _html.escape(json.dumps(graph), quote=True)

    # Session list for the filter dropdown: everything observed in the window.
    session_ids: list[str] = []
    seen_sids: set[str] = set()
    for r in hits:
        sid = r.get("_session") or ""
        if sid and sid not in seen_sids:
            seen_sids.add(sid)
            session_ids.append(sid)

    totals = {
        "rules": len(graph["rules"]),
        "targets": len(graph["targets"]),
        "edges": len(graph["edges"]),
        "hits": sum(e["count"] for e in graph["edges"]),
    }

    # Window presets for the dropdown.
    windows = [(900, "15m"), (3600, "1h"), (21600, "6h"), (86400, "24h")]
    window_opts = "".join(
        f'<option value="{sec}"{" selected" if int(window_s) == sec else ""}>{label}</option>'
        for sec, label in windows
    )
    session_opts = (
        f'<option value=""{" selected" if not session_filter else ""}>all sessions</option>'
        + "".join(
            f'<option value="{e(sid)}"{" selected" if session_filter == sid else ""}>{e(sid[:10])}</option>'
            for sid in session_ids
        )
    )
    filter_bar = (
        '<form id="ara-graph-filter" class="ara-graph-filter" method="get" action="/graph">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        '<label>window <select name="window" onchange="this.form.submit()">'
        f'{window_opts}</select></label>'
        '<label>session <select name="session" onchange="this.form.submit()">'
        f'{session_opts}</select></label>'
        '<span class="ara-graph-filter-sep"></span>'
        '<span class="ara-graph-filter-label">verdict</span>'
        '<label><input type="checkbox" data-filter="verdict" value="deny" checked>deny</label>'
        '<label><input type="checkbox" data-filter="verdict" value="approval" checked>approval</label>'
        '<label><input type="checkbox" data-filter="verdict" value="allow" checked>allow</label>'
        '<span class="ara-graph-filter-sep"></span>'
        '<span class="ara-graph-filter-label">kind</span>'
        '<label><input type="checkbox" data-filter="kind" value="path" checked>path</label>'
        '<label><input type="checkbox" data-filter="kind" value="command" checked>command</label>'
        '<label><input type="checkbox" data-filter="kind" value="net" checked>net</label>'
        '<label><input type="checkbox" data-filter="kind" value="other" checked>other</label>'
        '</form>'
    )

    legend = (
        '<div class="ara-graph-legend">'
        '<span><i class="leg-dir"></i>deny dir</span>'
        '<span><i class="leg-file"></i>deny file</span>'
        '<span><i class="leg-net"></i>deny net</span>'
        '<span><i class="leg-other"></i>other rules</span>'
        '<span><i class="leg-target"></i>target</span>'
        '<span style="margin-left:auto" class="ara-sub">'
        f'{totals["rules"]} rules · {totals["targets"]} targets · '
        f'{totals["edges"]} edges · {totals["hits"]} hits · 1h window'
        '</span>'
        '</div>'
    )

    controls = (
        '<div class="ara-graph-controls">'
        '<button id="ara-graph-in" type="button">+ zoom</button>'
        '<button id="ara-graph-out" type="button">– zoom</button>'
        '<button id="ara-graph-reset" type="button">reset</button>'
        '<span class="ara-sub" style="align-self:center;margin-left:8px">'
        'scroll to zoom · drag to pan · click node to filter · '
        'double-click rule to drill in'
        '</span>'
        '</div>'
    )
    graph_signals = (
        '<div class="courtyard-resolution-grid">'
        + _courtyard_signal_card("Rules", totals["rules"], "Policy nodes in view.", tone="graph")
        + _courtyard_signal_card("Targets", totals["targets"], "Paths, commands, and hosts.", tone="stream")
        + _courtyard_signal_card("Edges", totals["edges"], f'{totals["hits"]} weighted hit(s).', tone="live")
        + '</div>'
    )
    body = (
        '<section class="courtyard-graph-shell">'
        '<div class="courtyard-tab-panel">'
        '<div class="courtyard-panel-head">'
        '<div><div class="ara-eyebrow">Armor graph</div>'
        '<h2 class="ara-h">Rule → target map</h2></div>'
        '<p class="ara-sub">Each central node is a rule; leaves are the paths, '
        'commands, or hosts it has touched in the window. Edge weight scales '
        'with hit count.</p>'
        '</div>'
        + graph_signals
        + filter_bar
        + (
            # Empty-state card: either no hits at all, or hits exist
            # but none carried a rule_id (all were default-allow
            # background traffic). Show counts so operators can see
            # how much traffic is flowing even though nothing matched.
            '<div class="courtyard-graph-stage">'
            '<div class="ara-empty" style="margin-top:16px;max-width:640px">'
            '<strong>No rule fires in this window.</strong><br>'
            f'<span class="ara-sub">{len(hits):,} total policy event(s) '
            f'observed in the window; {sum(1 for h in hits if h.get("rule_id")):,} '
            f'carried a rule_id. If the number above is high, it means '
            f'traffic is flowing but none of your Bulwark rules are catching it — '
            f'<a href="/bulwark?t={e(token)}">add a rule on /bulwark</a> '
            f'or widen the time window above.</span>'
            '</div></div>'
            if not hits or (len(graph["rules"]) == 0 and len(graph["edges"]) == 0)
            else
            controls
            + '<div class="courtyard-graph-stage">'
            + f'<div id="ara-graph" data-graph="{graph_attr}"></div>'
            + '</div>'
            + legend
            + '<div id="ara-graph-panel" class="ara-graph-panel"></div>'
            + _GRAPH_JS
        )
        + '</div></section>'
    )
    return body


def render_rule_detail(
    state_root: Path, rule_id: str, token: str,
) -> str | None:
    """Drill-down for a single rule_id: hits, sparkline, targets, recent log."""
    all_hits = _load_policy_hits(state_root, window_s=86400.0, session_limit=50)
    hits = [r for r in all_hits if r.get("rule_id") == rule_id]
    if not hits:
        # Still show the page — rule might be defined but dormant.
        return _page(
            f"Rule {rule_id}", "status",
            '<section>'
            '<div class="ara-eyebrow">Rule detail</div>'
            f'<h2 class="ara-h">{e(rule_id)}</h2>'
            '<p class="ara-sub">No hits in the last 24h.</p>'
            f'<a class="ara-btn ara-btn-ghost" href="/armors?t={e(token)}" '
            'style="text-decoration:none">Back</a>'
            '</section>',
            token,
        )

    # 1-hour sparkline.
    pulse_map = _rule_pulse(hits, buckets=30, window_s=3600.0)
    pulse_1h = pulse_map.get(rule_id) or {"buckets": [0]*30, "count": 0, "verdict_counts": {}, "last_ts": 0}
    # 24-hour spark for larger context.
    pulse_map_24 = _rule_pulse(hits, buckets=48, window_s=86400.0)
    pulse_24 = pulse_map_24.get(rule_id) or {"buckets": [0]*48, "count": 0, "verdict_counts": {}, "last_ts": 0}

    # Target breakdown: count + last-seen per unique target.
    now = time.time()
    targets: dict[str, dict] = {}
    for r in hits:
        tgt = r.get("path") or r.get("command") or r.get("touched") or ""
        if not tgt:
            continue
        t = targets.setdefault(tgt, {"count": 0, "last_ts": 0, "verdicts": {}})
        t["count"] += 1
        ts = r.get("ts") or 0
        if ts > t["last_ts"]:
            t["last_ts"] = ts
        v = r.get("verdict") or ""
        t["verdicts"][v] = t["verdicts"].get(v, 0) + 1
    ranked_targets = sorted(targets.items(), key=lambda kv: kv[1]["count"], reverse=True)[:25]

    target_rows = "".join(
        '<tr>'
        f'<td class="mono" style="max-width:50ch;overflow:hidden;'
        f'text-overflow:ellipsis;white-space:nowrap">{e(tgt)}</td>'
        f'<td class="num">{t["count"]}</td>'
        f'<td class="num verdict-deny">{t["verdicts"].get("deny", 0)}</td>'
        f'<td class="num verdict-approval">{t["verdicts"].get("approval", 0)}</td>'
        f'<td class="num verdict-allow">{t["verdicts"].get("allow", 0)}</td>'
        f'<td class="mono ara-sub">{e(_fmt_ago(t["last_ts"]))}</td>'
        '</tr>'
        for tgt, t in ranked_targets
    )

    # Recent log (last 30).
    hits_sorted = sorted(hits, key=lambda r: r.get("ts") or 0, reverse=True)[:30]
    log_rows = "".join(
        "<tr>"
        f'<td class="mono">{e(_fmt_ts(r.get("ts")))}</td>'
        f'<td class="mono ara-sub">{e(_fmt_ago(r.get("ts")))}</td>'
        f'<td>{e(r.get("op") or "")}</td>'
        f'<td class="{"verdict-deny" if r.get("verdict")=="deny" else "verdict-approval" if r.get("verdict")=="approval" else "verdict-allow"}">'
        f'{e(r.get("verdict") or "")}</td>'
        f'<td class="mono" style="max-width:50ch;overflow:hidden;'
        f'text-overflow:ellipsis;white-space:nowrap">'
        f'{e(r.get("path") or r.get("command") or r.get("touched") or "")}</td>'
        "</tr>"
        for r in hits_sorted
    )

    vc = pulse_1h.get("verdict_counts") or {}
    top_verdict = max(vc, key=lambda k: vc[k]) if vc else ""
    spark_color = (
        "var(--error)" if top_verdict == "deny"
        else "#d4a53a" if top_verdict == "approval"
        else "var(--tertiary)"
    )

    body = (
        '<section>'
        '<div class="ara-eyebrow">Rule detail</div>'
        f'<h2 class="ara-h">{e(rule_id)}</h2>'
        f'<p class="ara-sub">Last hit {e(_fmt_ago(pulse_1h["last_ts"]) or "—")} · '
        f'24h total {pulse_24["count"]} · 1h total {pulse_1h["count"]}</p>'
        # Two sparklines: 1h (fine) and 24h (coarse)
        '<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center;margin:12px 0 6px 0">'
        '<div>'
        '<div class="ara-eyebrow" style="font-size:10px;margin-bottom:4px">Last hour</div>'
        + _sparkline_svg(pulse_1h["buckets"], width=260, height=40, stroke=spark_color)
        + '</div>'
        '<div>'
        '<div class="ara-eyebrow" style="font-size:10px;margin-bottom:4px">Last 24h</div>'
        + _sparkline_svg(pulse_24["buckets"], width=260, height=40, stroke=spark_color)
        + '</div>'
        '</div>'
        '<dl class="ara-dl ara-overview-counts" style="max-width:520px">'
        f'<dt>allow</dt><dd class="verdict-allow">{vc.get("allow", 0)}</dd>'
        f'<dt>approval</dt><dd class="verdict-approval">{vc.get("approval", 0)}</dd>'
        f'<dt>deny</dt><dd class="verdict-deny">{vc.get("deny", 0)}</dd>'
        f'<dt>targets</dt><dd>{len(targets)}</dd>'
        '</dl>'
        '<h3 class="ara-eyebrow" style="margin-top:22px">Top targets (24h)</h3>'
        '<table class="ara-table">'
        '<thead><tr><th>target</th><th class="num">hits</th>'
        '<th class="num">deny</th><th class="num">appr</th>'
        '<th class="num">allow</th><th>last</th></tr></thead>'
        f'<tbody>{target_rows}</tbody></table>'
        '<h3 class="ara-eyebrow" style="margin-top:22px">Recent events</h3>'
        '<table class="ara-table">'
        '<thead><tr><th>Time</th><th>Age</th><th>Op</th>'
        '<th>Verdict</th><th>Target</th></tr></thead>'
        f'<tbody>{log_rows}</tbody></table>'
        f'<div style="margin-top:18px"><a class="ara-btn ara-btn-ghost" '
        f'href="/armors?t={e(token)}" style="text-decoration:none">Back</a></div>'
        '</section>'
    )
    return _page(f"Rule {rule_id}", "status", body, token)


def _pid_alive(pid: int) -> bool:
    return process_state_mod.is_pid_live(pid)


_RUNTIME_INFO_CACHE: dict[str, dict] = {}
_RUNTIME_INFO_CACHE_AT: float = 0.0
_RUNTIME_INFO_TTL_S: float = 300.0  # 5 minutes

# Back-compat aliases (legacy code referenced these by name).
_OPENCLAW_INFO_CACHE: dict = {}
_OPENCLAW_INFO_CACHE_AT: float = 0.0
_OPENCLAW_INFO_TTL_S: float = _RUNTIME_INFO_TTL_S


def _probe_runtime(
    *,
    name: str,
    binary_name: str,
    state_dir: Path,
    config_filename: str | None,
    version_args: tuple[str, ...] = ("--version",),
    fallback_paths: tuple[Path, ...] = (),
) -> dict:
    """Generic worker-runtime probe. Looks up ``binary_name`` on PATH,
    runs it with ``version_args`` for a version line, sizes ``state_dir``
    if present, and resolves the config path inside it. ``fallback_paths``
    let us discover binaries that the armory's PATH doesn't include
    (e.g. user installs under ``~/.local/bin`` that a non-interactive
    shell wouldn't see).
    """
    import shutil as _shutil
    import subprocess as _sp
    binary_path = _shutil.which(binary_name)
    if not binary_path:
        for cand in fallback_paths:
            if cand.is_file():
                binary_path = str(cand)
                break
    info: dict[str, str | int | None] = {
        "name": name,
        "binary": binary_path,
        "version": None,
        "state_dir": None,
        "state_dir_size": None,
        "config_path": None,
    }
    if binary_path:
        try:
            cmd: list[str] = [binary_path] + list(version_args)
            r = _sp.run(
                cmd,
                capture_output=True, text=True, timeout=4,
            )
            lines = (r.stdout or r.stderr or "").splitlines()
            info["version"] = (lines[-1] if lines else "").strip()
        except Exception:  # noqa: BLE001
            pass
    if state_dir.is_dir():
        info["state_dir"] = str(state_dir)
        try:
            total = 0
            for p in state_dir.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        continue
                if total > 2 * 1024 * 1024 * 1024:
                    break
            info["state_dir_size"] = total
        except OSError:
            pass
        if config_filename:
            cfg = state_dir / config_filename
            if cfg.is_file():
                info["config_path"] = str(cfg)
    return info


def _runtime_infos() -> list[dict]:
    """Discover every supported worker runtime currently installed.
    Cached so we don't subprocess version probes on every page render.
    Returns runtimes in display order: openclaw first, then hermes.
    """
    global _RUNTIME_INFO_CACHE_AT
    import time as _time
    if (
        _RUNTIME_INFO_CACHE
        and (_time.time() - _RUNTIME_INFO_CACHE_AT) < _RUNTIME_INFO_TTL_S
    ):
        return [_RUNTIME_INFO_CACHE[k] for k in ("openclaw", "hermes")]
    oc = _probe_runtime(
        name="openclaw",
        binary_name="openclaw",
        state_dir=Path.home() / ".openclaw",
        config_filename="openclaw.json",
    )
    hr = _probe_runtime(
        name="hermes",
        binary_name="hermes",
        state_dir=Path.home() / ".hermes",
        config_filename="config.yaml",
        fallback_paths=(
            Path.home() / ".local" / "bin" / "hermes",
            Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
        ),
    )
    _RUNTIME_INFO_CACHE.clear()
    _RUNTIME_INFO_CACHE["openclaw"] = oc
    _RUNTIME_INFO_CACHE["hermes"] = hr
    _RUNTIME_INFO_CACHE_AT = _time.time()
    # Mirror to legacy single-runtime cache for any old caller still
    # reaching for ``_openclaw_info()``.
    global _OPENCLAW_INFO_CACHE_AT
    _OPENCLAW_INFO_CACHE.clear()
    _OPENCLAW_INFO_CACHE.update(oc)
    _OPENCLAW_INFO_CACHE_AT = _time.time()
    return [oc, hr]


def _openclaw_info() -> dict:
    """Back-compat: openclaw-only view of runtime info."""
    return _runtime_infos()[0]


def _ara_install_info() -> dict:
    import ara as _ara_mod
    pkg_path = Path(_ara_mod.__file__).resolve().parent
    repo_root = pkg_path.parent.parent
    return {
        "package": str(pkg_path),
        "repo_root": str(repo_root) if (repo_root / "pyproject.toml").exists() else str(pkg_path),
        "version": getattr(_ara_mod, "__version__", "?"),
    }


def _render_status_posture(state_root: Path, token: str) -> str:
    """Top-of-status "where am I" card cluster: equipped armor trading
    card, worker framework (openclaw) info, ARA install info, and the
    currently-active session if any."""
    import os as _os
    equipped_name = armors_mod.active_armor_name(state_root)
    equipped = (
        armors_mod.load_armor(state_root, equipped_name)
        if equipped_name else None
    )
    runtimes = _runtime_infos()
    ara = _ara_install_info()
    sessions_dir = state_root / "sessions"
    active_sessions: list[tuple[Path, dict]] = []
    if sessions_dir.is_dir():
        for p in sorted(
            sessions_dir.iterdir(),
            key=lambda p: p.stat().st_mtime if p.is_dir() else 0,
            reverse=True,
        ):
            if not p.is_dir():
                continue
            meta_path = p / "session.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:  # noqa: BLE001
                continue
            pid = meta.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                active_sessions.append((p, meta))

    # Full equipped loadout: armor + weapon + trim. Armor Sets owns
    # armor configuration; Status shows what the running agent is
    # actually wearing.
    if equipped:
        armor_card = (
            f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">'
            '<div class="status-armor-card">'
            + _render_loadout_stage(equipped, state_root, token)
            + '</div>'
        )
    else:
        armor_card = (
            '<div class="ara-empty" style="padding:20px">'
            'No armor equipped. Head to <a href="/armors?t=' + e(token) + '">Armor Sets</a> and equip one.</div>'
        )

    # Framework + install cards.
    def _bytes(n: int | None) -> str:
        if not n:
            return "—"
        return _fmt_bytes(n)

    def _compact_path(value: object) -> str:
        text = str(value or "")
        if not text:
            return "(none)"
        home = str(Path.home())
        if text.startswith(home):
            text = "~" + text[len(home):]
        if len(text) <= 54:
            return text
        return "…" + text[-53:]

    def _vital_rows(rows: list[tuple[str, str]]) -> str:
        return "".join(
            '<div class="status-vital-row">'
            f'<span>{e(label)}</span><strong>{value}</strong>'
            '</div>'
            for label, value in rows
        )

    def _runtime_block(rt: dict) -> str:
        installed = bool(rt.get("binary"))
        status_pill = (
            '<span class="status-runtime-pill status-runtime-pill-on">installed</span>'
            if installed else
            '<span class="status-runtime-pill status-runtime-pill-off">not installed</span>'
        )
        size = _bytes(rt.get("state_dir_size"))
        rows = _vital_rows([
            ("Binary", f'<span class="mono">{e(_compact_path(rt.get("binary") or "(not on PATH)"))}</span>'),
            ("Life folder", f'<span class="mono">{e(_compact_path(rt.get("state_dir") or "(none)"))}</span>'),
            ("Config", f'<span class="mono">{e(_compact_path(rt.get("config_path") or "(none)"))}</span>'),
            ("State size", f'<span>{e(size)}</span>'),
        ])
        return (
            '<article class="status-vital-card status-vital-runtime">'
            '<div class="status-vital-top">'
            '<span class="status-vital-orb"></span>'
            '<div><span class="ara-eyebrow">Worker runtime</span>'
            f'<h3>{e(rt["name"])}</h3></div>'
            f'{status_pill}'
            '</div>'
            f'<p class="status-vital-sub mono">{e(rt.get("version") or "?")}</p>'
            f'<div class="status-vital-rows">{rows}</div>'
            '</article>'
        )

    primary_runtime = next(
        (rt for rt in runtimes if str(rt.get("name") or "").lower() == "hermes"),
        runtimes[0] if runtimes else None,
    )
    framework_rows = _runtime_block(primary_runtime) if primary_runtime else ""
    ara_info_rows = _vital_rows([
        ("Package", f'<span class="mono">{e(_compact_path(ara["package"]))}</span>'),
        ("Repo root", f'<span class="mono">{e(_compact_path(ara["repo_root"]))}</span>'),
        ("State root", f'<span class="mono">{e(_compact_path(state_root))}</span>'),
        ("Armory pid", f'<span class="mono">{_os.getpid()}</span>'),
    ])
    ara_rows = (
        '<article class="status-vital-card status-vital-ara">'
        '<div class="status-vital-top">'
        '<span class="status-vital-orb"></span>'
        '<div><span class="ara-eyebrow">ARA install</span>'
        f'<h3>v{e(ara["version"])}</h3></div>'
        '<span class="status-runtime-pill status-runtime-pill-on">armed</span>'
        '</div>'
        '<p class="status-vital-sub">Local Agent Ready Armor build.</p>'
        f'<div class="status-vital-rows">{ara_info_rows}</div>'
        '</article>'
    )

    if active_sessions:
        sp, sm = active_sessions[0]
        started = sm.get("started_at") or sp.stat().st_mtime
        pid = sm.get("pid")
        session_info_rows = _vital_rows([
            ("Active session", f'<a class="mono" href="/sessions/{e(sp.name)}?t={e(token)}">{e(sp.name)}</a>'),
            ("Started", f'<span class="mono">{e(_fmt_ts(started))}</span>'),
            ("Age", f'<span>{e(_fmt_ago(started))} ago</span>'),
            ("Supervisor pid", f'<span class="mono">{e(pid)}</span>'),
            ("Live sessions", f'<span>{len(active_sessions)}</span>'),
        ])
        session_rows = (
            '<article class="status-vital-card status-vital-session">'
            '<div class="status-vital-top">'
            '<span class="status-vital-orb status-vital-orb-live"></span>'
            '<div><span class="ara-eyebrow">Live session</span>'
            '<h3>Contained</h3></div>'
            '<span class="status-runtime-pill status-runtime-pill-on">running</span>'
            '</div>'
            '<p class="status-vital-sub">Supervisor is active and reachable.</p>'
            f'<div class="status-vital-rows">{session_info_rows}</div>'
            '</article>'
        )
    else:
        session_rows = (
            '<article class="status-vital-card status-vital-session">'
            '<div class="status-vital-top">'
            '<span class="status-vital-orb"></span>'
            '<div><span class="ara-eyebrow">Live session</span>'
            '<h3>Idle</h3></div>'
            '<span class="status-runtime-pill status-runtime-pill-off">waiting</span>'
            '</div>'
            '<p class="status-vital-sub">No active supervisor is running.</p>'
            '<div class="status-vital-rows">'
            '<div class="status-vital-row"><span>Launch</span>'
            '<strong><code>ara launch --contain -- hermes -z &lt;prompt&gt;</code></strong></div>'
            '</div>'
            '</article>'
        )

    return (
        '<section class="status-posture-section">'
        '<div class="ara-eyebrow">Posture</div>'
        '<h2 class="ara-h">At a glance</h2>'
        '<div class="status-posture-grid">'
        f'<div class="status-posture-col status-posture-armor">{armor_card}</div>'
        '<div class="status-vitals-grid">'
        f'{framework_rows}{ara_rows}{session_rows}'
        '</div>'
        '</div>'
        '</section>'
    )


def _sessions_table_html(rows: list, token: str) -> str:
    """Render a list of data.SessionRow as an HTML table. Shared between
    the Status summary (if kept) and the /sessions page."""
    if not rows:
        return '<div class="ara-empty">(none)</div>'
    out = []
    for r in rows:
        row_class = ' class="ara-row-attention"' if r.pending_approvals > 0 else (
            ' class="ara-row-deny"' if r.denies > 0 else ''
        )
        live = (
            '<span class="status-live-dot" title="Supervisor pid alive">●</span> '
            if r.is_active else ''
        )
        contain_tag = (
            ' <span class="status-contain-tag" title="Contained launch">contained</span>'
            if r.is_contained else ''
        )
        out.append(
            f'<tr{row_class}>'
            f'<td class="mono" data-label="Session">{live}{e(r.short_id)}{contain_tag}</td>'
            f'<td data-label="Age">{e(_fmt_ago(r.started))}</td>'
            f'<td class="mono" data-label="Started">{e(_fmt_ts(r.started))}</td>'
            f'<td class="num" data-label="Guards">{r.guards}</td>'
            f'<td class="num" data-label="Hits">{r.hits}</td>'
            f'<td class="num" data-label="Denies">{r.denies}</td>'
            f'<td class="num" data-label="Pending">{r.pending_approvals}</td>'
            f'<td data-label=""><a href="/sessions/{e(r.id)}?t={e(token)}">view</a></td>'
            '</tr>'
        )
    return (
        '<table class="ara-table">'
        '<thead><tr>'
        '<th>Session</th><th>Age</th><th>Started</th>'
        '<th class="num">Guards</th><th class="num">Hits</th>'
        '<th class="num">Denies</th><th class="num">Pending</th><th></th>'
        '</tr></thead>'
        f'<tbody>{"".join(out)}</tbody></table>'
    )


def render_status(state_root: Path, token: str, *, viewer_token: str = "") -> str:
    """Status = runtime health at a glance.

    Posture card (equipped armor, worker runtime, ARA install, active
    supervisor summary) + click-through summary counts. The activity
    timeline, top-N tables, and raw event stream live on /audit. The
    sessions table lives on /sessions. The baselines list on /baselines.
    Keeping /status narrow prevents the same data from surfacing on
    three pages with three different renderings."""
    # Status page is intentionally narrow now: posture card + a single
    # "Jump to…" strip of compact tiles. The summary-counts table +
    # read-only share link + housekeeping purge all moved to /settings;
    # the left nav already surfaces everything that was redundant.
    from . import data as _data
    from .. import canary as canary_mod
    rows = _data.list_sessions(state_root)
    active_count = sum(1 for r in rows if r.is_active)

    # Canary summary — count seeded + total hits across all canaries.
    canary_store = canary_mod.load(state_root)
    canary_total = len(canary_store.canaries)
    canary_hits = sum(c.hit_count for c in canary_store.canaries)
    canary_hits_html = (
        f'<strong style="color:var(--error)">{canary_hits}</strong>'
        if canary_hits > 0 else f'<strong>{canary_hits}</strong>'
    )

    body = [
        _render_status_posture(state_root, token),
        '<section>',
        '<div class="ara-eyebrow">Runtime</div>',
        '<div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap">',
        f'<a class="ara-btn ara-btn-ghost" href="/courtyard?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center;gap:8px">'
        f'<strong>{active_count}</strong> '
        '<span class="ara-sub">active supervisor(s) · Courtyard →</span>'
        '</a>',
        f'<a class="ara-btn ara-btn-ghost" href="/courtyard?tab=live&t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center;gap:8px">'
        '<span class="ara-sub">live event stream → Courtyard</span>'
        '</a>',
        f'<a class="ara-btn ara-btn-ghost" href="/courtyard?tab=live&verdict=flag&t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center;gap:8px" '
        'title="Canaries + classifier hits + MCP-Scan findings">'
        f'{canary_hits_html} '
        f'<span class="ara-sub">canary hit(s) · {canary_total} seeded</span>'
        '</a>',
        f'<a class="ara-btn ara-btn-ghost" href="/settings?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center;gap:8px">'
        '<span class="ara-sub">share link, purge, timeouts → Settings</span>'
        '</a>',
        '</div>',
        '</section>',
    ]
    return _page("Status", "status", "".join(body), token)


def _courtyard_signal_card(
    label: str,
    value: str | int,
    detail: str,
    *,
    tone: str = "neutral",
) -> str:
    return (
        f'<article class="courtyard-signal-card courtyard-signal-{e(tone)}">'
        f'<span>{e(label)}</span>'
        f'<strong>{e(value)}</strong>'
        f'<small>{detail}</small>'
        '</article>'
    )


def render_hermes_guard_card(state_root) -> str:
    """Courtyard signal card for the hermes containment guard: contained /
    rogue-detected / held-off."""
    import time as _t
    from .. import hermes_guard as hg
    st = hg.load_state(Path(state_root))
    if st.held_off:
        return _courtyard_signal_card(
            "Hermes guard", "HELD OFF",
            e(st.last_detail or "repeated rogue — needs you"),
            tone="approval",
        )
    try:
        rogue = hg.find_uncontained_hermes()
    except Exception:
        rogue = []
    if rogue:
        return _courtyard_signal_card(
            "Hermes guard", "ROGUE",
            e(f"uncontained pids {rogue} — reconciling"),
            tone="approval",
        )
    return _courtyard_signal_card(
        "Hermes guard", "contained ✓",
        e(f"{hg.recent_strikes(st, _t.time())} recent strike(s)."),
        tone="live",
    )


def _courtyard_pending_count(state_root: Path) -> int:
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return 0
    total = 0
    for s in sessions_dir.iterdir():
        if s.is_dir():
            total += len(approvals_mod.list_pending(s))
    return total


def _render_courtyard_header(
    state_root: Path,
    token: str,
    tab: str,
    title_suffix: str,
    tabs_html: str,
) -> str:
    from . import data as _data
    rows = _data.list_sessions(state_root)
    active = [r for r in rows if r.is_active]
    events = list(_iter_recent_events(state_root, limit=500))
    flags = sum(
        1 for r in events
        if (r.get("verdict") in ("deny", "approval")
            or r.get("kind") in ("canary_hit", "classifier_hit", "mcp_scan_finding"))
    )
    pending = _courtyard_pending_count(state_root)
    graph_hits = len(_load_policy_hits(state_root, window_s=3600.0, session_limit=50))
    signal_grid = (
        '<div class="courtyard-signal-grid">'
        + _courtyard_signal_card(
            "Live sessions",
            len(active),
            "Contained workers currently alive.",
            tone="live" if active else "neutral",
        )
        + _courtyard_signal_card(
            "Stream events",
            len(events),
            f'{flags} flag(s) in recent audit traffic.',
            tone="stream" if events else "neutral",
        )
        + _courtyard_signal_card(
            "Approvals",
            pending,
            "Operator decisions waiting.",
            tone="approval" if pending else "neutral",
        )
        + _courtyard_signal_card(
            "Graph hits",
            graph_hits,
            "Policy events in the last hour.",
            tone="graph" if graph_hits else "neutral",
        )
        + render_hermes_guard_card(state_root)
        + '</div>'
    )
    active_label = {
        "sessions": "Sessions",
        "live": "Live stream",
        "approvals": "Approvals",
        "graph": "Graph",
    }.get(tab, "Sessions")
    return (
        '<section class="courtyard-hero">'
        '<div class="courtyard-hero-copy">'
        '<div class="ara-eyebrow">Runtime courtyard</div>'
        f'<h2 class="ara-h">Courtyard{title_suffix}</h2>'
        f'<p class="ara-sub">Active view: <strong>{e(active_label)}</strong>. '
        'Track contained runs, stream decisions, pending approvals, and the '
        'rule-to-target map from one live surface.</p>'
        + tabs_html +
        '</div>'
        + signal_grid +
        '</section>'
    )


def _render_session_cards(rows: list, token: str) -> str:
    if not rows:
        return (
            '<div class="ara-empty">No supervisors running. '
            'Launch one with <code>ara launch --contain -- hermes -z &lt;prompt&gt;</code>.</div>'
        )
    cards: list[str] = []
    for r in rows:
        attention = (
            "approval" if r.pending_approvals > 0
            else "deny" if r.denies > 0
            else "live"
        )
        contain_tag = (
            '<span class="ara-pill ok">contained</span>' if r.is_contained
            else '<span class="ara-pill">direct</span>'
        )
        cards.append(
            f'<article class="courtyard-session-card courtyard-session-{attention}">'
            '<div class="courtyard-session-top">'
            '<span class="status-live-dot">●</span>'
            f'<strong class="mono">{e(r.short_id)}</strong>'
            f'{contain_tag}'
            '</div>'
            '<div class="courtyard-session-meta">'
            f'<span>{e(_fmt_ago(r.started))} old</span>'
            f'<span class="mono">{e(_fmt_ts(r.started))}</span>'
            '</div>'
            '<div class="courtyard-session-stats">'
            f'<span><b>{r.guards}</b><small>guards</small></span>'
            f'<span><b>{r.hits}</b><small>hits</small></span>'
            f'<span><b>{r.denies}</b><small>denies</small></span>'
            f'<span><b>{r.pending_approvals}</b><small>pending</small></span>'
            '</div>'
            f'<a class="ara-btn ara-btn-ghost" href="/sessions/{e(r.id)}?t={e(token)}">view session</a>'
            '</article>'
        )
    return '<div class="courtyard-session-grid">' + "".join(cards) + '</div>'


def render_courtyard(
    state_root: Path, token: str, *,
    tab: str = "sessions",
    window_s: float = 3600.0,
    session_filter: str | None = None,
) -> str:
    """Courtyard = the runtime hub.

    Four tabs:
      * **Sessions** — active + archived supervisor runs (the historical
        Courtyard).
      * **Live stream** — the `/audit` event stream.
      * **Approvals** — pending operator decisions (same data as the
        old `/approvals`).
      * **Graph** — rule→target map (the old `/graph`).

    All three legacy URLs (`/audit`, `/approvals`, `/graph`)
    303-redirect into here so bookmarks keep working.
    """
    tab = tab if tab in ("sessions", "live", "approvals", "graph") else "sessions"

    tabs_html = _render_tab_bar(
        (
            ("sessions", "Sessions", f"?tab=sessions&t={e(token)}"),
            ("live", "Live stream", f"?tab=live&t={e(token)}"),
            ("approvals", "Approvals", f"?tab=approvals&t={e(token)}"),
            ("graph", "Graph", f"?tab=graph&t={e(token)}"),
        ),
        tab,
        label="Courtyard views",
    )

    title_suffix = {
        "sessions": "", "live": " · Live stream",
        "approvals": " · Approvals", "graph": " · Graph",
    }[tab]

    header = _render_courtyard_header(
        state_root, token, tab, title_suffix, tabs_html,
    )

    if tab == "live":
        body_frag = _render_audit_stream_body(state_root, token)
        return _page(f"Courtyard{title_suffix}", "courtyard",
                     header + body_frag, token)

    if tab == "approvals":
        body_frag = _render_approvals_body(state_root, token)
        return _page(f"Courtyard{title_suffix}", "courtyard",
                     header + body_frag, token)

    if tab == "graph":
        body_frag = _render_graph_body(
            state_root, token,
            window_s=window_s, session_filter=session_filter,
        )
        return _page(f"Courtyard{title_suffix}", "courtyard",
                     header + body_frag, token)

    # ---- sessions tab (default) ----
    from . import data as _data
    from .. import canary as canary_mod
    rows = _data.list_sessions(state_root)
    active = [r for r in rows if r.is_active]
    archived = [r for r in rows if not r.is_active]

    canary_store = canary_mod.load(state_root)
    canary_hits = sum(c.hit_count for c in canary_store.canaries)
    canary_total = len(canary_store.canaries)
    canary_strip = ""
    if canary_total > 0:
        hits_color = "var(--error)" if canary_hits > 0 else "var(--tertiary)"
        canary_strip = (
            '<div style="margin:8px 0 4px;padding:8px 12px;'
            'border-left:3px solid ' + hits_color + ';'
            'background:color-mix(in srgb,' + hits_color + ' 6%,transparent);'
            'font-size:12px">'
            f'<strong style="color:{hits_color}">{canary_hits}</strong> canary hit(s) '
            f'· <span class="ara-sub">{canary_total} seeded</span> · '
            f'<a href="?tab=live&t={e(token)}" '
            'style="color:var(--primary)">view flags in live stream →</a>'
            '</div>'
        )

    panel = (
        '<section class="courtyard-tab-panel courtyard-sessions-panel">'
        '<div class="courtyard-panel-head">'
        '<div><div class="ara-eyebrow">Sessions</div>'
        '<h2 class="ara-h">Contained run ledger</h2></div>'
        '<p class="ara-sub">Active supervisors sit as live cards; archived '
        'runs fold below for audit, transcript, and containment review.</p>'
        '</div>'
        + canary_strip +
        '<div class="courtyard-section-label">'
        f'<span>Active</span><b>{len(active)}</b></div>'
        + _render_session_cards(active, token)
        + (
            '<details class="status-archived"'
            ' style="margin-top:18px">'
            '<summary class="ara-eyebrow" style="cursor:pointer">'
            'Archived '
            f'<span class="ara-sub">· {len(archived)} (click to expand)</span>'
            '</summary>'
            f'<div style="margin-top:10px">{_sessions_table_html(archived, token)}</div>'
            '</details>'
            if archived else ''
        )
        + '</section>'
    )
    return _page("Courtyard", "courtyard", header + panel, token)


def render_baselines_page(state_root: Path, token: str) -> str:
    """/baselines promotes the existing Baselines section to a
    first-class page. Content is unchanged; only its address is."""
    return _page("Baselines", "baselines",
                 _render_baselines_section(state_root, token), token)


def _render_baselines_section(state_root: Path, token: str) -> str:
    from .. import baseline as baseline_mod
    names = baseline_mod.list_names(state_root)
    rows = []
    for n in names:
        try:
            b = baseline_mod.load(state_root, n)
        except Exception:
            continue
        rows.append(
            '<tr>'
            f'<td class="mono" data-label="Name">{e(b.name)}</td>'
            f'<td class="num" data-label="Paths">{len(b.paths)}</td>'
            f'<td class="num" data-label="Cmds">{len(b.commands)}</td>'
            f'<td class="num" data-label="Hosts">{len(b.hosts)}</td>'
            f'<td class="mono ara-sub" data-label="Source">'
            f'{e(b.source_session[:12])}</td>'
            f'<td data-label="Actions">'
            f'<form method="post" action="/baselines/{e(b.name)}/to-armor?t={e(token)}" '
            'style="display:inline">'
            f'<input type="text" name="armor" placeholder="armor name" required '
            'pattern="[a-zA-Z0-9._-]+" style="width:12ch;font-size:11px">'
            '<button class="ara-btn ara-btn-ghost" type="submit">→ armor</button>'
            '</form>'
            f'<form method="post" action="/baselines/{e(b.name)}/delete?t={e(token)}" '
            'style="display:inline;margin-left:6px" '
            'onsubmit="return confirm(\'Delete baseline ' + e(b.name) + '?\')">'
            '<button class="ara-btn ara-btn-bad" type="submit">×</button>'
            '</form>'
            '</td>'
            '</tr>'
        )
    table = (
        '<div class="ara-empty">No baselines yet. Record one from a session '
        'after a clean run.</div>'
        if not rows
        else '<table class="ara-table"><thead><tr>'
             '<th>Name</th><th class="num">Paths</th>'
             '<th class="num">Cmds</th><th class="num">Hosts</th>'
             '<th>Source</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>'
    )
    return (
        '<section>'
        '<div class="ara-eyebrow">Fingerprint</div>'
        '<h2 class="ara-h">Baselines</h2>'
        '<p class="ara-sub">Snapshots of what a known-good run touched. '
        'Promote to an armor to lock down everything else.</p>'
        + table +
        '<form method="post" action="/baselines/record?t=' + e(token) + '" '
        'style="margin-top:12px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">'
        '<span class="ara-sub">Record latest session as</span>'
        '<input type="text" name="name" placeholder="baseline name" required '
        'pattern="[a-zA-Z0-9._-]+" style="font-size:12px;padding:4px 8px">'
        '<button class="ara-btn ara-btn-primary" type="submit">Record</button>'
        '</form>'
        '</section>'
    )


# ---------- approvals ----------

def _render_approvals_body(state_root: Path, token: str) -> str:
    sessions_dir = state_root / "sessions"
    pending_rows: list[str] = []
    resolved_all: list[tuple[str, approvals_mod.ApprovalRecord]] = []
    now = time.time()
    if sessions_dir.is_dir():
        for s in sessions_dir.iterdir():
            if not s.is_dir():
                continue
            for rec in approvals_mod.list_pending(s):
                target = rec.path or rec.command or ""
                if rec.op == "resource.resolve_url":
                    target = rec.resource_id or target
                resource_url_input = (
                    '<input class="ara-input mono" name="resolved_url" '
                    'placeholder="Enter usable URL" '
                    f'value="{e(rec.suggested_url or rec.path or "")}" '
                    'style="min-width:260px;font-size:11px;padding:5px 8px">'
                    if rec.op == "resource.resolve_url" else ""
                )
                resource_hint = (
                    f'<div class="ara-sub">Resource: <strong>{e(rec.resource_name or "")}</strong>'
                    f' · suggested: <code>{e(rec.suggested_url or "")}</code></div>'
                    if rec.op == "resource.resolve_url" else ""
                )
                age = max(0.0, now - rec.created_at)
                pending_rows.append(
                    f'<tr class="{_age_attention(age)}">'
                    f'<td class="mono" data-label="ID">{e(rec.id)}</td>'
                    f'<td class="mono" data-label="Session">{e(s.name[:10])}</td>'
                    f'<td data-label="Age">{e(_fmt_ago(rec.created_at))}</td>'
                    f'<td class="mono" data-label="Op">{e(rec.op)}</td>'
                    f'<td class="mono" data-label="Target">{e(target)}{resource_hint}</td>'
                    f'<td class="mono" data-label="Rule">{e(rec.rule_id or "")}</td>'
                    f'<td data-label="Actions">'
                    f'  <form method="post" action="/approvals/{e(rec.id)}/respond?t={e(token)}">'
                    f'    {resource_url_input}'
                    '    <span class="ara-actions">'
                    '      <button class="ara-btn ara-btn-ok" name="action" value="allow">allow</button>'
                    '      <button class="ara-btn ara-btn-bad" name="action" value="deny">deny</button>'
                    '      <select name="ttl" class="ara-ttl" title="TTL for +always grants (blank = permanent)">'
                    '        <option value="">∞</option>'
                    '        <option value="5m">5m</option>'
                    '        <option value="1h">1h</option>'
                    '        <option value="1d">1d</option>'
                    '      </select>'
                    '      <button class="ara-btn ara-btn-ghost" name="action" value="allow-always">allow+</button>'
                    '      <button class="ara-btn ara-btn-ghost" name="action" value="deny-always">deny+</button>'
                    '    </span>'
                    "  </form>"
                    "</td>"
                    "</tr>"
                )
            for rec in approvals_mod.list_resolved(s):
                resolved_all.append((s.name, rec))

    # Sort resolved by resolved_at desc, cap at 50 for the page.
    resolved_all.sort(
        key=lambda pair: pair[1].resolved_at or pair[1].created_at, reverse=True
    )
    resolved_all = resolved_all[:50]

    # Latency summary — median & p90 over the last 50 resolved.
    latencies = [
        (r.resolved_at - r.created_at)
        for _, r in resolved_all
        if r.resolved_at and r.resolved_at >= r.created_at
    ]
    median_latency = "—"
    p90_latency = "—"
    max_latency = "—"
    if latencies:
        latencies.sort()
        def _pct(p: float) -> float:
            i = min(len(latencies) - 1, int(p * len(latencies)))
            return latencies[i]
        median_latency = _fmt_latency(_pct(0.5))
        p90_latency = _fmt_latency(_pct(0.9))
        max_latency = _fmt_latency(max(latencies))
    summary_html = (
        '<div class="courtyard-resolution-grid">'
        + _courtyard_signal_card(
            "Pending",
            len(pending_rows),
            "Requests waiting for an operator.",
            tone="approval" if pending_rows else "neutral",
        )
        + _courtyard_signal_card(
            "Resolved",
            len(latencies),
            "Decisions measured in the recent window.",
            tone="live" if latencies else "neutral",
        )
        + _courtyard_signal_card(
            "Median",
            median_latency,
            f'p90 {e(p90_latency)} · max {e(max_latency)}',
            tone="stream" if latencies else "neutral",
        )
        + '</div>'
    )

    resolved_rows: list[str] = []
    for sname, rec in resolved_all:
        target = rec.path or rec.command or ""
        latency = None
        if rec.resolved_at and rec.resolved_at >= rec.created_at:
            latency = rec.resolved_at - rec.created_at
        status = rec.status or ""
        status_cls = (
            "verdict-allow" if status == "allowed"
            else "verdict-deny" if status in ("denied", "timeout")
            else ""
        )
        resolved_rows.append(
            "<tr>"
            f'<td class="mono" data-label="ID">{e(rec.id)}</td>'
            f'<td class="mono" data-label="Session">{e(sname[:10])}</td>'
            f'<td class="mono" data-label="Resolved">{e(_fmt_ts(rec.resolved_at))}</td>'
            f'<td class="mono" data-label="Latency">{e(_fmt_latency(latency))}</td>'
            f'<td class="{e(status_cls)}" data-label="Status">{e(status)}</td>'
            f'<td class="mono" data-label="Action">{e(rec.action or "")}</td>'
            f'<td class="mono" data-label="Op">{e(rec.op)}</td>'
            f'<td class="mono" data-label="Target">{e(target)}</td>'
            "</tr>"
        )

    body = [
        '<section class="courtyard-approval-shell">',
        '<div class="courtyard-tab-panel">',
        '<div class="courtyard-panel-head">',
        '<div><div class="ara-eyebrow">Operator queue</div>',
        '<h2 class="ara-h">Pending approvals</h2></div>',
        '<p class="ara-sub">'
        'Responses: <strong>allow</strong>/<strong>deny</strong> resolve this one. '
        '<strong>allow+</strong>/<strong>deny+</strong> promote to persistent '
        'overrides/blackholes.</p>',
        '</div>',
        summary_html,
        (
            '<div class="ara-empty">No pending approvals.</div>'
            if not pending_rows
            else '<table class="ara-table">'
                 '<thead><tr><th>ID</th><th>Session</th><th>Age</th>'
                 '<th>Op</th><th>Target</th><th>Rule</th><th>Actions</th></tr></thead>'
                 f'<tbody>{"".join(pending_rows)}</tbody></table>'
        ),
        '<h3 class="ara-eyebrow" style="margin-top:26px">Recent resolutions</h3>',
        '<p class="ara-sub">Time-to-respond, most recent first.</p>',
        (
            '<div class="ara-empty">No resolved approvals yet.</div>'
            if not resolved_rows
            else '<table class="ara-table">'
                 '<thead><tr><th>ID</th><th>Session</th><th>Resolved</th>'
                 '<th>Latency</th><th>Status</th><th>Action</th>'
                 '<th>Op</th><th>Target</th></tr></thead>'
                 f'<tbody>{"".join(resolved_rows)}</tbody></table>'
        ),
        "</div></section>",
    ]
    return "".join(body)


def render_approvals(state_root: Path, token: str) -> str:
    """Back-compat wrapper. Primary surface is /audit?tab=approvals."""
    return _page("Audit · Approvals", "audit",
                 _render_approvals_body(state_root, token), token)


# ---------- walls ----------

def _resolve_walls_source(
    state_root: Path, startup_walls: Path | None
) -> tuple[Path | None, str]:
    """Pick which WALLS file the /walls page targets.

    Equipped armor's WALLS.md wins over the startup --walls file. That way
    edits made from the web go directly into the active loadout.
    """
    active = armors_mod.active_walls_file(state_root)
    if active is not None:
        active_name = armors_mod.active_armor_name(state_root) or "?"
        return active, f"equipped armor: {active_name}"
    if startup_walls and startup_walls.exists():
        return startup_walls, "startup --walls"
    return None, "(none)"


def render_bulwark(
    state_root: Path,
    token: str,
    *,
    scope_filter: str = "all",      # "all" | "global" | "armor"
    layer_filter: str = "all",
    flash: str | None = None,
    flash_level: str = "info",
) -> str:
    """Render the unified Bulwark policy view.

    Consolidates WALLS + Allowlist (overrides/blackholes) + Rhema rules
    into one page driven by ``<state_root>/bulwark.md``. Default view
    shows only Global + currently-equipped armor rules; other armors are
    collapsible cards.
    """
    from .. import bulwark as bw_mod
    from .. import armors as armors_mod
    bw_path = state_root / "bulwark.md"
    body: list[str] = [
        '<section class="bulwark-hero">',
        '<div class="bulwark-hero-copy">',
        '<div class="ara-eyebrow">Barracks</div>',
        '<h2 class="ara-h">Bulwark</h2>',
        '<p class="ara-sub">Unified guard doctrine for filesystem, network, '
        'agent tools, approvals, canaries, and Rhema. Edits to '
        '<span class="mono">bulwark.md</span> hot-reload for active sessions.</p>',
        '</div>',
    ]

    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        body.append(
            '<div class="ara-empty" '
            f'style="border-left:3px solid var({cls});margin-bottom:16px">{e(flash)}</div>'
        )

    if not bw_path.exists():
        body.append(
            '<div class="ara-empty">'
            f'<p>No <span class="mono">bulwark.md</span> yet at '
            f'<span class="mono">{e(str(bw_path))}</span>.</p>'
            '<p>Run <span class="mono">ara bulwark migrate</span> to '
            'synthesize one from existing WALLS / overrides / Rhema, or '
            'create the file by hand.</p></section>'
        )
        return _page("Bulwark", "bulwark", "\n".join(body), token)

    try:
        bw = bw_mod.parse_file(bw_path)
    except Exception as exc:
        body.append(
            f'<div class="ara-empty">Parse error: <span class="mono">{e(str(exc))}</span></div>'
            '</section>'
        )
        return _page("Bulwark", "bulwark", "\n".join(body), token)

    if bw.parse_errors:
        body.append('<div class="ara-empty" style="border-left:3px solid var(--error);margin-bottom:12px">')
        body.append('<strong>Parse warnings:</strong><ul>')
        for err in bw.parse_errors[:20]:
            body.append(f'<li><span class="mono">{e(err)}</span></li>')
        body.append('</ul></div>')

    try:
        equipped = armors_mod.active_armor_name(state_root)
    except Exception:  # noqa: BLE001
        equipped = None

    action_counts: dict[str, int] = {}
    layer_counts: dict[str, int] = {}
    for r in bw.rules:
        action_counts[r.action] = action_counts.get(r.action, 0) + 1
        family = r.layer.split(":", 1)[0]
        layer_counts[family] = layer_counts.get(family, 0) + 1
    top_layers = sorted(layer_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    layer_detail = ", ".join(f"{name}:{count}" for name, count in top_layers) or "no active layers"
    body.append(
        '<div class="bulwark-summary-grid">'
        + _courtyard_signal_card(
            "Rules",
            len(bw.rules),
            f'{len(layer_counts)} layer family/families watched.',
            tone="graph" if bw.rules else "neutral",
        )
        + _courtyard_signal_card(
            "Deny",
            action_counts.get("deny", 0),
            "Hard stops before the agent proceeds.",
            tone="approval" if action_counts.get("deny", 0) else "neutral",
        )
        + _courtyard_signal_card(
            "Approval",
            action_counts.get("approval", 0),
            "Operator gates before escalation.",
            tone="stream" if action_counts.get("approval", 0) else "neutral",
        )
        + _courtyard_signal_card(
            "Active armor",
            equipped or "global",
            f'Layer mix: {e(layer_detail)}.',
            tone="live" if equipped else "neutral",
        )
        + '</div>'
    )

    # ---- pill styling helpers ---------------------------------------

    def _pill(label: str, href: str, active: bool) -> str:
        if active:
            bg = "color-mix(in srgb, var(--primary) 22%, transparent)"
            border = "var(--primary)"
            fg = "var(--primary)"
        else:
            bg = "transparent"
            border = "var(--outline-variant)"
            fg = "var(--on-surface-variant)"
        return (
            f'<a href="{e(href)}" style="padding:4px 12px;'
            f'border:1px solid {border};background:{bg};color:{fg};'
            f'border-radius:12px;text-decoration:none;font-size:12px;'
            f'font-weight:500">{e(label)}</a>'
        )

    # ---- filter pills -----------------------------------------------

    layers = ["all", "net", "fs", "nav", "exec", "perm", "url", "env", "ask", "reply", "oc", "acp"]
    body.append('<div style="margin:16px 0 8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">')
    body.append('<span style="color:var(--text-mute);margin-right:8px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em">scope:</span>')
    for s in ("all", "global", "armor"):
        body.append(_pill(s, f"?scope={s}&layer={layer_filter}", scope_filter == s))
    body.append('</div>')
    body.append('<div style="margin:0 0 16px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">')
    body.append('<span style="color:var(--text-mute);margin-right:8px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em">layer:</span>')
    for L in layers:
        body.append(_pill(L, f"?scope={scope_filter}&layer={L}", layer_filter == L))
    body.append('</div>')

    # ---- partition rules --------------------------------------------

    def _layer_family(r: bw_mod.Rule) -> str:
        return r.layer.split(":", 1)[0]

    def _applies_to_armor(r: bw_mod.Rule, name: str) -> bool:
        return bw_mod.scope_matches(r.scope, name) and not r.scope.include_global

    def _layer_ok(r: bw_mod.Rule) -> bool:
        return layer_filter == "all" or _layer_family(r) == layer_filter

    global_rules = [r for r in bw.rules if r.scope.include_global and _layer_ok(r)]
    equipped_rules: list[bw_mod.Rule] = []
    if equipped:
        equipped_rules = [r for r in bw.rules if _applies_to_armor(r, equipped) and _layer_ok(r)]

    # Other armor sections — group by scope raw string.
    other_armor_groups: dict[str, list[bw_mod.Rule]] = {}
    all_armor_names: list[str] = []
    try:
        all_armor_names = [a.name for a in armors_mod.list_armors(state_root)]
    except Exception:  # noqa: BLE001
        pass
    for name in all_armor_names:
        if name == equipped:
            continue
        rules = [r for r in bw.rules if _applies_to_armor(r, name) and _layer_ok(r)]
        if rules:
            other_armor_groups[name] = rules

    # Scope filter: if user chose "global" only show globals; "armor" only show armor sections.
    show_global = scope_filter in ("all", "global")
    show_armor = scope_filter in ("all", "armor")

    # ---- action badge -----------------------------------------------

    def _action_badge(action: str) -> str:
        colors = {
            "deny":            ("var(--error)",     "color-mix(in srgb, var(--error) 18%, transparent)"),
            "allow":           ("var(--tertiary)",  "color-mix(in srgb, var(--tertiary) 18%, transparent)"),
            "approval":        ("#86efac",          "color-mix(in srgb, #86efac 18%, transparent)"),
            "rewrite":         ("var(--primary)",   "color-mix(in srgb, var(--primary) 18%, transparent)"),
            "shadow_rewrite":  ("var(--primary)",   "color-mix(in srgb, var(--primary) 12%, transparent)"),
        }
        fg, bg = colors.get(action, ("var(--on-surface-variant)", "transparent"))
        return (
            f'<span style="display:inline-block;padding:2px 10px;'
            f'border-radius:10px;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:600;letter-spacing:0.04em;'
            f'text-transform:uppercase;font-family:var(--font-mono)">{e(action)}</span>'
        )

    def _rule_card(r: bw_mod.Rule) -> str:
        mods = []
        if r.modifiers.ttl_seconds is not None:
            mods.append(f"ttl {r.modifiers.ttl_seconds}s")
        if r.modifiers.max_hits is not None:
            mods.append(f"x{r.modifiers.max_hits}")
        if r.modifiers.then_mode:
            mods.append(f"then:{r.modifiers.then_mode}")
        if r.modifiers.highlight:
            mods.append("highlight")
        regex_tag = (
            ' <span class="bulwark-rule-tag">regex</span>'
            if r.match.is_regex else ''
        )
        action_cls = "".join(
            ch if ch.isalnum() else "-"
            for ch in r.action.lower()
        )
        mods_html = (
            "".join(f'<span class="ara-pill">{e(m)}</span>' for m in mods)
            if mods else
            '<span class="ara-sub">no modifiers</span>'
        )
        return (
            f'<article class="bulwark-rule-card bulwark-rule-{e(action_cls)}">'
            '<div class="bulwark-rule-top">'
            f'{_action_badge(r.action)}'
            f'<span class="mono">{e(r.layer)}</span>'
            '</div>'
            '<div class="bulwark-rule-match">'
            f'<strong>{e(r.match.primary)}</strong>{regex_tag}'
            '</div>'
            f'<div class="bulwark-rule-mods">{mods_html}</div>'
            '</article>'
        )

    def _rule_table(rules: "list[bw_mod.Rule]") -> str:
        if not rules:
            return '<div class="ara-empty" style="font-size:12px">No rules in this group (matching current filters).</div>'
        return '<div class="bulwark-doctrine-grid">' + "".join(
            _rule_card(r) for r in rules
        ) + '</div>'

    # ---- sections ----------------------------------------------------

    section_wrap = (
        'border:1px solid var(--outline-variant);'
        'border-radius:10px;padding:14px 16px;margin-top:14px;'
        'background:color-mix(in srgb,var(--on-surface) 3%,transparent)'
    )

    if show_global:
        body.append(f'<div style="{section_wrap}">')
        body.append(
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">'
            '<h3 style="margin:0;font-size:14px">Global</h3>'
            f'<span style="color:var(--text-mute);font-size:11px">applies to every session · {len(global_rules)} rule(s)</span>'
            '</div>'
        )
        body.append(_rule_table(global_rules))
        body.append('</div>')

    if show_armor:
        if equipped:
            body.append(f'<div style="{section_wrap}">')
            body.append(
                '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">'
                f'<h3 style="margin:0;font-size:14px">Equipped armor — {e(equipped)}</h3>'
                f'<span style="color:var(--tertiary);font-size:11px;font-weight:600">● active</span>'
                f'<span style="color:var(--text-mute);font-size:11px">{len(equipped_rules)} rule(s)</span>'
                '</div>'
            )
            body.append(_rule_table(equipped_rules))
            body.append('</div>')
        else:
            body.append(
                f'<div style="{section_wrap};border-style:dashed">'
                '<p style="margin:0;color:var(--text-mute);font-size:12px">'
                'No armor equipped — only global rules are active. '
                '<a href="/armors?t=' + e(token) + '">Equip an armor</a> to load its rules.'
                '</p></div>'
            )

        if other_armor_groups:
            body.append(
                '<details style="margin-top:14px">'
                f'<summary style="cursor:pointer;color:var(--text-mute);font-size:12px;padding:8px 0">'
                f'Other armor sets ({len(other_armor_groups)}) — click to expand</summary>'
            )
            for name, rules in other_armor_groups.items():
                body.append(f'<div style="{section_wrap};border-style:dashed;opacity:0.85">')
                body.append(
                    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">'
                    f'<h3 style="margin:0;font-size:14px">Armor — {e(name)}</h3>'
                    f'<span style="color:var(--text-mute);font-size:11px">not equipped · {len(rules)} rule(s)</span>'
                    '</div>'
                )
                body.append(_rule_table(rules))
                body.append('</div>')
            body.append('</details>')

    # ---- add-rule form ----------------------------------------------
    other_armors_for_select = [
        name for name in all_armor_names if name != equipped
    ]
    armor_options = ""
    if equipped:
        armor_options += f'<option value="{e(equipped)}">{e(equipped)} (equipped)</option>'
    for name in other_armors_for_select:
        armor_options += f'<option value="{e(name)}">{e(name)}</option>'

    input_css = (
        "padding:8px;font-family:var(--font-mono);font-size:12px;"
        "background:var(--surface-container-lowest);color:var(--on-surface);"
        "border:1px solid var(--outline-variant);border-radius:6px;"
        "accent-color:var(--primary)"
    )

    builder_html = f"""
<style>
  #bw-builder input[type=number]::-webkit-inner-spin-button,
  #bw-builder input[type=number]::-webkit-outer-spin-button {{
    filter: invert(1) brightness(0.8); opacity: 0.7;
  }}
  #bw-builder input[type=checkbox] {{ accent-color: var(--primary); }}
  #bw-builder select {{ accent-color: var(--primary); }}
  #bw-builder option {{ background: var(--surface-container-lowest); color: var(--on-surface); }}
</style>
<details class="bulwark-builder-shell" style="{section_wrap};margin-top:24px">
<summary style="cursor:pointer;font-size:14px;font-weight:600;padding:2px 0">
Add rule — guided builder
<span style="color:var(--text-mute);font-weight:400;font-size:11px;margin-left:8px">
hover any field for a hint
</span>
</summary>

<div style="display:flex;gap:4px;margin:14px 0 8px;border-bottom:1px solid var(--outline-variant)">
<a href="#" id="bw-mode-rule"
   style="padding:6px 14px;border-bottom:2px solid var(--primary);color:var(--primary);
   font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.08em;
   text-decoration:none;cursor:pointer">Rule</a>
<a href="#" id="bw-mode-canary"
   style="padding:6px 14px;border-bottom:2px solid transparent;color:var(--text-mute);
   font-size:12px;text-transform:uppercase;letter-spacing:0.08em;text-decoration:none;
   cursor:pointer" title="Seed a honeytoken credential file AND auto-add a deny-reply rule that fires if the value is ever echoed in an LLM response.">Canary</a>
</div>

<!-- ======= CANARY MODE ======= -->
<form method="POST" action="/bulwark/add-canary" id="bw-canary-form"
  style="display:none;grid-template-columns:160px 1fr;gap:10px 14px;align-items:center;margin-top:6px;max-width:820px">
<input type="hidden" name="token" value="{e(token)}">

<label title="Template shape for the seeded credential file. Each kind writes a differently-shaped fake secret so the agent thinks it found the real thing."
  style="font-size:12px;color:var(--text-mute)">Kind</label>
<select name="kind" style="{input_css}"
  title="aws → AKIA-style + secret_access_key; stripe → sk_live_ prefix; github → ghp_ prefix; openai → sk- prefix; generic → ARA-CANARY prefix with a credentials-looking body.">
<option value="aws">aws (AKIA… / secret_access_key)</option>
<option value="stripe">stripe (sk_live_…)</option>
<option value="github">github (ghp_…)</option>
<option value="openai">openai (sk-…)</option>
<option value="generic" selected>generic</option>
</select>

<label title="Absolute path where the honeytoken file will be written. Must NOT already exist — pick somewhere the agent is likely to look (~/.aws/credentials, ~/.github/token, /etc/secrets/…)."
  style="font-size:12px;color:var(--text-mute)">Path</label>
<input name="path" type="text" style="{input_css}"
  placeholder="/home/user/.aws/credentials"
  title="Absolute path to seed. Existing files are rejected; pick a fresh location or delete the existing file first.">

<label style="font-size:12px;color:var(--text-mute)"
  title="Optional human-readable context for later triage.">Note</label>
<input name="note" type="text" style="{input_css}"
  placeholder="optional — e.g. 'staging sandbox'">

<div></div>
<div style="display:flex;flex-direction:column;gap:6px;margin-top:4px">
<p style="margin:0;font-size:11px;color:var(--text-mute);line-height:1.6">
This seeds a fake credential at the path AND auto-appends a
<span class="mono">deny reply regex:&lt;canary-value&gt;</span> rule under
<span class="mono">## global</span>. If any agent ever reads the file and
echoes the value into a model reply (indicating exfiltration intent), the
MITM blocks the response and a <span class="mono">canary_hit</span>
event fires on <a href="/audit?t={e(token)}&verdict=flag">/audit</a>.
</p>
<button type="submit" class="ara-btn ara-btn-primary"
  style="align-self:flex-start;margin-top:4px">Seed canary + add reply rule</button>
</div>
</form>

<!-- ======= RULE MODE ======= -->
<form method="POST" action="/bulwark/add-guided" id="bw-builder"
  style="display:grid;grid-template-columns:160px 1fr;gap:10px 14px;align-items:center;margin-top:14px;max-width:820px">
<input type="hidden" name="token" value="{e(token)}">

<label title="Who this rule applies to. Global rules fire for every session. Armor-scoped rules fire only when that armor is equipped."
  style="font-size:12px;color:var(--text-mute)">Scope</label>
<select name="scope_kind" id="bw-scope-kind" style="{input_css}"
  title="Global: all sessions. Armor: only when the selected armor is equipped. Custom: write your own scope expression (supports +/- / wildcards like armor: a+b-c).">
<option value="global">global</option>
<option value="armor">armor (pick one)</option>
<option value="custom">custom expression</option>
</select>

<label title="Which armor the rule is scoped to. Only used when Scope = armor."
  style="font-size:12px;color:var(--text-mute);display:none" id="bw-armor-label">Armor</label>
<select name="scope_armor" id="bw-scope-armor" style="{input_css}"
  title="The armor whose sessions this rule applies to. Only relevant when Scope = armor.">
{armor_options}
</select>

<label title="Custom scope expression. Advanced syntax supporting union (+), subtract (-), wildcards (*). Examples: armor: orion+atlas, armor: *-legacy, global -armor: sandbox."
  style="font-size:12px;color:var(--text-mute);display:none" id="bw-custom-label">Custom scope</label>
<input name="scope_custom" id="bw-scope-custom" type="text" style="{input_css};display:none"
  placeholder="armor: orion+atlas  OR  global -armor: sandbox"
  title="Advanced scope expression. Use + for union, - for subtract, * as a wildcard. Evaluated left-to-right.">

<label title="What happens when the rule matches. Allow permits; deny blocks; approval pauses and prompts the operator; rewrite/shadow_rewrite mutate content (only on url/ask/reply/oc layers)."
  style="font-size:12px;color:var(--text-mute)">Action</label>
<select name="action" id="bw-action" style="{input_css}"
  title="allow = permit, deny = block, approval = pause for operator, rewrite = mutate before forwarding (url/ask/reply/oc layers only), shadow_rewrite = log the alternate version for A/B without changing live behavior.">
<option value="allow" title="Let this request through and stop evaluation.">allow</option>
<option value="deny" selected title="Block this request. For net/fs/exec: syscall fails. For url/ask/reply: a synthetic deny reply is returned.">deny</option>
<option value="approval" title="Pause and prompt the operator; operator decision unblocks or aborts.">approval</option>
<option value="rewrite" title="Mutate the payload before forwarding upstream. Only valid on url/ask/reply/oc layers.">rewrite</option>
<option value="shadow_rewrite" title="Log what rewrite would produce without changing live behavior. A/B testing.">shadow_rewrite</option>
</select>

<label title="The category of event this rule matches against. Layer determines which field of an event is compared to your match pattern."
  style="font-size:12px;color:var(--text-mute)">Layer</label>
<select name="layer" id="bw-layer" style="{input_css}"
  title="Layer = what kind of event fires the rule. Each layer matches on a specific target: net→host, fs→file path, nav→directory path, perm→path being chmod/chattr'd, exec→binary path, url→HTTP request URL, env→env var name, ask→request body, reply→response body, oc→agent tool call, acp→openclaw gateway method call.">
<option value="net" title="Outbound network connection. Match on host[:port], e.g. *.api.openai.com or 1.2.3.4:443.">net</option>
<option value="fs" title="File content access (read/write/unlink/rename/stat/open). Match on absolute path glob.">fs</option>
<option value="nav" title="Directory operations (readdir/opendir/chdir/mkdir/rmdir). Match on directory path glob.">nav</option>
<option value="exec" title="Process launch. Match on binary path glob — e.g. /usr/bin/curl or **/python*.">exec</option>
<option value="perm" title="Permission/attribute changes (chmod, chattr, setxattr, setuid, …). Match on path glob; mechanism-agnostic.">perm</option>
<option value="url" title="HTTP request through MITM. Match on URL glob, optionally prefixed with HTTP method.">url</option>
<option value="env" title="Environment variable scrub at worker spawn. Match on variable name (case-sensitive).">env</option>
<option value="ask" title="Content inside the request payload (user prompt). Substring match by default; prefix with regex: for full regex.">ask</option>
<option value="reply" title="Content inside the response payload (model output). Substring match by default; prefix with regex: for full regex.">reply</option>
<option value="oc" title="Agent built-in tool call (any). Choose oc:&lt;tool&gt; below to narrow to a specific tool like oc:bash.">oc (any tool)</option>
<option value="oc:read" title="Agent's Read tool. Match on target file path glob.">oc:read</option>
<option value="oc:write" title="Agent's Write tool. Match on target file path glob.">oc:write</option>
<option value="oc:edit" title="Agent's Edit tool. Match on target file path glob.">oc:edit</option>
<option value="oc:bash" title="Agent's Bash tool. Match on command string or regex: pattern.">oc:bash</option>
<option value="oc:webfetch" title="Agent's WebFetch tool. Match on URL glob.">oc:webfetch</option>
<option value="oc:websearch" title="Agent's WebSearch tool. Match on query glob.">oc:websearch</option>
<option value="acp" title="OpenClaw gateway method call (ACP — Agent Control Protocol). Match on method name like chat.inject, cron.add, sessions.reset. Fires on every WebSocket req frame crossing the gateway tap. See docs/GATEWAY-WS-TAP-PLAN.md.">acp (gateway method)</option>
</select>

<label title="The pattern that the event target is compared against. Supports shell-style globs: * matches one path segment, ** matches any number."
  style="font-size:12px;color:var(--text-mute)">Match</label>
<input name="match" id="bw-match" type="text" style="{input_css}"
  placeholder="/etc/** or *.api.openai.com or POST https://api.x.com/*"
  title="Pattern to compare the event target against. Glob syntax: * = one segment, ** = any depth. To match a full regex instead, tick the regex box below.">

<label style="font-size:12px;color:var(--text-mute)"
  title="Treat the match field as a Python regex instead of a glob. Useful for complex patterns.">Use regex</label>
<div>
<input type="checkbox" name="is_regex" id="bw-regex" value="1"
  title="When checked, 'Match' is treated as a Python regex. Example: ^https://.*\\.evil\\.(com|net)/.*$">
<span style="color:var(--text-mute);font-size:11px">treat Match as regex</span>
</div>

<label style="font-size:12px;color:var(--text-mute)"
  title="Optional extra behaviors stacked on the rule.">Modifiers</label>
<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
<input name="mod_ttl" type="text" placeholder="ttl (30s/15m/2h/7d)"
  style="{input_css};width:160px"
  title="Auto-expire this rule after the given duration. Format: 30s, 15m, 2h, 7d. Empty = permanent.">
<input name="mod_count" type="number" min="1" placeholder="x N (fire N times)"
  style="{input_css};width:170px"
  title="Rule becomes inert after firing N times. Empty = unlimited.">
<select name="mod_then" style="{input_css};width:180px"
  title="What happens after an xN counter exhausts. Default = fall through to counter_exhaust_default (deny).">
<option value="" title="Use the file-level counter_exhaust_default (deny).">then: (default)</option>
<option value="deny" title="Once exhausted, hard-flip to deny on further matches.">then:deny</option>
<option value="allow" title="Once exhausted, flip to allow on further matches.">then:allow</option>
<option value="approval" title="Once exhausted, send to operator for each further match.">then:approval</option>
<option value="fallthrough" title="Once exhausted, the rule becomes invisible — lower-priority rules get to match.">then:fallthrough</option>
</select>
<label style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-mute)"
  title="Tag firings of this rule in the UI / audit stream without changing the allow/deny decision.">
<input type="checkbox" name="mod_highlight" value="1"
  title="When on, every firing of this rule is visually tagged in audit and live views. Decision itself is unchanged.">
highlight
</label>
</div>

<label style="font-size:12px;color:var(--text-mute)"
  title="Optional human-readable note appended to the rule as a comment.">Note</label>
<input name="note" type="text" style="{input_css}"
  placeholder="optional reason / context"
  title="Free-form note. Stored as '# &lt;note&gt;' at the end of the rule line so other operators know why it exists.">

<div></div>
<div style="display:flex;flex-direction:column;gap:6px;margin-top:4px">
<div style="font-size:11px;color:var(--text-mute)">preview (what will be written to bulwark.md):</div>
<code id="bw-preview" style="{input_css};white-space:pre-wrap;min-height:32px;color:var(--primary)"></code>
<button type="submit" class="ara-btn ara-btn-primary" style="align-self:flex-start;margin-top:4px">
Append to bulwark.md
</button>
</div>
</form>

<script>
(function() {{
  const f = document.getElementById('bw-builder');
  if (!f) return;
  const kind = document.getElementById('bw-scope-kind');
  const armorLbl = document.getElementById('bw-armor-label');
  const armorSel = document.getElementById('bw-scope-armor');
  const customInp = document.getElementById('bw-scope-custom');
  const customLbl = document.getElementById('bw-custom-label');
  const action = document.getElementById('bw-action');
  const layer = document.getElementById('bw-layer');
  const match = document.getElementById('bw-match');
  const regex = document.getElementById('bw-regex');
  const modTtl = f.elements['mod_ttl'];
  const modCount = f.elements['mod_count'];
  const modThen = f.elements['mod_then'];
  const modHl = f.elements['mod_highlight'];
  const note = f.elements['note'];
  const preview = document.getElementById('bw-preview');

  function updateVis() {{
    const k = kind.value;
    armorLbl.style.display = (k === 'armor') ? '' : 'none';
    armorSel.style.display = (k === 'armor') ? '' : 'none';
    customInp.style.display = (k === 'custom') ? '' : 'none';
    customLbl.style.display = (k === 'custom') ? '' : 'none';
  }}

  function scopeText() {{
    const k = kind.value;
    if (k === 'global') return 'global';
    if (k === 'armor') return 'armor: ' + (armorSel.value || '<pick-armor>');
    return customInp.value || '<custom-scope>';
  }}

  function ruleText() {{
    const mods = [];
    if (modTtl.value.trim()) mods.push('ttl ' + modTtl.value.trim());
    if (modCount.value) mods.push('x' + modCount.value);
    if (modThen.value) mods.push('then:' + modThen.value);
    if (modHl.checked) mods.push('highlight');
    const modStr = mods.length ? ' ' + mods.join(' ') : '';
    const m = match.value || '<match>';
    const mStr = regex.checked ? 'regex:' + m : m;
    const base = action.value + modStr + ' ' + layer.value + ' ' + mStr;
    return note.value ? base + '  # ' + note.value : base;
  }}

  function update() {{
    updateVis();
    preview.textContent = '## ' + scopeText() + '\\n' + ruleText();
  }}

  f.addEventListener('input', update);
  f.addEventListener('change', update);
  update();

  // Mode toggle: Rule ↔ Canary.
  const ruleForm = document.getElementById('bw-builder');
  const canaryForm = document.getElementById('bw-canary-form');
  const modeRule = document.getElementById('bw-mode-rule');
  const modeCanary = document.getElementById('bw-mode-canary');
  function setMode(mode) {{
    const ruleActive = mode === 'rule';
    ruleForm.style.display = ruleActive ? 'grid' : 'none';
    canaryForm.style.display = ruleActive ? 'none' : 'grid';
    modeRule.style.borderBottomColor = ruleActive ? 'var(--primary)' : 'transparent';
    modeRule.style.color = ruleActive ? 'var(--primary)' : 'var(--text-mute)';
    modeRule.style.fontWeight = ruleActive ? '600' : '400';
    modeCanary.style.borderBottomColor = ruleActive ? 'transparent' : 'var(--primary)';
    modeCanary.style.color = ruleActive ? 'var(--text-mute)' : 'var(--primary)';
    modeCanary.style.fontWeight = ruleActive ? '400' : '600';
  }}
  if (modeRule && modeCanary) {{
    modeRule.addEventListener('click', (e) => {{ e.preventDefault(); setMode('rule'); }});
    modeCanary.addEventListener('click', (e) => {{ e.preventDefault(); setMode('canary'); }});
  }}
}})();
</script>
</details>
"""
    body.append(builder_html)

    # Keep the raw-DSL form as a secondary option — power users will want
    # it for quick edits without opening the builder.
    body.append(
        '<details style="' + section_wrap + ';margin-top:16px">'
        '<summary style="cursor:pointer;font-size:13px;color:var(--text-mute);padding:2px 0">'
        'Raw DSL (power user)</summary>'
        '<form method="POST" action="/bulwark/add" '
        'style="display:flex;flex-direction:column;gap:8px;max-width:760px;margin-top:12px">'
        f'<input type="hidden" name="token" value="{e(token)}">'
        '<label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-mute)">'
        'Scope'
        f'<input name="scope" type="text" placeholder="global  OR  armor: {e(equipped or "<name>")}" style="{input_css}"></label>'
        '<label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-mute)">'
        'Rule line'
        f'<input name="rule" type="text" placeholder="deny url POST https://api.evil.com/*" style="{input_css}"></label>'
        '<button type="submit" class="ara-btn ara-btn-primary" '
        'style="align-self:flex-start;margin-top:4px">Append to bulwark.md</button>'
        '</form></details>'
    )

    body.append(
        f'<p style="margin-top:16px;font-size:11px;color:var(--text-mute)">'
        f'Config: counter_exhaust_default = <span class="mono">'
        f'{e(bw.config.counter_exhaust_default)}</span>.  '
        f'Total rules in file: {len(bw.rules)}.</p>'
    )
    body.append('</section>')
    return _page("Bulwark", "bulwark", "\n".join(body), token)


def render_walls(
    walls_file: Path | None,
    state_root: Path,
    token: str,
    *,
    edit: bool = False,
    flash: str | None = None,
    flash_level: str = "info",
    explain_op: str | None = None,
    explain_target: str | None = None,
    explain_result: str | None = None,
) -> str:
    target, source_label = _resolve_walls_source(state_root, walls_file)
    contents = ""
    walls: Walls | None = None
    issues: list[str] = []
    if target and target.exists():
        contents = target.read_text()
        try:
            walls = parse_file(target)
            issues = validate_file(target, home=None)
        except Exception as exc:
            issues = [f"PARSE ERROR: {exc}"]

    body = [
        '<section>',
        '<div class="ara-eyebrow">Policy</div>',
        '<h2 class="ara-h">WALLS</h2>',
        f'<p class="ara-sub">Source: <span class="mono">{e(target) if target else "(none)"}</span> '
        f'<span style="color:var(--text-mute)">· {e(source_label)}</span></p>',
    ]

    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        body.append(
            '<div class="ara-empty" '
            f'style="border-left:3px solid var({cls});margin-bottom:16px">{e(flash)}</div>'
        )

    if walls is not None:
        body.extend([
            '<dl class="ara-dl">',
            f'<dt>deny_path</dt><dd>{len(walls.path_rules)}</dd>',
            f'<dt>deny_command</dt><dd>{len(walls.command_rules)}</dd>',
            f'<dt>approval (total)</dt><dd>{len(walls.approval_rules)}</dd>',
            '</dl>',
        ])
    if issues:
        body.append('<h3 class="ara-eyebrow" style="margin-top:22px">Issues</h3>')
        body.append('<ul class="ara-empty" style="list-style:none;padding-left:0">')
        for i in issues:
            body.append(f'<li style="padding:4px 0">{e(i)}</li>')
        body.append('</ul>')

    if not target:
        body.extend([
            '<div class="ara-empty" style="margin-top:16px">'
            'No WALLS file to edit. Equip an armor set to create one.</div>',
            '</section>',
        ])
        return _page("WALLS", "walls", "".join(body), token)

    if edit:
        body.extend([
            '<h3 class="ara-eyebrow" style="margin-top:22px">Edit source</h3>',
            f'<form method="post" action="/walls/save?t={e(token)}">',
            f'<input type="hidden" name="target" value="{e(target)}">',
            '<textarea name="contents" spellcheck="false" '
            'style="width:100%;min-height:420px;padding:14px 16px;'
            'background:var(--surface-container-lowest);color:var(--on-surface);'
            'border:0;border-radius:var(--radius-md);resize:vertical;'
            'font-family:var(--font-mono);font-size:12px;line-height:1.55;'
            'box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--outline-variant) 20%,transparent);">'
            f'{e(contents)}</textarea>',
            '<div style="margin-top:12px;display:flex;gap:8px">',
            '<button class="ara-btn ara-btn-primary" type="submit">Save</button>',
            f'<a class="ara-btn ara-btn-ghost" href="/walls?t={e(token)}" '
            'style="text-decoration:none;display:inline-flex;align-items:center">Cancel</a>',
            '</div>',
            '<p class="ara-sub" style="margin-top:12px">'
            'Saving writes the file atomically. Supervisor mtime-watch will hot-reload '
            'into any running session automatically.</p>',
            '</form>',
            '</section>',
        ])
    else:
        body.extend([
            '<div style="margin-top:22px;display:flex;gap:8px;align-items:baseline">',
            '<h3 class="ara-eyebrow" style="margin:0">Source</h3>',
            f'<a class="ara-btn ara-btn-ghost" href="/walls?t={e(token)}&edit=1" '
            'style="text-decoration:none;font-size:10px;padding:4px 10px">Edit</a>',
            '</div>',
            f'<pre class="ara-code">{e(contents)}</pre>',
            '</section>',
        ])

    # Explain form: test a query against the equipped armor's policy without
    # running anything. Lives at the bottom of /walls so operators can
    # explore rules against a source they're already looking at.
    body.append(
        '<section style="margin-top:22px">'
        '<div class="ara-eyebrow">Policy query</div>'
        '<h2 class="ara-h">Explain</h2>'
        '<p class="ara-sub">Dry-run a query against the equipped armor\'s '
        'rules + overrides. Nothing is executed.</p>'
        f'<form class="ara-form" method="get" action="/walls">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        '<label>op<select name="explain_op">'
        + "".join(
            f'<option value="{v}"{" selected" if explain_op == v else ""}>{v}</option>'
            for v in ("fs.read", "fs.open", "fs.readdir", "exec.spawn", "net.connect")
        ) +
        '</select></label>'
        '<label>target<input type="text" name="explain_target" '
        f'value="{e(explain_target or "")}" '
        'placeholder="/path or command or host:port" style="min-width:260px">'
        '</label>'
        '<button class="ara-btn ara-btn-primary" type="submit">Explain</button>'
        '</form>'
        + (explain_result or "")
        + '</section>'
    )
    return _page("WALLS", "walls", "".join(body), token)


def _summarize_text_value(text: object, status: object = None) -> str:
    """Return a readable one-line summary for raw text or JSON-shaped text."""
    if not isinstance(text, str) or not text.strip():
        return ""
    raw = text.strip()
    preview = None
    try:
        parsed = json.loads(raw)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            preview = err.get("message") or err.get("type")
        if not preview and isinstance(parsed, dict):
            c = parsed.get("content")
            if isinstance(c, list) and c:
                first = c[0]
                if isinstance(first, dict):
                    preview = first.get("text") or first.get("type")
        if not preview and isinstance(parsed, dict):
            ch = parsed.get("choices")
            if isinstance(ch, list) and ch:
                first_choice = ch[0]
                msg = first_choice.get("message") if isinstance(first_choice, dict) else None
                if isinstance(msg, dict):
                    preview = msg.get("content")
        if not preview and isinstance(parsed, dict):
            name = parsed.get("name") or parsed.get("tool") or parsed.get("id")
            desc = (
                parsed.get("description")
                or parsed.get("message")
                or parsed.get("status")
            )
            if name and desc:
                preview = f"{name}: {desc}"
            elif name:
                preview = name
    except (TypeError, ValueError):
        preview = raw
    if preview is None:
        preview = raw
    preview = str(preview).strip().splitlines()[0][:200]
    if status is not None:
        return f"[{status}] {preview}"
    return preview


def _summarize_body_text(body_text: object, status: object = None) -> str:
    """Return a readable one-line summary for captured HTTP body_text."""
    return _summarize_text_value(body_text, status)


def _summarize_payload(role: str, payload: dict) -> str:
    """Return one short, human-readable line summarizing a transcript turn.
    Walks through several common payload shapes (OpenAI chat, Anthropic
    messages, raw HTTP capture) and falls through to body_text when the
    structured paths come up empty."""
    try:
        if role == "request" or role.endswith(".out"):
            body = _turn_body(payload)
            msgs = body.get("messages") or payload.get("messages") or []
            if isinstance(msgs, list):
                for m in reversed(msgs):
                    c = m.get("content") if isinstance(m, dict) else None
                    if isinstance(c, str) and c.strip():
                        return _summarize_text_value(c)
                    if isinstance(c, list):
                        for blk in c:
                            t = blk.get("text") if isinstance(blk, dict) else None
                            if isinstance(t, str) and t.strip():
                                return _summarize_text_value(t)
            url = payload.get("url")
            body_summary = _summarize_body_text(payload.get("body_text"))
            if body_summary:
                return body_summary
            if url:
                method = payload.get("method", "POST")
                return f"{method} {url}"
        if role == "response" or role.endswith(".in"):
            stream = _stream_parts(payload)
            if stream["answer"].strip():
                return _summarize_text_value(stream["answer"])
            if stream["thoughts"].strip():
                return "thoughts: " + stream["thoughts"].strip().splitlines()[0][:190]
            for k in ("content", "choices"):
                v = payload.get(k)
                if isinstance(v, list) and v:
                    first = v[0]
                    if isinstance(first, dict):
                        msg = first.get("message") if "message" in first else first
                        if isinstance(msg, dict):
                            txt = msg.get("content")
                            if isinstance(txt, str) and txt.strip():
                                return _summarize_text_value(txt)
                            if isinstance(txt, list):
                                for blk in txt:
                                    if isinstance(blk, dict) and blk.get("text"):
                                        return _summarize_text_value(str(blk["text"]))
            # HTTP-capture fallback: surface status code and a one-line peek
            # at body_text so error responses don't render as just "{".
            status = payload.get("status")
            body_text = payload.get("body_text")
            body_summary = _summarize_body_text(body_text, status)
            if body_summary:
                return body_summary
            if status is not None:
                return f"[status {status}]"
        if role == "rhema_deny":
            return f"DENY {payload.get('rule_id','')} — {payload.get('redirect','')[:160]}"
    except Exception:
        pass
    return ""


def _turn_body(payload: dict) -> dict:
    """Return the logical request/response body for both MITM and Tier-A rows."""
    body = payload.get("body") if isinstance(payload.get("body"), dict) else None
    if isinstance(body, dict):
        return body
    # Tier-A transcript rows are already the logical body, e.g. Hermes'
    # ``{"messages": ..., "model": ...}`` request payload.
    if any(k in payload for k in ("messages", "input", "model", "tools", "chunks")):
        return payload
    return {}


def _stream_chunks(payload: dict) -> list[dict]:
    body = _turn_body(payload)
    chunks = body.get("chunks") if isinstance(body, dict) else None
    if isinstance(chunks, list):
        return [c for c in chunks if isinstance(c, dict)]
    return []


def _stream_parts(payload: dict) -> dict:
    """Extract human-readable parts from OpenAI-compatible stream chunks."""
    answer: list[str] = []
    thoughts: list[str] = []
    tool_calls: list[dict] = []
    usage = None
    finish = ""
    model = str(payload.get("model") or "")
    for chunk in _stream_chunks(payload):
        if not model and isinstance(chunk.get("model"), str):
            model = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if isinstance(choice.get("finish_reason"), str):
                finish = choice["finish_reason"]
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str):
                answer.append(content)
            reasoning = (
                delta.get("reasoning_content")
                or delta.get("reasoning")
                or delta.get("thinking")
            )
            if isinstance(reasoning, str):
                thoughts.append(reasoning)
            tc = delta.get("tool_calls")
            if isinstance(tc, list):
                tool_calls.extend(t for t in tc if isinstance(t, dict))
    return {
        "answer": "".join(answer),
        "thoughts": "".join(thoughts),
        "tool_calls": tool_calls,
        "usage": usage,
        "finish": finish,
        "model": model,
        "chunks": _stream_chunks(payload),
    }


def _parse_sse(text: str) -> list[dict]:
    """Parse a Server-Sent-Events blob into [{event, data}] records.
    Tolerant of partial/truncated streams."""
    out: list[dict] = []
    cur_event = None
    cur_data_lines: list[str] = []
    def _flush():
        if cur_event is None and not cur_data_lines:
            return
        data_raw = "\n".join(cur_data_lines)
        parsed_json = None
        try:
            parsed_json = json.loads(data_raw) if data_raw else None
        except Exception:
            parsed_json = None
        out.append({
            "event": cur_event or "message",
            "raw": data_raw,
            "data": parsed_json,
        })
    for line in text.splitlines():
        if not line.strip():
            _flush()
            cur_event = None
            cur_data_lines = []
            continue
        if line.startswith(":"):
            continue  # SSE comment
        if line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            cur_data_lines.append(line[5:].lstrip())
    if cur_event is not None or cur_data_lines:
        _flush()
    return out


def _assemble_assistant_text(events: list[dict]) -> str:
    """Walk parsed SSE events and stitch together the assistant's output text
    from response.output_text.delta + reasoning deltas. Works for OpenAI
    Responses API style streams; tolerant of other shapes."""
    out_parts: list[str] = []
    for ev in events:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        # OpenAI Responses API streaming shape
        delta = d.get("delta")
        if isinstance(delta, str):
            out_parts.append(delta)
            continue
        # Chat completions streaming shape: choices[].delta.content
        choices = d.get("choices")
        if isinstance(choices, list):
            for ch in choices:
                delta2 = ch.get("delta") if isinstance(ch, dict) else None
                if isinstance(delta2, dict):
                    c = delta2.get("content")
                    if isinstance(c, str):
                        out_parts.append(c)
        # Fully-formed response.output[*].content[*].text
        response = d.get("response")
        if isinstance(response, dict):
            for o in response.get("output") or []:
                if isinstance(o, dict):
                    for c in o.get("content") or []:
                        if isinstance(c, dict):
                            t = c.get("text")
                            if isinstance(t, str):
                                out_parts.append(t)
    return "".join(out_parts)


_RHEMA_UID = [0]
def _next_uid() -> str:
    _RHEMA_UID[0] += 1
    return f"rh{_RHEMA_UID[0]}"


def _render_rhema_detail(rec: dict) -> str:
    """Produce the expanded panel for a single transcript turn with three
    tabs: Readable (default, chat-bubble view), Structured (SSE events
    broken out), Raw (literal body_text / sorted JSON)."""
    role = str(rec.get("role") or "")
    payload = rec.get("payload") or {}
    uid = _next_uid()

    if not isinstance(payload, dict):
        cls = "rh-bubble-assistant" if str(role).startswith("response") else "rh-bubble-user"
        label = "assistant" if str(role).startswith("response") else str(role or "turn")
        return (
            '<div class="rhema-detail">'
            + _bubble(label, str(payload), cls)
            + '</div>'
        )

    is_response = (role == "response") or role.endswith(".in")
    is_request = (role == "request") or role.endswith(".out")
    is_deny = role == "rhema_deny"

    # -- common pieces
    body_text = payload.get("body_text") if is_response else None
    sse_events = (
        _parse_sse(body_text)
        if isinstance(body_text, str) and body_text and "event:" in body_text[:200]
        else []
    )

    readable = _render_readable_pane(role, payload, sse_events, is_response, is_request, is_deny)
    structured = _render_structured_pane(role, payload, sse_events, body_text)
    raw = _render_raw_pane(payload, body_text)

    tabs = (
        f'<div class="rhema-tabs" data-uid="{uid}">'
        f'<button class="rhema-tab rhema-tab-active" data-pane="r">Readable</button>'
        f'<button class="rhema-tab" data-pane="s">Structured</button>'
        f'<button class="rhema-tab" data-pane="x">Raw</button>'
        '</div>'
        f'<div class="rhema-panes" data-uid="{uid}">'
        f'<div class="rhema-pane rhema-pane-r rhema-pane-active">{readable}</div>'
        f'<div class="rhema-pane rhema-pane-s" hidden>{structured}</div>'
        f'<div class="rhema-pane rhema-pane-x" hidden>{raw}</div>'
        '</div>'
    )
    return (
        '<div class="rhema-detail">'
        + tabs
        + _rhema_selection_toolbar_html()
        + '</div>'
    )


# ---- Readable: chat-bubble / card view -----------------------------------

def _render_readable_pane(
    role: str, payload: dict, sse_events: list[dict],
    is_response: bool, is_request: bool, is_deny: bool,
) -> str:
    parts: list[str] = []

    # --- header strip: status chip + url, always visible
    url = payload.get("url") or ""
    if url:
        chip = str(payload.get("status", "") if is_response else payload.get("method", ""))
        parts.append(
            '<div class="rhema-detail-head">'
            + (f'<span class="rhema-chip">{e(chip)}</span>' if chip else "")
            + f'<span class="mono">{e(url)}</span>'
            '</div>'
        )

    # --- RESPONSE: assembled assistant bubble + tool calls + footer
    stream = _stream_parts(payload) if is_response else {}
    if is_response and (sse_events or stream.get("chunks")):
        assembled = (
            str(stream.get("answer") or "")
            if stream.get("chunks") else _assemble_assistant_text(sse_events)
        ).strip()
        tool_calls = (
            stream.get("tool_calls") or _assemble_tool_calls(sse_events)
        )
        reasoning = (
            str(stream.get("thoughts") or "")
            if stream.get("chunks") else _assemble_reasoning(sse_events)
        )
        usage, finish = (
            (stream.get("usage"), str(stream.get("finish") or ""))
            if stream.get("chunks") else _assemble_finish(sse_events)
        )
        model = payload.get("model") or stream.get("model") or _extract_model(sse_events)

        if assembled:
            parts.append(
                '<div class="rh-bubble rh-bubble-assistant ara-selectable">'
                '<div class="rh-bubble-head">'
                f'<span class="rh-role-tag">assistant</span>'
                + (f'<span class="ara-sub mono">{e(str(model))}</span>' if model else "")
                + '</div>'
                f'<div class="rh-bubble-body">{e(assembled)}</div>'
                '</div>'
            )
        if tool_calls:
            for tc in tool_calls:
                args = tc.get("arguments") or tc.get("input") or {}
                args_pretty = (
                    json.dumps(args, indent=2, sort_keys=True)
                    if isinstance(args, (dict, list)) else str(args)
                )
                parts.append(
                    '<div class="rh-tool ara-selectable">'
                    '<div class="rh-bubble-head">'
                    '<span class="rh-role-tag rh-role-tool">tool_call</span>'
                    f'<span class="mono">{e(tc.get("name") or tc.get("type") or "")}</span>'
                    '</div>'
                    f'<pre class="ara-code">{e(args_pretty)}</pre>'
                    '</div>'
                )
        if reasoning:
            parts.append(
                '<details class="rh-reasoning" open>'
                '<summary><span class="rh-role-tag rh-role-reason">thoughts</span>'
                f'<span class="ara-sub">{len(reasoning)} chars</span></summary>'
                f'<div class="rh-bubble-body ara-selectable">{e(reasoning)}</div>'
                '</details>'
            )
        if usage or finish:
            badges = []
            if finish:
                badges.append(f'<span class="rh-badge">finish: {e(finish)}</span>')
            if isinstance(usage, dict):
                for k in ("input_tokens", "output_tokens", "total_tokens",
                          "prompt_tokens", "completion_tokens"):
                    if k in usage:
                        badges.append(f'<span class="rh-badge">{e(k)}: {e(str(usage[k]))}</span>')
            if badges:
                parts.append('<div class="rh-footer">' + "".join(badges) + '</div>')
        if not parts or (len(parts) == 1 and url):
            # No usable text was extracted — fall back to structured view hint.
            parts.append(
                '<div class="ara-empty">'
                'No assistant text extracted. Switch to <strong>Structured</strong> '
                'for the raw SSE events.</div>'
            )
        return "".join(parts)

    # --- REQUEST: messages first (what the operator cares about), then
    # system prompt collapsed, then tool schemas collapsed.
    if is_request:
        body = _turn_body(payload)
        if isinstance(body, dict) and body:
            model = body.get("model") or payload.get("model") or ""
            meta_badges: list[str] = []
            if model:
                meta_badges.append(f'<span class="rh-badge">model: {e(str(model))}</span>')

            # --- Conversation messages (the real signal).
            messages = body.get("messages") or body.get("input") or []
            msg_count = 0
            if isinstance(messages, list):
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    mrole = m.get("role") or "user"
                    mcontent = m.get("content")
                    text = _content_to_text(mcontent)
                    if text:
                        cls = {
                            "user": "rh-bubble-user",
                            "assistant": "rh-bubble-assistant",
                            "system": "rh-bubble-system",
                            "tool": "rh-bubble-tool",
                        }.get(mrole, "rh-bubble-user")
                        # Keep user/assistant/tool messages fully expanded
                        # regardless of length — this is the real content.
                        parts.append(_bubble(mrole, text, cls))
                        msg_count += 1
            meta_badges.append(
                f'<span class="rh-badge">{msg_count} msg{"s" if msg_count != 1 else ""}</span>'
            )

            # --- System prompt / instructions (collapsed if large).
            sys_msg = body.get("instructions") or body.get("system")
            sys_text = ""
            if isinstance(sys_msg, str) and sys_msg.strip():
                sys_text = sys_msg
            elif isinstance(sys_msg, list):
                chunks = []
                for b in sys_msg:
                    txt = b.get("text") if isinstance(b, dict) else None
                    if isinstance(txt, str) and txt.strip():
                        chunks.append(txt)
                sys_text = "\n\n".join(chunks)
            if sys_text:
                parts.append(_bubble_collapsible(
                    "system prompt", sys_text, "rh-bubble-system",
                ))
                meta_badges.append(
                    f'<span class="rh-badge">system: {len(sys_text):,} chars</span>'
                )

            # --- Tool schemas. Names visible as badges; schemas collapsed.
            tools = body.get("tools")
            tool_names: list[str] = []
            tool_schemas_pretty = ""
            if isinstance(tools, list) and tools:
                for t in tools:
                    if isinstance(t, dict):
                        n = (
                            t.get("name")
                            or t.get("function", {}).get("name")
                            or t.get("type")
                        )
                        if n:
                            tool_names.append(str(n))
                try:
                    tool_schemas_pretty = json.dumps(
                        tools, indent=2, sort_keys=True,
                    )
                except (TypeError, ValueError):
                    tool_schemas_pretty = str(tools)
            if tool_names:
                meta_badges.append(
                    f'<span class="rh-badge">{len(tool_names)} tools</span>'
                )

            # Emit meta strip at the top for quick scanning.
            if meta_badges:
                parts.insert(0, '<div class="rh-footer">' + "".join(meta_badges) + '</div>')

            if tool_names:
                parts.append(
                    '<details class="rh-tools-block">'
                    '<summary class="rh-bubble-head">'
                    '<span class="rh-role-tag rh-role-tool">tools</span>'
                    f'<span class="ara-sub" style="margin-left:6px">'
                    f'{len(tool_names)} available · click to expand schemas'
                    '</span></summary>'
                    '<div class="rh-footer" style="margin-top:8px">'
                    + "".join(f'<span class="rh-badge">{e(n)}</span>'
                              for n in tool_names)
                    + '</div>'
                    + (
                        f'<pre class="ara-code">{e(tool_schemas_pretty[:200_000])}</pre>'
                        if tool_schemas_pretty else ""
                    )
                    + '</details>'
                )
        else:
            # No parsed body — show body_text inline.
            bt = payload.get("body_text")
            if bt:
                parts.append(
                    '<pre class="ara-code ara-selectable">'
                    f'{e(str(bt)[:4000])}</pre>'
                )
        if not parts or (len(parts) == 1 and url):
            parts.append('<div class="ara-empty">Empty request body.</div>')
        return "".join(parts)

    # --- DENY / other: pretty card
    if is_deny:
        parts.append(
            '<div class="rh-deny ara-selectable">'
            f'<div class="rh-bubble-head">'
            f'<span class="rh-role-tag rh-role-deny">denied</span>'
            f'<span class="mono">{e(str(payload.get("rule_id","")))}</span>'
            '</div>'
            '<div class="rh-deny-grid">'
            f'<div><span class="rh-label">pattern</span>'
            f'<span class="mono">{e(str(payload.get("pattern","")))}</span></div>'
            f'<div><span class="rh-label">scope</span>'
            f'<span class="mono">{e(str(payload.get("scope","")))}</span></div>'
            '</div>'
            f'<div class="rh-label">redirect</div>'
            f'<div class="rh-bubble-body">{e(str(payload.get("redirect","")))}</div>'
            + (f'<div class="ara-sub" style="margin-top:6px">{e(str(payload.get("reason","")))}</div>'
               if payload.get("reason") else "")
            + '</div>'
        )
        return "".join(parts)

    # Unknown role — just show the JSON.
    return (
        '<pre class="ara-code ara-selectable">'
        + e(json.dumps(payload, indent=2, sort_keys=True))
        + '</pre>'
    )


def _bubble(role: str, text: str, cls: str) -> str:
    return (
        f'<div class="rh-bubble {cls} ara-selectable">'
        f'<div class="rh-bubble-head">'
        f'<span class="rh-role-tag">{e(role)}</span></div>'
        f'<div class="rh-bubble-body">{e(text)}</div>'
        f'</div>'
    )


def _bubble_collapsible(
    role: str, text: str, cls: str, *,
    preview_chars: int = 180,
    open_if_under: int = 1200,
) -> str:
    """Like ``_bubble`` but wraps the body in a <details> when the text is
    long. Short bubbles render open so the reader still sees the whole
    thing; long ones (system prompts, tool schema dumps) default closed
    with a size summary + preview snippet so the actual user messages
    don't get buried under 35KB of boilerplate."""
    text = text or ""
    chars = len(text)
    if chars <= open_if_under:
        return _bubble(role, text, cls)
    preview = text[:preview_chars].replace("\n", " ").strip()
    summary = f"{role} · {chars:,} chars · click to expand"
    return (
        f'<details class="rh-bubble rh-collapsible {cls} ara-selectable">'
        '<summary class="rh-bubble-head">'
        f'<span class="rh-role-tag">{e(role)}</span>'
        f'<span class="ara-sub" style="margin-left:6px">{e(summary)}</span>'
        '</summary>'
        f'<div class="rh-bubble-preview ara-sub">{e(preview)}…</div>'
        f'<div class="rh-bubble-body">{e(text)}</div>'
        '</details>'
    )


def _content_to_text(content) -> str:
    """Normalize OpenAI/Anthropic content shapes into a single string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text") or b.get("input_text") or b.get("content")
                if isinstance(t, str):
                    out.append(t)
                elif isinstance(t, list):
                    out.append(_content_to_text(t))
        return "\n".join(out)
    return str(content)


def _assemble_tool_calls(events: list[dict]) -> list[dict]:
    """Collect tool_call-shaped items from SSE events."""
    out = []
    for ev in events:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        # OpenAI Responses API
        item = d.get("item")
        if isinstance(item, dict) and item.get("type") in ("function_call", "tool_use"):
            out.append(item)
        # Chat Completions
        choices = d.get("choices")
        if isinstance(choices, list):
            for c in choices:
                if isinstance(c, dict):
                    delta = c.get("delta") or c.get("message") or {}
                    if isinstance(delta, dict):
                        tc = delta.get("tool_calls")
                        if isinstance(tc, list):
                            out.extend(tc)
    return out


def _assemble_reasoning(events: list[dict]) -> str:
    parts = []
    for ev in events:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        if ev.get("event") in ("response.reasoning_summary_part.added",
                               "response.reasoning.delta",
                               "response.reasoning_text.delta"):
            r = d.get("delta") or d.get("part") or d.get("text")
            if isinstance(r, str):
                parts.append(r)
            elif isinstance(r, dict):
                t = r.get("text")
                if isinstance(t, str):
                    parts.append(t)
        choices = d.get("choices")
        if isinstance(choices, list):
            for ch in choices:
                delta = ch.get("delta") if isinstance(ch, dict) else None
                if isinstance(delta, dict):
                    r = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thinking")
                    )
                    if isinstance(r, str):
                        parts.append(r)
    return "".join(parts)


def _assemble_finish(events: list[dict]) -> tuple[dict | None, str]:
    usage = None
    finish = ""
    for ev in events:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        if isinstance(d.get("usage"), dict):
            usage = d["usage"]
        r = d.get("response")
        if isinstance(r, dict):
            if isinstance(r.get("usage"), dict):
                usage = r["usage"]
            st = r.get("status")
            if isinstance(st, str) and st in ("completed", "incomplete", "failed", "cancelled"):
                finish = st
    return usage, finish


def _extract_model(events: list[dict]) -> str:
    for ev in events:
        d = ev.get("data")
        if isinstance(d, dict):
            r = d.get("response")
            if isinstance(r, dict) and isinstance(r.get("model"), str):
                return r["model"]
    return ""


# ---- Structured: SSE events + headers ------------------------------------

def _render_structured_pane(
    role: str, payload: dict, sse_events: list[dict], body_text,
) -> str:
    parts: list[str] = []
    url = payload.get("url") or ""
    if url:
        parts.append(
            '<div class="rhema-detail-head">'
            f'<span class="rhema-chip">{e(str(payload.get("status","") or payload.get("method","")))}</span>'
            f'<span class="mono">{e(url)}</span>'
            '</div>'
        )
    headers = payload.get("headers") or {}
    if headers:
        parts.append(
            '<details class="rhema-event" open>'
            '<summary><span class="rhema-event-name">headers</span>'
            f'<span class="rhema-event-hint ara-sub">{len(headers)} entries</span>'
            '</summary>'
            '<pre class="ara-code ara-selectable">'
            f'{e(json.dumps(headers, indent=2, sort_keys=True))}</pre>'
            '</details>'
        )

    stream = _stream_parts(payload)
    chunks = stream.get("chunks") or []
    if chunks:
        answer = str(stream.get("answer") or "")
        thoughts = str(stream.get("thoughts") or "")
        parts.append(
            '<div class="rhema-section-head">stream chunks '
            f'({len(chunks)})</div>'
            '<div class="rh-footer">'
            f'<span class="rh-badge">answer: {len(answer):,} chars</span>'
            f'<span class="rh-badge">thoughts: {len(thoughts):,} chars</span>'
            + (
                f'<span class="rh-badge">finish: {e(str(stream.get("finish")))}</span>'
                if stream.get("finish") else ""
            )
            + '</div>'
        )
        rows = []
        for idx, chunk in enumerate(chunks):
            hint = ""
            choices = chunk.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                if isinstance(delta, dict):
                    if isinstance(delta.get("content"), str) and delta.get("content"):
                        hint = "answer: " + delta["content"][:80].replace("\n", "\\n")
                    elif isinstance(delta.get("reasoning_content"), str) and delta.get("reasoning_content"):
                        hint = "thoughts: " + delta["reasoning_content"][:80].replace("\n", "\\n")
            rows.append(
                '<details class="rhema-event">'
                f'<summary><span class="rhema-event-name">chunk {idx + 1}</span>'
                f'<span class="rhema-event-hint ara-sub">{e(hint)}</span>'
                '</summary>'
                '<pre class="ara-code ara-selectable">'
                f'{e(json.dumps(chunk, indent=2, sort_keys=True))}</pre>'
                '</details>'
            )
        parts.append('<div class="rhema-events">' + "".join(rows) + '</div>')
        return "".join(parts)

    if sse_events:
        rows = []
        for ev in sse_events:
            name = ev.get("event") or "message"
            data = ev.get("data")
            pretty = (
                json.dumps(data, indent=2, sort_keys=True)
                if data is not None else ev.get("raw", "")
            )
            hint = ""
            if isinstance(data, dict):
                if isinstance(data.get("delta"), str):
                    hint = data["delta"][:120]
                elif isinstance(data.get("response"), dict):
                    hint = f"status={data['response'].get('status','')}"
                elif isinstance(data.get("type"), str):
                    hint = data["type"]
            rows.append(
                '<details class="rhema-event">'
                f'<summary><span class="rhema-event-name">{e(name)}</span>'
                f'<span class="rhema-event-hint ara-sub">{e(hint)}</span>'
                '</summary>'
                f'<pre class="ara-code ara-selectable">{e(pretty)}</pre>'
                '</details>'
            )
        parts.append(
            '<div class="rhema-events">'
            f'<div class="rhema-section-head">SSE events ({len(sse_events)})</div>'
            + "".join(rows) + '</div>'
        )
        return "".join(parts)

    # Non-SSE: structured JSON of the body if available, else everything.
    body = payload.get("body")
    if isinstance(body, (dict, list)):
        parts.append(
            '<div class="rhema-section-head">body (parsed)</div>'
            f'<pre class="ara-code ara-selectable">'
            f'{e(json.dumps(body, indent=2, sort_keys=True))}</pre>'
        )
    elif isinstance(body_text, str):
        parts.append(
            '<div class="rhema-section-head">body_text</div>'
            f'<pre class="ara-code ara-selectable">{e(body_text)}</pre>'
        )
    else:
        parts.append(
            '<pre class="ara-code ara-selectable">'
            f'{e(json.dumps(payload, indent=2, sort_keys=True))}</pre>'
        )
    return "".join(parts)


# ---- Raw: plain body -----------------------------------------------------

def _render_raw_pane(payload: dict, body_text) -> str:
    if isinstance(body_text, str):
        return f'<pre class="ara-code ara-selectable">{e(body_text)}</pre>'
    return (
        '<pre class="ara-code ara-selectable">'
        + e(json.dumps(payload, indent=2, sort_keys=True))
        + '</pre>'
    )


def _rhema_selection_toolbar_html() -> str:
    """Floating tooltip that appears when the operator selects text inside
    a .ara-selectable block. Offers one-click rule creation from the
    selection. JS lives once on the page (see _RHEMA_SELECTION_JS)."""
    return (
        '<div class="rhema-sel-toolbar" hidden>'
        '<button type="button" data-action="rule">'
        '<span class="mono" style="font-size:10px">+ Rhema rule from selection</span>'
        '</button>'
        '</div>'
    )


_RHEMA_TAB_JS = """
<script>
(function() {
  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.rhema-tab');
    if (!btn) return;
    const uid = btn.parentElement.dataset.uid;
    const pane = btn.dataset.pane;
    // Toggle sibling tabs
    btn.parentElement.querySelectorAll('.rhema-tab').forEach(b =>
      b.classList.toggle('rhema-tab-active', b === btn));
    // Toggle panes
    const panes = document.querySelector(`.rhema-panes[data-uid="${uid}"]`);
    if (!panes) return;
    panes.querySelectorAll('.rhema-pane').forEach(p => {
      const match = p.classList.contains(`rhema-pane-${pane}`);
      p.hidden = !match;
      p.classList.toggle('rhema-pane-active', match);
    });
  });
})();
</script>
"""


_RHEMA_SELECTION_JS = """
<script>
(function() {
  const selToolbar = document.createElement('div');
  selToolbar.className = 'rhema-sel-float';
  selToolbar.hidden = true;
  selToolbar.innerHTML =
    '<button type="button" id="rhema-sel-btn">+ Rhema rule from selection</button>';
  document.body.appendChild(selToolbar);

  let currentText = '';
  function place(rect) {
    selToolbar.style.top = (window.scrollY + rect.bottom + 6) + 'px';
    selToolbar.style.left = (window.scrollX + rect.left) + 'px';
    selToolbar.hidden = false;
  }
  function hide() { selToolbar.hidden = true; }

  document.addEventListener('selectionchange', () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) { hide(); return; }
    const text = sel.toString().trim();
    if (text.length < 3 || text.length > 500) { hide(); return; }
    // Only show if the selection is inside a .ara-selectable node.
    let node = sel.anchorNode;
    while (node && node.nodeType === 3) node = node.parentNode;
    if (!node || !node.closest || !node.closest('.ara-selectable')) { hide(); return; }
    currentText = text;
    try {
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      place(rect);
    } catch (e) { hide(); }
  });

  document.getElementById('rhema-sel-btn').addEventListener('click', () => {
    if (!currentText) return;
    // Open prefilled form: submit a POST to /rhema/add with the selection
    // as the pattern. Scope defaults to response because most selections
    // will come from the assistant reply panel. Pattern kind = substring
    // so whatever was copy-selected matches literally.
    const token = new URLSearchParams(location.search).get('t') || '';
    const redirect = prompt(
      "Redirect text to give the agent (what you want it to do instead):",
      "Rework your reply — that phrasing is disallowed.",
    );
    if (redirect === null) { hide(); return; }
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/rhema/add?t=' + encodeURIComponent(token);
    const add = (name, value) => {
      const i = document.createElement('input');
      i.type = 'hidden'; i.name = name; i.value = value;
      form.appendChild(i);
    };
    add('scope', 'response');
    add('pattern_kind', 'substring');
    add('pattern', currentText);
    add('action', 'deny');
    add('redirect', redirect);
    add('reason', 'created from dashboard selection');
    document.body.appendChild(form);
    form.submit();
  });
})();
</script>
"""


_RHEMA_DECK_FILTER_JS = """
<script id="rhema-deck-filter">(function(){
  const stream = document.getElementById("rhema-stream");
  if (!stream) return;
  const search = document.getElementById("rhema-search");
  const countEl = document.getElementById("rhema-count");
  const showAll = document.getElementById("rhema-show-all");
  let mode = "both";
  let expanded = false;
  function apply() {
    const q = (search?.value || "").trim().toLowerCase();
    let visible = 0;
    const pairs = stream.querySelectorAll(".rhema-pair");
    pairs.forEach((p, i) => {
      const ask = p.dataset.ask || "";
      const reply = p.dataset.reply || "";
      const inAsk = ask.indexOf(q) > -1;
      const inReply = reply.indexOf(q) > -1;
      let matches = true;
      if (q) {
        if (mode === "ask") matches = inAsk;
        else if (mode === "reply") matches = inReply;
        else matches = inAsk || inReply;
      }
      const defaultHidden = p.dataset.hiddenDefault === "1";
      const shouldShow = matches && (expanded || !defaultHidden || q);
      p.style.display = shouldShow ? "" : "none";
      if (shouldShow) visible++;
    });
    if (countEl) {
      const total = pairs.length;
      countEl.textContent = q
        ? "matched " + visible + " of " + total + " pair(s)"
        : "showing " + visible + " of " + total + " pair(s)";
    }
  }
  if (search) search.addEventListener("input", apply);
  stream.parentElement.querySelectorAll("[data-rhema-filter]").forEach(btn => {
    btn.addEventListener("click", () => {
      stream.parentElement.querySelectorAll("[data-rhema-filter]")
        .forEach(b => b.classList.remove("audit-chip-on"));
      btn.classList.add("audit-chip-on");
      mode = btn.dataset.rhemaFilter;
      apply();
    });
  });
  if (showAll) showAll.addEventListener("click", () => {
    expanded = !expanded;
    showAll.textContent = expanded ? "▲ collapse to recent" : showAll.dataset.moreLabel || showAll.textContent;
    if (expanded && !showAll.dataset.moreLabel) {
      showAll.dataset.moreLabel = "▼ show more";
    }
    apply();
  });
  apply();
})();</script>
"""


def _render_rhema_tabs(tab: str, token: str) -> str:
    def _tab(label: str, key: str) -> str:
        active = tab == key
        cls = "rhema-mode-tab" + (" rhema-mode-tab-active" if active else "")
        return (
            f'<a class="{cls}" href="?tab={e(key)}&t={e(token)}">'
            f'{e(label)}</a>'
        )

    return (
        '<nav class="rhema-mode-tabs" aria-label="Rhema views">'
        + _tab("Live stream", "live")
        + _tab("Envelope", "envelope")
        + _tab("Shadows", "shadows")
        + '</nav>'
    )


def _render_rhema_live_overview(
    *, turn_count: int, pair_count: int, rule_count: int,
) -> str:
    pair_label = f"{pair_count} pair" + ("" if pair_count == 1 else "s")
    turn_label = f"{turn_count} turn" + ("" if turn_count == 1 else "s")
    rule_label = f"{rule_count} rule" + ("" if rule_count == 1 else "s")
    return (
        '<div class="rhema-overview" aria-label="Rhema live summary">'
        '<div class="rhema-overview-card">'
        '<span>Interceptions</span>'
        f'<strong>{e(pair_label)}</strong>'
        '<small>ASK / REPLY pairs</small>'
        '</div>'
        '<div class="rhema-overview-card">'
        '<span>Turns</span>'
        f'<strong>{e(turn_label)}</strong>'
        '<small>latest transcript rows</small>'
        '</div>'
        '<div class="rhema-overview-card">'
        '<span>Rules</span>'
        f'<strong>{e(rule_label)}</strong>'
        '<small>Rhema mediation rules</small>'
        '</div>'
        '</div>'
    )


def render_rhema(
    state_root: Path, token: str, *,
    flash: str | None = None, flash_level: str = "info",
    channel_filter: str | None = None,
    status_filter: str | None = None,
    rule_filter: str | None = None,
    sort_key: str | None = None,
    tab: str = "live",
    armor_override: str | None = None,
    absolute_override: str | None = None,
    edit: bool = False,
) -> str:
    """Rhema — envelope-layer surface.

    Two tabs:
      * **live** — conversation stream + firing Bulwark rules with
        provenance badges.
      * **envelope** — the envelope manifest editor (formerly the
        standalone ABSOLUTE page).
    """
    from .. import rhema as rhema_mod

    tab = tab if tab in ("live", "envelope", "shadows") else "live"
    tabs_html = _render_rhema_tabs(tab, token)

    if tab in ("envelope", "shadows"):
        if tab == "envelope":
            body_frag = _render_envelope_body(
                state_root, token,
                edit=edit, flash=flash, flash_level=flash_level,
                armor_override=armor_override,
                absolute_override=absolute_override,
            )
            title = "Rhema · Envelope"
            eyebrow = "Envelope"
        else:
            body_frag = _render_shadows_body(
                state_root, token, flash=flash, flash_level=flash_level,
                channel_filter=channel_filter,
                status_filter=status_filter,
                rule_filter=rule_filter,
                sort_key=sort_key,
            )
            title = "Rhema · Shadows"
            eyebrow = "Shadows"
        header = (
            '<section class="rhema-workbench">'
            f'<div class="ara-eyebrow">{eyebrow}</div>'
            '<h2 class="ara-h">Rhema</h2>'
            '<p class="ara-sub">Rhema is the envelope layer — the live '
            'conversation stream, the manifest of what the envelope can '
            'contain, and the A/B evidence log for shadow rewrites. '
            'Bulwark rules fire here with a provenance badge.</p>'
            + tabs_html +
            '</section>'
        )
        return _page(title, "rhema", header + body_frag, token)

    rules = rhema_mod.load(state_root)

    # --- Live conversation stream: walk every session's transcript.jsonl
    # and aggregate ask/reply turns across sessions. Transcript stores
    # can grow very large, so the live page tail-scans each transcript
    # and keeps the most-recent turns globally. Large visible turns are
    # still lazy-loaded by byte offset.
    sessions_dir = state_root / "sessions"
    # (ts, session_short, full_session_id, line_idx, offset, record)
    turns: list[tuple[float, str, str, int | None, int | None, dict]] = []
    if sessions_dir.is_dir():
        transcript_paths = [
            s / "transcript.jsonl"
            for s in sessions_dir.iterdir()
            if s.is_dir() and (s / "transcript.jsonl").is_file()
        ]
        transcript_paths.sort(
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        for t in transcript_paths[:_RHEMA_LIVE_SESSION_SCAN_LIMIT]:
            s = t.parent
            for offset, rec in _read_jsonl_tail_with_offsets(t):
                turns.append((rec.get("ts") or 0, s.name[:10], s.name, None, offset, rec))
    turns.sort(key=lambda x: x[0], reverse=True)
    turns = turns[:_RHEMA_LIVE_MAX_TURNS]
    turn_count = len(turns)
    pair_count = 0
    # Channel-filter bar inputs: scan turns for any channel tag.
    seen_channels_for_filter: list[str] = []
    untagged_present = False
    seen_set_rhema: set = set()
    for tup in turns:
        ch = tup[5].get("channel")
        if ch and ch not in seen_set_rhema:
            seen_set_rhema.add(ch)
            seen_channels_for_filter.append(ch)
        if not ch:
            untagged_present = True
    if untagged_present:
        seen_channels_for_filter.append("_untagged")
    if channel_filter == "_untagged":
        turns = [t for t in turns if not t[4].get("channel")]
    elif channel_filter:
        turns = [t for t in turns if t[4].get("channel") == channel_filter]

    if not turns:
        stream_html = (
            '<div class="ara-empty">'
            'No transcript turns recorded yet. Once a worker spawned with '
            '<code>ara launch</code> hits an LLM endpoint, its requests + '
            'responses appear here.</div>'
        )
    else:
        # Group ask/reply pairs: turns are newest-first; pair each "ask"
        # (request/tool call) with the closest subsequent "reply" so the
        # default-visible window is the last 2 completed exchanges.
        ASK_ROLES = {"request", "tool_call", "tool_use"}
        REPLY_ROLES = {"response", "rhema_deny"}
        pairs: list[dict] = []  # newest pair first
        pending_reply = None
        for ts, sid_short, sid_full, line_idx, offset, rec in turns:
            role = str(rec.get("role") or "")
            base = role.split(".")[0]
            if base in REPLY_ROLES or role.endswith(".in"):
                pending_reply = (ts, sid_short, sid_full, line_idx, offset, rec)
            elif base in ASK_ROLES or role.endswith(".out"):
                pairs.append({
                    "ts": ts, "sid": sid_short, "sid_full": sid_full,
                    "ask": rec, "ask_line": line_idx,
                    "ask_offset": offset,
                    "reply": pending_reply[5] if pending_reply else None,
                    "reply_sid": pending_reply[1] if pending_reply else None,
                    "reply_sid_full": pending_reply[2] if pending_reply else None,
                    "reply_line": pending_reply[3] if pending_reply else None,
                    "reply_offset": pending_reply[4] if pending_reply else None,
                })
                pending_reply = None
        # Any trailing orphan reply without an ask = a leading incomplete pair.
        if pending_reply:
            pairs.append({
                "ts": pending_reply[0], "sid": pending_reply[1],
                "sid_full": pending_reply[2],
                "ask": None, "ask_line": None,
                "ask_offset": None,
                "reply": pending_reply[5],
                "reply_sid": pending_reply[1],
                "reply_sid_full": pending_reply[2],
                "reply_line": pending_reply[3],
                "reply_offset": pending_reply[4],
            })
        pairs.sort(
            key=lambda p: (
                1 if p.get("ask") is not None and p.get("reply") is not None else 0,
                float(p.get("ts") or 0),
            ),
            reverse=True,
        )
        default_visible = 9
        pair_count = len(pairs)

        def _render_turn(
            role: str, ts: float, sid: str, rec: dict,
            *, sid_full: str = "", line_idx: int | None = None,
            offset: int | None = None,
            lazy: bool = False,
        ) -> str:
            model = rec.get("model") or ""
            summary = _summarize_payload(role, rec.get("payload") or {})
            role_label = role.upper()
            tint = "var(--text-mute)"
            base = role.split(".")[0]
            if base == "request" or role.endswith(".out"):
                role_label, tint = "ASK", "var(--primary)"
            elif base == "response" or role.endswith(".in"):
                role_label, tint = "REPLY", "var(--tertiary)"
            elif role == "rhema_deny":
                role_label, tint = "BLOCK", "var(--error)"
            elif base in ("tool_call", "tool_use"):
                role_label, tint = "TOOL", "#d4a53a"
            if lazy and sid_full and (line_idx is not None or offset is not None):
                # Defer body render — JS will fetch on first <details>
                # open and replace the stub. Cheap on initial paint.
                locator = (
                    f"off={int(offset)}" if offset is not None
                    else f"line={int(line_idx or 0)}"
                )
                fetch_url = f'/rhema/turn?sid={e(sid_full)}&{locator}&t={e(token)}'
                detail = (
                    '<div class="rhema-detail-stub" '
                    f'data-fetch-url="{fetch_url}">'
                    '<div class="ara-sub" style="padding:14px 18px">'
                    '(loading…)</div></div>'
                )
            else:
                detail = _render_rhema_detail(rec)
            face_class = "rhema-card-face-ask"
            if role_label == "REPLY":
                face_class = "rhema-card-face-reply"
            elif role_label == "BLOCK":
                face_class = "rhema-card-face-block"
            elif role_label == "TOOL":
                face_class = "rhema-card-face-tool"
            return (
                '<details class="rhema-turn rhema-card-face ' + face_class + '" '
                'data-role="' + e(role_label.lower()) + '"'
                f' data-search="{e((summary or "").lower())}">'
                '<summary class="rhema-summary">'
                f'<span class="mono ara-sub" style="width:6ch">{e(_fmt_ago(ts))}</span>'
                f'<span class="rhema-role" style="color:{tint}">{e(role_label)}</span>'
                f'<a class="mono ara-sub" href="/sessions/{e(sid_full or sid)}?t={e(token)}" '
                f'style="color:inherit" onclick="event.stopPropagation()">{e(sid)}</a>'
                f'<span class="ara-sub">{e(model)}</span>'
                f'<span class="rhema-preview">'
                + (e(summary) or '<span class="ara-sub">(no preview)</span>')
                + '</span>'
                '</summary>'
                + detail +
                '</details>'
            )

        hand_stage_blocks: list[str] = []
        hand_drawer_blocks: list[str] = []
        selected_block = (
            '<div class="ara-empty">No selected envelope yet. Once a pair '
            'is captured, the newest complete ASK / REPLY appears here.</div>'
        )

        def _rule_field(rule: object, field: str, fallback: str = "") -> str:
            if isinstance(rule, dict):
                value = rule.get(field, fallback)
            else:
                value = getattr(rule, field, fallback)
            return str(value or fallback)

        rule_stack_items = []
        for rule in rules[:5]:
            rule_name = _rule_field(rule, "id", "rule")
            rule_scope = _rule_field(rule, "scope", "any")
            rule_action = _rule_field(rule, "action", "watch")
            rule_pattern = _rule_field(rule, "pattern", "")
            rule_stack_items.append(
                '<li>'
                '<span class="mono">' + e(rule_scope) + '</span>'
                '<strong>' + e(rule_name) + '</strong>'
                '<small>' + e(rule_action)
                + (f' · {e(rule_pattern[:72])}' if rule_pattern else '')
                + '</small>'
                '</li>'
            )
        rule_stack_html = (
            '<ul>' + ''.join(rule_stack_items) + '</ul>'
            if rule_stack_items
            else '<div class="rhema-empty-stack">No Rhema-local rules. '
                 'Bulwark remains the active durable rule source.</div>'
        )

        for i, pair in enumerate(pairs):
            visible = i < default_visible
            # Lazy-render the body for non-default-visible pairs so the
            # initial page paint stays small. Operators expand on demand.
            lazy = not visible
            parts: list[str] = []
            combined_search: list[str] = []
            display_summaries: list[str] = []
            card_model = ""
            card_sid = pair.get("sid") or pair.get("reply_sid") or ""
            card_sid_full = pair.get("sid_full") or pair.get("reply_sid_full") or card_sid
            if pair["ask"] is not None:
                a = pair["ask"]
                card_model = str(a.get("model") or card_model)
                a_summary = _summarize_payload(
                    str(a.get("role") or ""), a.get("payload") or {},
                ) or ""
                combined_search.append(a_summary.lower())
                display_summaries.append(a_summary)
                parts.append(_render_turn(
                    str(a.get("role") or ""), a.get("ts") or 0,
                    pair["sid"], a,
                    sid_full=pair.get("sid_full") or "",
                    line_idx=pair.get("ask_line"),
                    offset=pair.get("ask_offset"),
                    lazy=lazy or _json_size_hint(a.get("payload") or {}) > _RHEMA_INLINE_DETAIL_MAX_BYTES,
                ))
            if pair["reply"] is not None:
                r = pair["reply"]
                card_model = str(r.get("model") or card_model)
                card_sid = pair.get("reply_sid") or card_sid
                card_sid_full = pair.get("reply_sid_full") or card_sid_full
                r_summary = _summarize_payload(
                    str(r.get("role") or ""), r.get("payload") or {},
                ) or ""
                combined_search.append(r_summary.lower())
                display_summaries.append(r_summary)
                parts.append(_render_turn(
                    str(r.get("role") or ""), r.get("ts") or 0,
                    pair["reply_sid"] or pair["sid"], r,
                    sid_full=pair.get("reply_sid_full") or pair.get("sid_full") or "",
                    line_idx=pair.get("reply_line"),
                    offset=pair.get("reply_offset"),
                    lazy=lazy or _json_size_hint(r.get("payload") or {}) > _RHEMA_INLINE_DETAIL_MAX_BYTES,
                ))
            card_title = (
                display_summaries[0] if display_summaries else ""
            ) or (display_summaries[1] if len(display_summaries) > 1 else "")
            if not card_title:
                card_title = "(no preview)"
            card_title = card_title[:260]
            card_state = "ASK / REPLY" if len(parts) > 1 else "SINGLE TURN"
            card_meta = (
                '<div class="rhema-card-badges">'
                f'<span>{e(_fmt_ago(pair["ts"]))}</span>'
                f'<a href="/sessions/{e(card_sid_full)}?t={e(token)}">{e(card_sid)}</a>'
                + (f'<span>{e(card_model)}</span>' if card_model else '')
                + '</div>'
            )
            if i == 0:
                selected_block = (
                    '<article class="rhema-selected-card rhema-card">'
                    '<div class="rhema-card-top">'
                    '<div>'
                    '<span class="rhema-card-kicker">Current envelope</span>'
                    f'<strong>{e(card_state)}</strong>'
                    '</div>'
                    + card_meta
                    + '</div>'
                    f'<p class="rhema-card-title">{e(card_title)}</p>'
                    '<div class="rhema-card-faces">'
                    + "".join(parts)
                    + '</div>'
                    '</article>'
                )
            in_stage = i < 7
            row_class = (
                "rhema-hand-card rhema-ledger-row rhema-pair"
                if in_stage else
                "rhema-hand-drawer-row rhema-ledger-row rhema-pair"
            )
            if i == 0:
                row_class += " rhema-ledger-row-active"
            card_html = (
                f'<article class="{row_class}" '
                f'data-ask="{e(combined_search[0] if len(combined_search) > 0 else "")}" '
                f'data-reply="{e(combined_search[1] if len(combined_search) > 1 else "")}"'
                + ('' if visible else ' data-hidden-default="1"')
                + '>'
                '<div class="rhema-ledger-top">'
                '<div class="rhema-ledger-main">'
                f'<span class="rhema-card-kicker">{e(card_state)}</span>'
                f'<strong class="rhema-ledger-title">{e(card_title)}</strong>'
                '</div>'
                + card_meta
                + '</div>'
                '<div class="rhema-ledger-faces">'
                + "".join(parts)
                + '</div>'
                '</article>'
            )
            if in_stage:
                hand_stage_blocks.append(card_html)
            else:
                hand_drawer_blocks.append(card_html)

        hidden_count = max(0, len(pairs) - default_visible)
        toolbar = (
            '<div class="rhema-stream-toolbar">'
            '<div class="rhema-toolbar-title">'
            '<span>Flow filters</span>'
            '<strong>ASK / REPLY stream</strong>'
            '</div>'
            '<input type="search" id="rhema-search" '
            'placeholder="filter ask / reply by text…" autocomplete="off">'
            '<div class="rhema-filter-chips">'
            '<button type="button" class="audit-chip audit-chip-on" '
            'data-rhema-filter="both">both</button>'
            '<button type="button" class="audit-chip" '
            'data-rhema-filter="ask">ask only</button>'
            '<button type="button" class="audit-chip" '
            'data-rhema-filter="reply">reply only</button>'
            '</div>'
            f'<span class="ara-sub rhema-count-readout" id="rhema-count">'
            f'showing {min(default_visible, len(pairs))} of {len(pairs)} pair(s)</span>'
            + (
                f'<button type="button" class="audit-chip" id="rhema-show-all">'
                f'▼ show {hidden_count} more</button>'
                if hidden_count else ''
            )
            + '</div>'
        )

        stream_html = (
            '<div class="rhema-stream rhema-codex" id="rhema-stream">'
            '<div class="rhema-codex-layout">'
            '<div class="rhema-focus-grid">'
            '<aside class="rhema-codex-controls">'
            '<div class="rhema-live-pulse">'
            '<span aria-hidden="true"></span>'
            '<div><strong>Live thoughts</strong><small>Rhema envelope capture</small></div>'
            '</div>'
            + _render_rhema_live_overview(
                turn_count=turn_count,
                pair_count=pair_count,
                rule_count=len(rules),
            )
            + toolbar
            + '</aside>'
            '<aside class="rhema-inspector" aria-label="Selected envelope">'
            '<div class="rhema-section-head">'
            '<span>Selected envelope</span>'
            '<strong>Current envelope</strong>'
            '</div>'
            + selected_block
            + '<div class="rhema-rule-stack">'
            '<div class="rhema-section-head">'
            '<span>Rule stack</span>'
            f'<strong>{e(str(len(rules)))} active</strong>'
            '</div>'
            + rule_stack_html
            + '</div>'
            + '</aside>'
            + '</div>'
            '<section class="rhema-ledger rhema-envelope-hand" aria-label="Live Rhema envelope hand">'
            '<div class="rhema-section-head">'
            '<span>Envelope hand</span>'
            f'<strong>{e(str(pair_count))} live card'
            f'{"" if pair_count == 1 else "s"}</strong>'
            '</div>'
            '<div class="rhema-hand-stage">'
            + "".join(hand_stage_blocks)
            + '</div>'
            '<div class="rhema-hand-drawer">'
            '<div class="rhema-section-head">'
            '<span>Inspector drawer</span>'
            f'<strong>{e(str(max(0, pair_count - len(hand_stage_blocks))))} archived</strong>'
            '</div>'
            '<div class="rhema-hand-drawer-list">'
            + "".join(hand_drawer_blocks)
            + '</div>'
            + '</div>'
            + '</section>'
            + '</div>'
            + _RHEMA_DECK_FILTER_JS
        )

    # --- Rules list.
    rule_rows = []
    for r in sorted(rules, key=lambda r: r.added_at, reverse=True):
        last_fired = _fmt_ago(r.last_fired_at) if r.last_fired_at else "—"
        rule_rows.append(
            '<tr>'
            f'<td class="mono" data-label="ID">{e(r.id)}</td>'
            f'<td data-label="Scope">{e(r.scope)}</td>'
            f'<td class="mono" data-label="Pattern">'
            f'<span style="opacity:0.6">[{e(r.pattern_kind)}]</span> {e(r.pattern)}'
            '</td>'
            f'<td data-label="Action"><span class="rhema-action rhema-action-{e(r.action)}">'
            f'{e(r.action.upper())}</span></td>'
            f'<td class="mono" data-label="Redirect">{e((r.redirect or "")[:60])}</td>'
            f'<td class="num" data-label="Fires">{r.fire_count}</td>'
            f'<td class="ara-sub" data-label="Last">{e(last_fired)}</td>'
            f'<td data-label="">'
            f'<form method="post" action="/rhema/{e(r.id)}/remove?t={e(token)}" '
            'style="display:inline" onsubmit="return confirm(\'Remove this rule?\')">'
            '<button class="ara-btn ara-btn-bad" type="submit">×</button>'
            '</form>'
            '</td>'
            '</tr>'
        )
    rules_html = (
        '<div class="ara-empty">No Rhema rules yet. Add one below to start '
        'mediating what the agent says.</div>'
        if not rule_rows
        else '<table class="ara-table"><thead><tr>'
             '<th>ID</th><th>Scope</th><th>Pattern</th>'
             '<th>Action</th><th>Redirect</th>'
             '<th class="num">Fires</th><th>Last</th><th></th>'
             '</tr></thead>'
             f'<tbody>{"".join(rule_rows)}</tbody></table>'
    )

    flash_html = ""
    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        flash_html = (
            '<div class="ara-empty" '
            f'style="border-left:3px solid var({cls});margin-bottom:14px">{e(flash)}</div>'
        )

    # Collect recent classifier + canary hits across all sessions so we
    # can show a compact "flags" banner at the top of the live stream.
    flag_events: list[dict] = []
    if sessions_dir.is_dir():
        import time as _t
        cutoff = _t.time() - 3600  # last hour
        for s in sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime,
                        reverse=True)[:10]:
            events_path = s / "events.jsonl"
            if not events_path.exists():
                continue
            try:
                # Tail-scan — only the last ~100 events per session.
                lines = events_path.read_text().splitlines()[-100:]
            except OSError:
                continue
            for line in lines:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("kind") not in ("classifier_hit", "canary_hit", "bulwark_deny"):
                    continue
                if (rec.get("ts") or 0) < cutoff:
                    continue
                flag_events.append(rec)
    flag_events.sort(key=lambda r: r.get("ts") or 0, reverse=True)

    flags_banner = ""
    if flag_events:
        by_kind: dict[str, int] = {}
        for f in flag_events:
            by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        chips = []
        colors = {
            "bulwark_deny":   "var(--error)",
            "canary_hit":     "#ffcf66",
            "classifier_hit": "#86efac",
        }
        for kind, count in by_kind.items():
            color = colors.get(kind, "var(--primary)")
            chips.append(
                f'<span style="padding:3px 10px;border-radius:10px;'
                f'background:color-mix(in srgb,{color} 18%,transparent);'
                f'color:{color};font-size:11px;font-weight:600;'
                f'text-transform:uppercase;letter-spacing:0.04em;'
                f'font-family:var(--font-mono)">{count}&nbsp;{e(kind)}</span>'
            )
        flags_banner = (
            '<div style="margin:12px 0;padding:10px 14px;'
            'border:1px solid var(--outline-variant);border-radius:10px;'
            'background:color-mix(in srgb,var(--on-surface) 3%,transparent);'
            'display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
            '<span style="color:var(--text-mute);font-size:11px;'
            'text-transform:uppercase;letter-spacing:0.08em">flags · last 1h</span>'
            + "".join(chips) +
            f'<a href="/audit?t={e(token)}" style="margin-left:auto;'
            f'color:var(--primary);font-size:11px;text-decoration:none">audit →</a>'
            '</div>'
        )

    body = (
        '<section class="rhema-workbench">'
        '<div class="ara-eyebrow">Envelope</div>'
        '<h2 class="ara-h">Rhema</h2>'
        '<p class="ara-sub">'
        'Envelope layer — live conversation stream. Rules applied to this '
        'stream live in <a href="/bulwark?t=' + e(token) + '">Bulwark</a> '
        '(ask/reply/oc layers); when one fires, it appears inline here '
        'with a <span class="mono">◆&nbsp;bulwark</span> provenance badge.'
        '</p>'
        + tabs_html
        + flags_banner
        + flash_html +
        '<h3 class="ara-eyebrow" style="margin-top:18px">Live thoughts</h3>'
        + (
            _render_channel_filter_bar(
                current=channel_filter or "",
                base_url="/rhema", token=token,
                available=seen_channels_for_filter,
            )
            if seen_channels_for_filter else ""
        )
        + stream_html +
        '</section>'
        + _RHEMA_TAB_JS
        + _RHEMA_SELECTION_JS
        + _RHEMA_EVENTS_REFRESH_JS
        + _RHEMA_LAZY_DETAIL_JS
    )
    return _page("Rhema", "rhema", body, token)


def render_rhema_turn_detail(
    state_root: Path, session_id: str, line_idx: int | None = None,
    *, offset: int | None = None,
) -> str | None:
    """Server-rendered detail panel for a single transcript turn. Used
    by the /rhema lazy-load endpoint. Returns ``None`` when the session
    or line doesn't exist."""
    sdir = state_root / "sessions" / session_id
    tx = sdir / "transcript.jsonl"
    if not tx.exists():
        return None
    rec = _read_jsonl_at_offset(tx, offset) if offset is not None else None
    if rec is None and line_idx is not None:
        rec = _read_jsonl_line(tx, line_idx)
    if rec is None:
        return None
    return _render_rhema_detail(rec)


_RHEMA_LAZY_DETAIL_JS = """
<script>(function(){
  // Lazy-load deferred turn detail panes on first <details> open.
  // Stubs render with data-fetch-url; we replace their innerHTML with
  // the server-rendered detail HTML once the operator expands the
  // turn. Keeps initial /rhema page weight bounded even when there
  // are hundreds of pairs in the stream.
  document.addEventListener('toggle', async (ev) => {
    const det = ev.target;
    if (!(det instanceof HTMLDetailsElement)) return;
    if (!det.open) return;
    const stub = det.querySelector('.rhema-detail-stub[data-fetch-url]');
    if (!stub) return;
    const url = stub.dataset.fetchUrl;
    delete stub.dataset.fetchUrl;
    try {
      const resp = await fetch(url, { credentials: 'same-origin' });
      if (!resp.ok) {
        stub.innerHTML = '<div class="ara-empty" style="margin:14px 18px">'
          + 'fetch failed: HTTP ' + resp.status + '</div>';
        return;
      }
      stub.outerHTML = await resp.text();
    } catch (err) {
      stub.innerHTML = '<div class="ara-empty" style="margin:14px 18px">'
        + 'fetch failed: ' + err + '</div>';
    }
  }, true);
})();
</script>
"""


_RHEMA_EVENTS_REFRESH_JS = """
<script id="rhema-events-refresh">(function(){
  // Subscribe to /rhema/events — a lightweight SSE stream that emits a
  // running count of transcript lines appended since this page loaded.
  // We don't live-render new pair cards (the pair/match UI is
  // server-rendered and tricky to reconstruct client-side). Instead we
  // surface a single banner that says "N new turns — refresh" and lets
  // the operator pull the updated server render in one click.
  const token = new URLSearchParams(window.location.search).get("t") || "";
  if (!token) return;
  let banner = null;
  function ensureBanner(n){
    if (banner) {
      banner.querySelector("[data-count]").textContent = n;
      return;
    }
    banner = document.createElement("div");
    banner.className = "ara-callout";
    banner.style.cssText =
      "position:sticky;top:0;z-index:20;margin:0 0 10px 0;" +
      "padding:8px 14px;border-left:3px solid var(--tertiary);" +
      "background:color-mix(in srgb,var(--tertiary) 12%,var(--surface));" +
      "display:flex;gap:12px;align-items:center";
    banner.innerHTML =
      '<span><strong data-count>' + n + '</strong> new transcript turn(s) since load</span>' +
      '<a href="javascript:location.reload()" class="ara-btn ara-btn-ghost" ' +
      'style="text-decoration:none;padding:4px 10px;font-size:11px">↻ Refresh</a>';
    const host = document.querySelector("main") || document.body;
    host.insertBefore(banner, host.firstChild);
  }
  const es = new EventSource("/rhema/events?t=" + encodeURIComponent(token));
  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (typeof data.new_since_open === "number" && data.new_since_open > 0) {
        ensureBanner(data.new_since_open);
      }
    } catch (err) { /* ignore malformed frames */ }
  };
  es.onerror = () => { /* silent: browser auto-reconnects SSE */ };
})();
</script>
"""


def _extract_captured_system_prompt(state_root: Path) -> tuple[str, str, str]:
    """Scan recent transcripts for a request with an ``instructions`` field.
    Returns ``(prompt_text, session_id, model)`` or ``('', '', '')`` if
    nothing captured. Newest-first, first hit wins."""
    sessions_root = state_root / "sessions"
    if not sessions_root.exists():
        return ("", "", "")
    for session_dir in sorted(sessions_root.iterdir(), reverse=True):
        tx = session_dir / "transcript.jsonl"
        if not tx.exists():
            continue
        try:
            with tx.open() as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for raw in reversed(lines):  # most recent line first
            try:
                rec = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("role") != "request":
                continue
            body = (rec.get("payload") or {}).get("body")
            if not isinstance(body, dict):
                continue
            instr = body.get("instructions") or body.get("system")
            if isinstance(instr, list):
                # Anthropic shape: list of {type:'text', text:'...'}
                chunks = []
                for b in instr:
                    if isinstance(b, dict):
                        t = b.get("text")
                        if isinstance(t, str):
                            chunks.append(t)
                instr = "\n\n".join(chunks)
            if isinstance(instr, str) and instr.strip():
                return (
                    instr,
                    session_dir.name,
                    str(rec.get("model") or body.get("model") or ""),
                )
    return ("", "", "")


def _split_prompt_sections(text: str) -> list[tuple[str, str]]:
    """Split a large system prompt into (title, body) sections on markdown
    headings. Falls back to a single-section tuple when there are no
    headings."""
    if not text:
        return []
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = "Preamble"
    current_body: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Treat ``## `` or ``# `` or ``### `` as a boundary.
        if stripped.startswith(("# ", "## ", "### ")):
            if current_body and any(l.strip() for l in current_body):
                sections.append((current_title, current_body))
            current_title = stripped.lstrip("# ").strip() or "(untitled)"
            current_body = []
        else:
            current_body.append(line)
    if current_body and any(l.strip() for l in current_body):
        sections.append((current_title, current_body))
    if not sections:
        return [("prompt", text)]
    return [(t, "\n".join(b).strip()) for t, b in sections]


def _render_captured_prompt_block(
    state_root: Path, edit_mode: bool = False, *,
    armor_name: str = "default",
) -> str:
    """Read-only mirror of the most recently captured system prompt, used
    on the /absolute VIEW page so operators can see what openclaw currently
    sends. On the EDIT page we use ``_render_absolute_section_editor``
    instead; that one treats ABSOLUTE.md itself as the editable section
    list (seeded from captured if the file is empty)."""
    prompt, sid, model = _extract_captured_system_prompt(state_root)
    header = (
        '<p class="ara-sub">What the agent\'s runtime actually sends before '
        'your ABSOLUTE — pulled from the most recent intercepted request. '
        'Un-check a chunk to stage it for a <code>shadow_rewrite</code> rule '
        '(preview-only right now).</p>'
    )
    if not prompt:
        return (
            '<details class="abs-captured-drawer">'
            '<summary class="abs-captured-summary">'
            '<span class="ara-eyebrow">Captured baseline</span>'
            '<strong>Default system prompt</strong>'
            '<span class="rh-badge">none captured</span>'
            '</summary>'
            '<div class="abs-captured-body">'
            + header +
            '<div class="ara-empty">'
            'No system prompt captured yet. Once an agent runs a turn through '
            'ARA\'s MITM proxy, the latest one will appear here broken into '
            'checkable chunks.</div>'
            '</div>'
            '</details>'
        )
    sections = _split_prompt_sections(prompt)
    # ``total_chars`` is the sum of the body lengths actually shown in the
    # rows so "check all" hits 100% of the readout. Heading lines and
    # between-section whitespace aren't counted here because they aren't
    # in any individual section's displayed size either.
    total_chars = sum(len(b) for _, b in sections)
    max_chars = max((len(b) for _, b in sections), default=1) or 1

    meta = (
        f'<p class="ara-sub" style="margin-bottom:10px">'
        f'<span class="rh-badge">model: {e(model or "—")}</span> '
        f'<span class="rh-badge">session: {e(sid[:12])}</span> '
        f'<span class="rh-badge">{len(sections)} section{"s" if len(sections) != 1 else ""}</span> '
        f'<span class="rh-badge">{total_chars:,} chars</span>'
        '</p>'
    )

    load_btn = (
        '<button type="button" class="audit-chip audit-chip-on" '
        'id="abs-load-into-editor" title="Overwrite the ABSOLUTE.md editor '
        'below with the checked sections. You can tweak titles/bodies before '
        'saving.">⇩ load checked into editor</button>'
        if edit_mode else ''
    )
    toolbar = (
        '<div class="abs-toolbar">'
        '<input type="search" id="abs-search" placeholder="search sections…" autocomplete="off">'
        '<button type="button" class="audit-chip" id="abs-expand-all">expand all</button>'
        '<button type="button" class="audit-chip" id="abs-collapse-all">collapse all</button>'
        '<button type="button" class="audit-chip" id="abs-uncheck-all">uncheck all</button>'
        '<button type="button" class="audit-chip" id="abs-check-all">check all</button>'
        + load_btn +
        '<div class="abs-readout">'
        '<div class="abs-readout-bar"><div class="abs-readout-fill" '
        'id="abs-readout-fill" style="width:100%"></div></div>'
        '<span id="abs-selected-chars" class="mono">'
        f'{total_chars:,} / {total_chars:,} chars'
        '</span>'
        '</div>'
        '</div>'
    )

    rows: list[str] = []
    for i, (title, body) in enumerate(sections):
        chars = len(body)
        # Width of the inline size bar, relative to the biggest section.
        bar_pct = max(2, int(chars / max_chars * 100))
        rows.append(
            '<details class="abs-row" data-title="'
            + e(title.lower()) +
            f'" data-chars="{chars}">'
            '<summary class="abs-row-head">'
            f'<input type="checkbox" class="abs-cb" checked data-abs-section="{i}" '
            'aria-label="include">'
            f'<span class="abs-row-title">{e(title)}</span>'
            '<span class="abs-row-size-bar" aria-hidden="true">'
            f'<span class="abs-row-size-fill" style="width:{bar_pct}%"></span>'
            '</span>'
            f'<span class="abs-row-chars mono">{chars:,}</span>'
            '<span class="abs-row-chev" aria-hidden="true">▸</span>'
            '</summary>'
            f'<pre class="ara-code abs-row-body">{e(body)}</pre>'
            '</details>'
        )
    list_block = (
        '<div class="abs-rows" id="abs-rows">'
        + "".join(rows)
        + '</div>'
    )

    # Operator-authored sections — client-side persisted in localStorage per
    # armor. Only rendered in edit mode; a "+ Add section" button spawns
    # blank rows with editable title/body and a drag handle.
    operator_block = ""
    if edit_mode:
        operator_block = (
            '<div class="abs-operator-block">'
            '<div class="abs-operator-head">'
            '<h4 class="ara-eyebrow" style="margin:18px 0 6px 0">'
            'Your sections (added locally)</h4>'
            '<button type="button" class="audit-chip audit-chip-on" '
            f'id="abs-add-section" data-armor="{e(armor_name)}">'
            '+ add section</button>'
            '</div>'
            '<p class="ara-sub" style="margin:0 0 8px 0">'
            'Sections you add here are stored in your browser (per armor) '
            'and get composed into the editor below when you hit "load '
            'checked into editor". Drag the ⋮⋮ handle to reorder.</p>'
            f'<div class="abs-rows abs-operator-rows" id="abs-operator-rows" '
            f'data-armor="{e(armor_name)}"></div>'
            '</div>'
        )

    script = (
        '<script>(function(){'
        'const total=' + str(total_chars) + ';'
        'const rows=document.querySelectorAll(".abs-row");'
        'const search=document.getElementById("abs-search");'
        'const readout=document.getElementById("abs-selected-chars");'
        'const fill=document.getElementById("abs-readout-fill");'
        'function recalc(){let s=0;rows.forEach(r=>{'
        'const cb=r.querySelector(".abs-cb");'
        'if(cb&&cb.checked)s+=parseInt(r.dataset.chars||"0",10);});'
        'if(readout)readout.textContent='
        's.toLocaleString()+" / "+total.toLocaleString()+" chars";'
        'if(fill)fill.style.width=(total?Math.max(0,Math.min(100,s*100/total)):0)+"%";'
        '}'
        'rows.forEach(r=>{const cb=r.querySelector(".abs-cb");'
        'if(cb)cb.addEventListener("click",(e)=>e.stopPropagation());'
        'if(cb)cb.addEventListener("change",recalc);});'
        'if(search)search.addEventListener("input",()=>{'
        'const q=search.value.trim().toLowerCase();'
        'rows.forEach(r=>{const t=r.dataset.title||"";'
        'r.style.display=(!q||t.indexOf(q)>-1)?"":"none";});});'
        'const ea=document.getElementById("abs-expand-all");'
        'if(ea)ea.addEventListener("click",()=>rows.forEach(r=>r.open=true));'
        'const ca=document.getElementById("abs-collapse-all");'
        'if(ca)ca.addEventListener("click",()=>rows.forEach(r=>r.open=false));'
        'const uc=document.getElementById("abs-uncheck-all");'
        'if(uc)uc.addEventListener("click",()=>{rows.forEach(r=>{'
        'const cb=r.querySelector(".abs-cb");if(cb)cb.checked=false;});recalc();});'
        'const cc=document.getElementById("abs-check-all");'
        'if(cc)cc.addEventListener("click",()=>{rows.forEach(r=>{'
        'const cb=r.querySelector(".abs-cb");if(cb)cb.checked=true;});recalc();});'
        # Compose checked sections into the ABSOLUTE.md textarea. Each row
        # becomes a ``## Title`` heading followed by its body. Operator
        # can further edit titles/bodies in the textarea before saving.
        # Button lookup here; editor is resolved inside the click handler
        # because the captured block renders before the <form> containing
        # the textarea — so the editor is null at script-parse time.
        # --- Operator-authored sections (localStorage-backed) ------------
        'const opRoot=document.getElementById("abs-operator-rows");'
        'const opKey=opRoot?"abs-ops:"+(opRoot.dataset.armor||"default"):null;'
        'function opLoad(){if(!opKey)return [];try{'
        'return JSON.parse(localStorage.getItem(opKey)||"[]")||[];'
        '}catch(e){return [];}}'
        'function opSave(items){if(opKey)localStorage.setItem(opKey,JSON.stringify(items));}'
        'function opRender(items){if(!opRoot)return;opRoot.innerHTML="";'
        'items.forEach((it,idx)=>{'
        'const row=document.createElement("div");'
        'row.className="abs-row abs-row-operator";'
        'row.draggable=true;row.dataset.opIdx=idx;'
        'row.innerHTML='
        '"<div class=\\"abs-row-head\\">"+'
        '"<span class=\\"abs-drag-handle\\" title=\\"drag to reorder\\">⋮⋮</span>"+'
        '"<input type=\\"checkbox\\" class=\\"abs-cb\\" checked aria-label=\\"include\\">"+'
        '"<input class=\\"abs-op-title\\" placeholder=\\"section title\\" value=\\"\\">"+'
        '"<span class=\\"abs-row-chars mono\\" data-op-chars>0</span>"+'
        '"<button type=\\"button\\" class=\\"abs-op-remove\\" title=\\"remove\\">✕</button>"+'
        '"</div>"+'
        '"<textarea class=\\"abs-op-body\\" placeholder=\\"section body…\\" rows=\\"6\\"></textarea>";'
        'row.querySelector(".abs-op-title").value=it.title||"";'
        'row.querySelector(".abs-op-body").value=it.body||"";'
        'row.querySelector("[data-op-chars]").textContent=(it.body||"").length.toLocaleString();'
        'opRoot.appendChild(row);});'
        'wireOperatorHandlers();recalcAll();}'
        'function wireOperatorHandlers(){if(!opRoot)return;'
        'opRoot.querySelectorAll(".abs-row-operator").forEach(row=>{'
        'const idx=parseInt(row.dataset.opIdx,10);'
        'const title=row.querySelector(".abs-op-title");'
        'const body=row.querySelector(".abs-op-body");'
        'const cb=row.querySelector(".abs-cb");'
        'const rm=row.querySelector(".abs-op-remove");'
        'const chars=row.querySelector("[data-op-chars]");'
        '[title,body].forEach(inp=>inp.addEventListener("input",()=>{'
        'const items=opLoad();if(items[idx]){items[idx].title=title.value;'
        'items[idx].body=body.value;opSave(items);}'
        'if(chars)chars.textContent=body.value.length.toLocaleString();'
        'recalcAll();}));'
        'cb.addEventListener("change",recalcAll);'
        'rm.addEventListener("click",()=>{const items=opLoad();'
        'items.splice(idx,1);opSave(items);opRender(items);});'
        # Drag reorder
        'row.addEventListener("dragstart",(e)=>{e.dataTransfer.setData("text/plain",idx);'
        'row.classList.add("abs-row-dragging");});'
        'row.addEventListener("dragend",()=>row.classList.remove("abs-row-dragging"));'
        'row.addEventListener("dragover",(e)=>{e.preventDefault();'
        'row.classList.add("abs-row-drop-target");});'
        'row.addEventListener("dragleave",()=>row.classList.remove("abs-row-drop-target"));'
        'row.addEventListener("drop",(e)=>{e.preventDefault();'
        'row.classList.remove("abs-row-drop-target");'
        'const from=parseInt(e.dataTransfer.getData("text/plain"),10);'
        'const to=parseInt(row.dataset.opIdx,10);'
        'if(isNaN(from)||isNaN(to)||from===to)return;'
        'const items=opLoad();const moved=items.splice(from,1)[0];'
        'items.splice(to,0,moved);opSave(items);opRender(items);});'
        '});}'
        'const addBtn=document.getElementById("abs-add-section");'
        'if(addBtn)addBtn.addEventListener("click",(e)=>{e.preventDefault();'
        'const items=opLoad();items.push({title:"New section",body:""});opSave(items);'
        'opRender(items);'
        'const last=opRoot.lastElementChild;if(last)last.querySelector(".abs-op-title")?.focus();});'
        'function recalcAll(){let s=0;rows.forEach(r=>{'
        'const cb=r.querySelector(".abs-cb");'
        'if(cb&&cb.checked)s+=parseInt(r.dataset.chars||"0",10);});'
        'let t=total;'
        'opRoot&&opRoot.querySelectorAll(".abs-row-operator").forEach(r=>{'
        'const cb=r.querySelector(".abs-cb");'
        'const body=r.querySelector(".abs-op-body");'
        'const n=(body?body.value.length:0);t+=n;'
        'if(cb&&cb.checked)s+=n;});'
        'if(readout)readout.textContent='
        's.toLocaleString()+" / "+t.toLocaleString()+" chars";'
        'if(fill)fill.style.width=(t?Math.max(0,Math.min(100,s*100/t)):0)+"%";'
        '}'
        'if(opRoot)opRender(opLoad());'
        # --- Load-checked button: include operator sections too ---------
        'const loadBtn=document.getElementById("abs-load-into-editor");'
        'if(loadBtn)loadBtn.addEventListener("click",(e)=>{'
        'e.preventDefault();'
        'const editor=document.querySelector(\'textarea[name="contents"]\');'
        'if(!editor){loadBtn.textContent="⚠ textarea not found";'
        'setTimeout(()=>loadBtn.textContent="⇩ load checked into editor",1800);return;}'
        'const out=[];rows.forEach(r=>{'
        'const cb=r.querySelector(".abs-cb");if(!(cb&&cb.checked))return;'
        'const title=r.querySelector(".abs-row-title")?.textContent.trim()||"";'
        'const body=r.querySelector(".abs-row-body")?.textContent||"";'
        'out.push("## "+title+"\\n\\n"+body.trim());});'
        'if(opRoot)opRoot.querySelectorAll(".abs-row-operator").forEach(r=>{'
        'const cb=r.querySelector(".abs-cb");if(!(cb&&cb.checked))return;'
        'const t=r.querySelector(".abs-op-title")?.value.trim()||"";'
        'const b=r.querySelector(".abs-op-body")?.value||"";'
        'if(t||b.trim())out.push("## "+t+"\\n\\n"+b.trim());});'
        'editor.value=out.join("\\n\\n");'
        'editor.focus();editor.scrollIntoView({behavior:"smooth",block:"center"});'
        'loadBtn.textContent="✓ loaded "+out.length+" section"+(out.length===1?"":"s");'
        'setTimeout(()=>loadBtn.textContent="⇩ load checked into editor",1800);'
        '});'
        'recalc();recalcAll();'
        '})();</script>'
    )
    footer = (
        '<p class="ara-sub" style="margin-top:10px">'
        'Enforcement wires up via <code>/shadows</code> — pair this selection '
        'with a <code>shadow_rewrite</code> rule to A/B the stripped prompt '
        'before making it real.</p>'
    )
    return (
        '<details class="abs-captured-drawer">'
        '<summary class="abs-captured-summary">'
        '<span class="ara-eyebrow">Captured baseline</span>'
        '<strong>Default system prompt</strong>'
        f'<span class="rh-badge">{len(sections)} sections</span>'
        f'<span class="rh-badge">{total_chars:,} chars</span>'
        '</summary>'
        '<div class="abs-captured-body">'
        + header + meta + toolbar + list_block + operator_block + footer + script +
        '</div>'
        '</details>'
    )


def _render_absolute_section_editor(
    state_root: Path, armor_name: str,
    current_absolute_md: str,
) -> str:
    """Section-based editor for ABSOLUTE.md.

    ABSOLUTE.md is *the* prompt — editing these sections (title/body,
    reorder, add, remove) rebuilds the composed markdown live into the
    textarea on the right. If ABSOLUTE.md is empty, a "Seed from captured
    prompt" button populates it with the 64 sections openclaw is
    currently sending so the operator can start subtracting / rewriting
    from the real baseline.
    """
    # Parse whatever is in ABSOLUTE.md into (title, body) pairs.
    initial_sections = _split_prompt_sections(current_absolute_md)
    # Also pull the captured sections for the seed button; may be empty.
    captured, _sid, _model = _extract_captured_system_prompt(state_root)
    captured_sections = _split_prompt_sections(captured) if captured else []
    captured_json = json.dumps([
        {"title": t, "body": b} for t, b in captured_sections
    ])
    initial_json = json.dumps([
        {"title": t, "body": b} for t, b in initial_sections
    ])

    # Prominent reseed banner — distinct from the inline chip toolbar so it
    # reads as a first-class action, not a utility. Three states:
    #   - file empty                → "you need to seed first"
    #   - file non-empty + captured → "replace with captured" (stale warn)
    #   - captured unavailable      → "no turn yet" empty state
    if not captured_sections:
        seed_banner = (
            '<div class="abs-seed-banner abs-seed-banner-none">'
            '<div class="abs-seed-icon">∅</div>'
            '<div class="abs-seed-body">'
            '<strong>No captured prompt yet.</strong>'
            '<p>Run an agent turn first (openclaw or hermes) so ARA\'s '
            'MITM intercepts the current system prompt — then come back '
            'and seed.</p>'
            '</div>'
            '</div>'
        )
    elif not initial_sections:
        seed_banner = (
            '<div class="abs-seed-banner abs-seed-banner-empty" '
            'data-captured="' + str(len(captured_sections)) + '">'
            '<div class="abs-seed-icon">⚠</div>'
            '<div class="abs-seed-body">'
            '<strong>Start here →</strong> ABSOLUTE.md is empty. '
            'Seed it from the captured agent prompt so your first edits '
            'apply on top of the real baseline instead of a blank file.'
            '<p class="ara-sub">Hit this button once. After that, every '
            'add / remove / reorder is relative to what the agent '
            '(openclaw or hermes) actually sends.</p>'
            '</div>'
            '<button type="button" class="ara-btn ara-btn-primary" '
            'id="abs-seed-captured">⇩ Seed from captured '
            f'({len(captured_sections)} sections)</button>'
            '</div>'
        )
    else:
        seed_banner = (
            '<div class="abs-seed-banner abs-seed-banner-ok" '
            'data-captured="' + str(len(captured_sections)) + '">'
            '<div class="abs-seed-icon">↺</div>'
            '<div class="abs-seed-body">'
            '<strong>Reseed from captured.</strong>'
            '<p class="ara-sub">Discards your current sections and '
            'repopulates from whatever the agent is sending right now '
            '(openclaw or hermes). Use if the agent updated its system '
            'prompt and you want your edits to rebase onto the new '
            'baseline — otherwise keep editing what\'s already here.</p>'
            '</div>'
            '<button type="button" class="ara-btn ara-btn-ghost" '
            'id="abs-seed-captured-top">↺ Reseed</button>'
            '</div>'
        )

    toolbar = (
        '<div class="abs-toolbar">'
        '<input type="search" id="abs-search" placeholder="search sections…" autocomplete="off">'
        '<button type="button" class="audit-chip audit-chip-on" id="abs-add-section">+ add section</button>'
        '<button type="button" class="audit-chip" id="abs-expand-all">expand all</button>'
        '<button type="button" class="audit-chip" id="abs-collapse-all">collapse all</button>'
        '<div class="abs-readout">'
        '<div class="abs-readout-bar"><div class="abs-readout-fill" id="abs-readout-fill"></div></div>'
        '<span id="abs-selected-chars" class="mono">0 chars</span>'
        '</div>'
        '</div>'
    )

    # Section rows are rendered entirely by JS (so add/remove/reorder stay
    # in one place). Server provides the initial JSON blob.
    other_armor_names = [
        a.name for a in armors_mod.list_armors(state_root)
        if a.name != armor_name
    ]
    other_armors_json = json.dumps(other_armor_names)
    rows_host = (
        '<div class="abs-rows abs-editor-rows" id="abs-editor-rows" '
        f'data-armor="{e(armor_name)}" '
        f'data-initial="{e(initial_json)}" '
        f'data-captured="{e(captured_json)}" '
        f'data-other-armors="{e(other_armors_json)}"></div>'
    )

    script = """
<script>(function(){
  const host = document.getElementById("abs-editor-rows");
  if (!host) return;
  const readout = document.getElementById("abs-selected-chars");
  const fill = document.getElementById("abs-readout-fill");
  const search = document.getElementById("abs-search");

  let state = [];
  try {
    const raw = JSON.parse(host.dataset.initial || "[]") || [];
    // Infer origin: everything loaded from file starts as "captured" since
    // the only way content is there is either a previous save of captured
    // content or operator edits we can't distinguish retroactively. New
    // "+ add" rows explicitly mark origin="operator" below.
    state = raw.map(s => Object.assign({ origin: "captured" }, s));
  } catch (e) {}
  const captured = (() => {
    try { return JSON.parse(host.dataset.captured || "[]") || []; } catch (e) { return []; }
  })();
  const otherArmors = (() => {
    try { return JSON.parse(host.dataset.otherArmors || "[]") || []; } catch (e) { return []; }
  })();
  // Extract the Armory token from the URL so the section-copy fetch can
  // authenticate the same way the rest of the page does.
  const armoryToken = new URLSearchParams(window.location.search).get("t") || "";

  function composed() {
    return state.map(s => "## " + (s.title || "") + "\\n\\n" + (s.body || "").trim()).join("\\n\\n");
  }
  function syncTextarea() {
    const ta = document.querySelector('textarea[name="contents"]');
    if (ta) ta.value = composed();
  }
  function recalc() {
    let total = 0;
    state.forEach(s => total += (s.body || "").length);
    if (readout) readout.textContent = total.toLocaleString() + " chars · " + state.length + " section" + (state.length === 1 ? "" : "s");
    if (fill) fill.style.width = state.length ? "100%" : "0%";
  }
  // Click-to-locate: when a section opens, select its "## Title" block in
  // the composed textarea so the operator sees exactly where it lives.
  function highlightInTextarea(title) {
    const ta = document.querySelector('textarea[name="contents"]');
    if (!ta || !title) return;
    const needle = "## " + title;
    const text = ta.value;
    const pos = text.indexOf(needle);
    if (pos < 0) return;
    // Select the whole section body up to the next "## " or end.
    let end = text.indexOf("\\n## ", pos + needle.length);
    if (end < 0) end = text.length;
    ta.focus({ preventScroll: true });
    ta.setSelectionRange(pos, end);
    // Manual scroll: setSelectionRange alone isn't reliable across browsers.
    const before = text.slice(0, pos);
    const lineNo = (before.match(/\\n/g) || []).length;
    const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 18;
    ta.scrollTop = Math.max(0, lineNo * lineHeight - ta.clientHeight / 3);
  }
  function render() {
    host.innerHTML = "";
    state.forEach((sec, idx) => {
      const row = document.createElement("details");
      const origin = sec.origin || "captured";
      row.className = "abs-row abs-row-editor";
      row.draggable = true;
      row.dataset.idx = idx;
      row.dataset.origin = origin;
      row.dataset.title = (sec.title || "").toLowerCase();
      const badge = origin === "operator"
        ? '<span class="abs-origin-badge abs-origin-operator" title="Operator-added section">+ added</span>'
        : '<span class="abs-origin-badge abs-origin-captured" title="From captured agent prompt (openclaw or hermes)">captured</span>';
      row.innerHTML =
        '<summary class="abs-row-head">' +
          '<span class="abs-drag-handle" title="drag to reorder">⋮⋮</span>' +
          '<input class="abs-op-title" placeholder="section title" value="">' +
          badge +
          '<span class="abs-row-chars mono" data-chars>0</span>' +
          (otherArmors.length
            ? '<select class="abs-op-copy-to" title="copy this section to another armor">' +
                '<option value="">Copy to…</option>' +
                otherArmors.map(n => '<option value="' + n.replace(/"/g, "&quot;") + '">' + n + '</option>').join("") +
              '</select>'
            : '') +
          '<button type="button" class="abs-op-remove" title="remove">✕</button>' +
          '<span class="abs-row-chev" aria-hidden="true">▸</span>' +
        '</summary>' +
        '<textarea class="abs-op-body" placeholder="section body…" rows="8"></textarea>';
      const titleEl = row.querySelector(".abs-op-title");
      const bodyEl = row.querySelector(".abs-op-body");
      const charsEl = row.querySelector("[data-chars]");
      titleEl.value = sec.title || "";
      bodyEl.value = sec.body || "";
      charsEl.textContent = (sec.body || "").length.toLocaleString();
      host.appendChild(row);
    });
    wireHandlers();
    recalc();
    syncTextarea();
  }
  function wireHandlers() {
    host.querySelectorAll(".abs-row-editor").forEach(row => {
      const idx = parseInt(row.dataset.idx, 10);
      const titleEl = row.querySelector(".abs-op-title");
      const bodyEl = row.querySelector(".abs-op-body");
      const rmBtn = row.querySelector(".abs-op-remove");
      const charsEl = row.querySelector("[data-chars]");
      [titleEl, bodyEl].forEach(inp => {
        inp.addEventListener("input", () => {
          state[idx].title = titleEl.value;
          state[idx].body = bodyEl.value;
          charsEl.textContent = bodyEl.value.length.toLocaleString();
          row.dataset.title = titleEl.value.toLowerCase();
          recalc();
          syncTextarea();
        });
        inp.addEventListener("click", e => e.stopPropagation());
      });
      rmBtn.addEventListener("click", e => {
        e.preventDefault(); e.stopPropagation();
        state.splice(idx, 1);
        render();
      });
      const copySel = row.querySelector(".abs-op-copy-to");
      if (copySel) {
        copySel.addEventListener("click", e => e.stopPropagation());
        copySel.addEventListener("change", async e => {
          e.preventDefault(); e.stopPropagation();
          const target = copySel.value;
          if (!target) return;
          const sec = state[idx];
          const fd = new FormData();
          fd.append("armor", target);
          fd.append("title", sec.title || "");
          fd.append("body", sec.body || "");
          try {
            const resp = await fetch(
              "/absolute/append-section?t=" + encodeURIComponent(armoryToken),
              { method: "POST", body: fd, credentials: "same-origin" },
            );
            if (resp.ok) {
              copySel.style.background =
                "color-mix(in srgb,var(--tertiary) 35%,transparent)";
              setTimeout(() => { copySel.style.background = ""; }, 1400);
            } else {
              alert("Copy failed: HTTP " + resp.status);
            }
          } catch (err) {
            alert("Copy failed: " + err);
          }
          copySel.value = "";
        });
      }
      // Opening a section → locate + highlight it in the composed textarea.
      row.addEventListener("toggle", () => {
        if (!row.open) return;
        highlightInTextarea(state[idx].title);
      });
      row.addEventListener("dragstart", e => {
        e.dataTransfer.setData("text/plain", String(idx));
        row.classList.add("abs-row-dragging");
      });
      row.addEventListener("dragend", () => row.classList.remove("abs-row-dragging"));
      row.addEventListener("dragover", e => { e.preventDefault(); row.classList.add("abs-row-drop-target"); });
      row.addEventListener("dragleave", () => row.classList.remove("abs-row-drop-target"));
      row.addEventListener("drop", e => {
        e.preventDefault();
        row.classList.remove("abs-row-drop-target");
        const from = parseInt(e.dataTransfer.getData("text/plain"), 10);
        const to = parseInt(row.dataset.idx, 10);
        if (isNaN(from) || isNaN(to) || from === to) return;
        const moved = state.splice(from, 1)[0];
        state.splice(to, 0, moved);
        render();
      });
    });
  }
  const addBtn = document.getElementById("abs-add-section");
  if (addBtn) addBtn.addEventListener("click", e => {
    e.preventDefault();
    state.push({ title: "New section", body: "", origin: "operator" });
    render();
    const last = host.lastElementChild;
    if (last) {
      last.open = true;
      last.querySelector(".abs-op-title")?.focus();
      last.querySelector(".abs-op-title")?.select();
    }
  });
  const seedBtns = ["abs-seed-captured", "abs-seed-captured-top"]
    .map(id => document.getElementById(id)).filter(Boolean);
  seedBtns.forEach(btn => btn.addEventListener("click", e => {
    e.preventDefault();
    if (state.length && !confirm(
      "Replace all " + state.length + " current section(s) with the " +
      captured.length + " captured section(s)? Your operator-added " +
      "sections will be lost."
    )) return;
    state = captured.map(s => ({ title: s.title, body: s.body, origin: "captured" }));
    render();
  }));
  const expandAll = document.getElementById("abs-expand-all");
  if (expandAll) expandAll.addEventListener("click",
    () => host.querySelectorAll("details").forEach(d => d.open = true));
  const collapseAll = document.getElementById("abs-collapse-all");
  if (collapseAll) collapseAll.addEventListener("click",
    () => host.querySelectorAll("details").forEach(d => d.open = false));
  if (search) search.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    host.querySelectorAll(".abs-row-editor").forEach(r => {
      const t = r.dataset.title || "";
      r.style.display = (!q || t.indexOf(q) > -1) ? "" : "none";
    });
  });
  render();
})();</script>
"""
    return seed_banner + toolbar + rows_host + script


def _render_envelope_readable_output(contents: str) -> str:
    sections = _split_prompt_sections(contents)
    if not sections:
        return '<div class="abs-readable-output ara-empty">No composed envelope content yet.</div>'
    cards: list[str] = []
    for idx, (title, body) in enumerate(sections[:12], start=1):
        preview = body[:900]
        omitted = len(body) - len(preview)
        cards.append(
            '<article class="abs-readable-section">'
            '<div class="abs-readable-section-head">'
            f'<span class="ara-eyebrow">{idx:02d}</span>'
            f'<strong>{e(title)}</strong>'
            f'<span class="rh-badge">{len(body):,} chars</span>'
            '</div>'
            f'<div class="shadow-readable-block">{e(preview)}</div>'
            + (
                f'<p class="ara-sub">Preview truncated; {omitted:,} more chars in this section.</p>'
                if omitted > 0 else ''
            )
            + '</article>'
        )
    if len(sections) > 12:
        cards.append(
            '<div class="ara-empty">'
            f'{len(sections) - 12:,} more section(s) are in the raw editor below.'
            '</div>'
        )
    return '<div class="abs-readable-output">' + "".join(cards) + '</div>'


def _render_envelope_body(
    state_root: Path, token: str, *,
    edit: bool = False, flash: str | None = None, flash_level: str = "info",
    armor_override: str | None = None,
    absolute_override: str | None = None,
) -> str:
    """Envelope manifest editor (formerly ABSOLUTE). Returns a body
    fragment — callers wrap with ``_page`` or embed inside another view.

    Each armor carries one or more envelope files under

    Each armor carries one or more absolute files under
    ``<armor>/absolutes/*.md``; only one is active at any time (tracked
    by ``<armor>/absolutes/active``). Operators can:
    - view any armor's absolutes (armor picker)
    - pick a specific absolute within the armor to edit (absolute picker)
    - "activate" a non-active absolute → becomes the one injected next turn
    - save / create new / rename / delete individual absolutes
    """
    active_name = armors_mod.active_armor_name(state_root)
    viewing_name = armor_override or active_name
    armor = armors_mod.load_armor(state_root, viewing_name) if viewing_name else None
    is_viewing_equipped = bool(
        viewing_name and active_name and viewing_name == active_name
    )
    # Figure out which specific absolute file inside the armor we're viewing.
    abs_names = armors_mod.list_absolutes(armor) if armor else []
    active_abs = armors_mod.active_absolute_name(armor) if armor else None
    viewing_abs = absolute_override or active_abs or (abs_names[0] if abs_names else None)
    target = (
        armors_mod.path_for_absolute(armor, viewing_abs)
        if armor and viewing_abs else None
    )
    is_viewing_active_abs = bool(
        viewing_abs and active_abs and viewing_abs == active_abs
    )
    contents = ""
    if target and target.exists():
        contents = target.read_text()

    # Armor picker — every armor's ABSOLUTE is one click away. Clicking a
    # tab VIEWS that armor's ABSOLUTE; a separate "equip" action swaps the
    # active armor. Tabs marked (equipped) and (viewing) at a glance.
    all_armors = armors_mod.list_armors(state_root)
    armor_tabs_html = ""
    if all_armors:
        chips = []
        for a in all_armors:
            is_equipped = a.name == active_name
            is_viewing = a.name == viewing_name
            classes = ["abs-armor-tab"]
            if is_viewing:
                classes.append("abs-armor-tab-viewing")
            if is_equipped:
                classes.append("abs-armor-tab-equipped")
            mode_q = "&edit=1" if edit else ""
            badges = ""
            if is_equipped:
                badges += '<span class="abs-armor-tab-tag abs-armor-tab-tag-eq">equipped</span>'
            if is_viewing and not is_equipped:
                badges += '<span class="abs-armor-tab-tag abs-armor-tab-tag-view">viewing</span>'
            chips.append(
                f'<a class="{" ".join(classes)}" '
                f'href="/absolute?t={e(token)}&armor={e(a.name)}{mode_q}">'
                f'<span class="abs-armor-tab-name">{e(a.name)}</span>{badges}</a>'
            )
        equip_action = ""
        if viewing_name and not is_viewing_equipped:
            equip_action = (
                f'<form method="post" action="/armors/{e(viewing_name)}/equip?t={e(token)}" '
                'class="abs-armor-equip-form">'
                '<button class="ara-btn ara-btn-primary" type="submit">'
                f'⚡ Equip {e(viewing_name)}</button>'
                '</form>'
            )
        armor_tabs_html = (
            '<div class="abs-armor-picker">'
            '<div class="abs-armor-tabs">' + "".join(chips) + '</div>'
            + equip_action +
            '</div>'
        )

    # Absolute-file picker within the viewed armor. Each armor can carry
    # multiple absolutes (drafts, variants); one is active at a time.
    absolutes_picker_html = ""
    if armor and abs_names:
        edit_q = "&edit=1" if edit else ""
        abs_chips: list[str] = []
        for name in abs_names:
            is_active_abs = name == active_abs
            is_viewing_abs = name == viewing_abs
            classes = ["abs-file-tab"]
            if is_viewing_abs:
                classes.append("abs-file-tab-viewing")
            if is_active_abs:
                classes.append("abs-file-tab-active")
            badges = ""
            if is_active_abs:
                badges += '<span class="abs-armor-tab-tag abs-armor-tab-tag-eq">active</span>'
            elif is_viewing_abs:
                badges += '<span class="abs-armor-tab-tag abs-armor-tab-tag-view">viewing</span>'
            abs_chips.append(
                f'<a class="{" ".join(classes)}" '
                f'href="/absolute?t={e(token)}&armor={e(viewing_name or "")}'
                f'&absolute={e(name)}{edit_q}">'
                f'<span class="abs-file-tab-name">{e(name)}</span>{badges}</a>'
            )
        activate_form = ""
        if viewing_abs and not is_viewing_active_abs:
            activate_form = (
                f'<form method="post" '
                f'action="/armors/{e(viewing_name or "")}/absolutes/{e(viewing_abs)}/activate?t={e(token)}" '
                'class="abs-armor-equip-form">'
                '<button class="ara-btn ara-btn-primary" type="submit">'
                f'⚡ Activate {e(viewing_abs)}</button>'
                '</form>'
            )
        # Quick actions: new, rename, delete for the current absolute.
        actions_form = (
            '<div class="abs-file-actions">'
            f'<form method="post" action="/armors/{e(viewing_name or "")}/absolutes/new?t={e(token)}" '
            'style="display:inline-flex;gap:4px;align-items:center">'
            '<input type="text" name="name" placeholder="new-name" required '
            'pattern="[a-zA-Z0-9._-]+" style="font-size:11px;width:14ch;padding:3px 8px">'
            '<button class="ara-btn ara-btn-ghost" type="submit" '
            'style="font-size:11px;padding:3px 8px">+ new</button>'
            '</form>'
            + (
                f'<form method="post" '
                f'action="/armors/{e(viewing_name or "")}/absolutes/{e(viewing_abs)}/rename?t={e(token)}" '
                'style="display:inline-flex;gap:4px;align-items:center">'
                '<input type="text" name="new_name" placeholder="rename to" required '
                'pattern="[a-zA-Z0-9._-]+" style="font-size:11px;width:14ch;padding:3px 8px">'
                '<button class="ara-btn ara-btn-ghost" type="submit" '
                'style="font-size:11px;padding:3px 8px">rename</button>'
                '</form>'
                + (
                    f'<form method="post" '
                    f'action="/armors/{e(viewing_name or "")}/absolutes/{e(viewing_abs)}/duplicate?t={e(token)}" '
                    'style="display:inline-flex;gap:4px;align-items:center">'
                    '<input type="text" name="new_name" placeholder="clone as" required '
                    'pattern="[a-zA-Z0-9._-]+" style="font-size:11px;width:14ch;padding:3px 8px">'
                    '<button class="ara-btn ara-btn-ghost" type="submit" '
                    'style="font-size:11px;padding:3px 8px">⧉ duplicate</button>'
                    '</form>'
                    + (
                        f'<form method="post" '
                        f'action="/armors/{e(viewing_name or "")}/absolutes/{e(viewing_abs)}/delete?t={e(token)}" '
                        'onsubmit="return confirm(\'Delete absolute ' + e(viewing_abs) + '?\')" '
                        'style="display:inline">'
                        '<button class="ara-btn ara-btn-bad" type="submit" '
                        'style="font-size:11px;padding:3px 8px">× delete</button>'
                        '</form>'
                        if len(abs_names) > 1 else ''
                    )
                )
                if viewing_abs else ''
            )
            + '</div>'
        )
        absolutes_picker_html = (
            '<div class="abs-file-picker">'
            '<div class="ara-eyebrow" style="margin:0 0 6px 0">'
            'Absolutes for <span class="mono">' + e(viewing_name or "") + '</span>'
            '</div>'
            '<div class="abs-file-tabs">' + "".join(abs_chips) + '</div>'
            + activate_form +
            actions_form +
            '</div>'
        )

    armor_status = (
        f'equipped: {e(active_name)}' if active_name else '(no armor equipped)'
    )
    if viewing_name and viewing_name != active_name:
        armor_status += f' · viewing: {e(viewing_name)} (not equipped)'

    section_count = len(_split_prompt_sections(contents))
    source_path = e(target) if target else "(none)"
    active_label = e(active_abs or "default")
    viewing_label = e(viewing_abs or "default")
    body = [
        '<section>',
        '<div class="abs-forge-hero">',
        '<div>',
        '<div class="ara-eyebrow">Envelope</div>',
        '<h2 class="ara-h">ABSOLUTE</h2>',
        '<p class="ara-sub">'
        'ABSOLUTE.md is the prompt-side envelope — its contents are injected '
        'into the agent\'s context before every turn, ahead of the user message '
        'and any prior reply. Use it for instructions that must hold across all '
        'sessions: the agent\'s role, hard constraints, and the fact that it\'s '
        'running under ARA supervision.'
        '</p>',
        '</div>',
        '<div class="abs-forge-runes" aria-label="Envelope status">',
        f'<span><strong>{section_count}</strong> sections</span>',
        f'<span><strong>{active_label}</strong> active file</span>',
        f'<span><strong>{e(active_name or "none")}</strong> equipped</span>',
        '</div>',
        '</div>',
    ]
    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        body.append(
            '<div class="ara-empty" '
            f'style="border-left:3px solid var({cls});margin-bottom:16px">{e(flash)}</div>'
        )
    source_drawer = (
        '<details class="abs-source-drawer">'
        '<summary class="abs-source-summary">'
        '<span class="ara-eyebrow">Source & armor</span>'
        '<strong>Armor, file, and provenance</strong>'
        f'<span class="rh-badge">armor: {e(viewing_name or "none")}</span>'
        f'<span class="rh-badge">file: {viewing_label}</span>'
        f'<span class="rh-badge">{section_count} sections</span>'
        '</summary>'
        '<div class="abs-source-body">'
        + (armor_tabs_html or '<div class="ara-empty">No armor sets found.</div>')
        + (absolutes_picker_html or '')
        + '<div class="abs-forge-source-card">'
        '<span class="ara-eyebrow">Current path</span>'
        f'<code>{source_path}</code>'
        f'<p>{armor_status}</p>'
        f'<p>Viewing file <strong>{viewing_label}</strong>; active injected file is '
        f'<strong>{active_label}</strong>.</p>'
        '</div>'
        '</div>'
        '</details>'
    )
    if not target:
        body.append(
            source_drawer +
            '<div class="abs-forge-shell">'
            '<div class="abs-forge-panel abs-forge-sections">'
            '<div class="ara-empty" style="margin-top:16px">'
            'No armor equipped. Equip one and the envelope manifest will appear here.</div>'
            '</div></div>'
        )
        body.append('</section>')
        return "".join(body)

    # Default surface: section editor always visible at the top. The
    # raw-markdown textarea used to live on the right; it's now tucked
    # into a collapsed <details> below the editor, since almost all
    # day-to-day edits happen on sections, not the raw source.
    if viewing_name and viewing_abs:
        save_action = (
            f"/armors/{e(viewing_name)}/absolutes/"
            f"{e(viewing_abs)}/save?t={e(token)}"
        )
    else:
        save_action = f"/absolute/save?t={e(token)}"
    body.extend([
        source_drawer,
        '<div class="abs-forge-shell">',
        f'<form method="post" action="{save_action}" '
        'class="abs-edit-form abs-forge-edit-form">',
        '<section class="abs-forge-panel abs-forge-sections">',
        '<div class="abs-forge-panel-head">',
        '<span class="ara-eyebrow">Section forge</span>',
        '<strong>Shape the envelope one plate at a time</strong>',
        '</div>',
        '<p class="ara-sub">'
        'Edit titles and bodies, reorder with the '
        '<span class="mono">⋮⋮</span> handles, add or remove as needed. '
        'If empty, seed from what the agent currently sends and subtract '
        'from there.</p>',
        _render_absolute_section_editor(
            state_root, viewing_name or "default", contents,
        ),
        '</section>',
        '<section class="abs-forge-panel abs-forge-output">',
        '<div class="abs-forge-panel-head">',
        '<span class="ara-eyebrow">Composed envelope</span>',
        '<strong>Final ABSOLUTE.md payload</strong>',
        '</div>',
        '<p class="ara-sub">'
        'Readable section preview for the envelope that will be injected. '
        'Use the raw editor drawer only when exact markdown surgery is needed.</p>',
        _render_envelope_readable_output(contents),
        '<details class="abs-raw-editor-drawer">',
        '<summary>Raw ABSOLUTE.md editor</summary>',
        '<p class="ara-sub">Exact source written on Save. Section edits rebuild '
        'this field live; direct edits here remain the source of truth until '
        'the next section change.</p>',
        '<textarea name="contents" spellcheck="false" '
        'class="abs-composed-textarea">'
        f'{e(contents)}</textarea>',
        '</details>',
        '<div class="abs-edit-actions abs-forge-actions">',
        '<button class="ara-btn ara-btn-primary" type="submit">'
        f'Save to {e(viewing_name or "default")}</button>',
        f'<a class="ara-btn ara-btn-ghost" href="/rhema?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        'Back to Rhema</a>',
        f'<a class="ara-btn ara-btn-ghost" href="/rhema?t={e(token)}&tab=shadows" '
        'style="text-decoration:none;display:inline-flex;align-items:center" '
        'title="A/B preview of what the envelope produces on each turn.">'
        'Shadows →</a>',
        '</div>',
        '<p class="ara-sub" style="margin-top:10px">'
        f'<strong>Save</strong> writes to armor <code>{e(viewing_name or "default")}</code>'
        + ('' if is_viewing_equipped else ' (not the equipped one — equip it above to activate).')
        + ' The MITM reads the equipped armor\'s active envelope on every '
        'turn, so edits take effect immediately without a sync step.</p>',
        '</section>',
        '</form>',
        '</div>',
        _render_captured_prompt_block(state_root),
        '</section>',
        ])
    return "".join(body)


def render_absolute(
    state_root: Path, token: str, *,
    edit: bool = False, flash: str | None = None, flash_level: str = "info",
    armor_override: str | None = None,
    absolute_override: str | None = None,
) -> str:
    """Legacy page wrapper — retained for callers that still link to the
    standalone envelope editor. The primary surface is the Envelope tab on
    /rhema; this wrapper emits the same body under the Rhema nav key so
    the sidebar highlights correctly."""
    body = _render_envelope_body(
        state_root, token, edit=edit, flash=flash, flash_level=flash_level,
        armor_override=armor_override, absolute_override=absolute_override,
    )
    return _page("Rhema · Envelope", "rhema", body, token)


def render_explain_result(
    state_root: Path, op: str, target: str, token: str,
) -> str:
    """Run the policy evaluator against (op, target) and return an HTML block."""
    from .. import armors as _armors
    from ..policy import evaluate
    from ..walls import parse_file as _parse_file

    wp = _armors.active_walls_file(state_root)
    if wp is None or not wp.exists():
        return (
            '<div class="ara-empty" style="margin-top:12px">'
            'No equipped armor — equip one first.</div>'
        )
    try:
        walls = _parse_file(wp)
    except Exception as exc:
        return (
            '<div class="ara-empty" style="margin-top:12px;'
            'border-left:3px solid var(--error)">'
            f'Parse error: {e(str(exc))}</div>'
        )
    store = overrides_mod.load(state_root)
    active_name = _armors.active_armor_name(state_root)
    if active_name:
        armor = _armors.load_armor(state_root, active_name)
        if armor is not None:
            store = store.merge(overrides_mod.load_at(armor.overrides_path))

    query: dict = {"op": op}
    if op.startswith("fs."):
        query["path"] = target
    elif op == "exec.spawn":
        query["command"] = target
        query["args"] = []
    elif op == "net.connect":
        if ":" in target and not target.startswith("/"):
            h, _, p = target.rpartition(":")
            query["host"] = h
            try: query["port"] = int(p)
            except ValueError: query["port"] = p
        else:
            query["host"] = target
            query["port"] = 80
    d = evaluate(walls, query, override_store=store)
    color = (
        "var(--error)" if d.verdict == "deny"
        else "#d4a53a" if d.verdict == "approval"
        else "var(--tertiary)"
    )
    rule_link = ""
    if d.rule_id:
        import urllib.parse as _up
        rule_link = (
            f' · <a class="mono" style="color:inherit" '
            f'href="/rules/{_up.quote(d.rule_id, safe="")}?t={e(token)}">'
            f'{e(d.rule_id)}</a>'
        )
    return (
        '<div class="ara-empty" '
        f'style="margin-top:12px;border-left:3px solid {color}">'
        f'<div style="font-family:var(--font-mono);font-size:14px;color:{color}">'
        f'{e(d.verdict.upper())}</div>'
        f'<div class="ara-sub" style="margin-top:4px">'
        f'reason: {e(d.reason or "(none)")}{rule_link}'
        + (f' · touched: <span class="mono">{e(d.touched)}</span>'
           if d.touched else '')
        + '</div></div>'
    )


# ---------- overrides / blackholes ----------

def _render_entry_page(
    kind_name: str,           # "overrides" | "blackholes"
    state_root: Path,
    token: str,
) -> str:
    # Effective view (audit #5): merge global + per-armor entries from the
    # currently equipped armor into a single table, with an "Applies now?"
    # column that marks expired entries and armor entries whose armor is
    # not equipped as inactive.
    global_store = overrides_mod.load(state_root)
    active_name = armors_mod.active_armor_name(state_root)
    armor_store = None
    if active_name:
        armor = armors_mod.load_armor(state_root, active_name)
        if armor is not None:
            armor_store = overrides_mod.load_at(armor.overrides_path)

    def _pick(store) -> list:
        if store is None:
            return []
        return store.overrides if kind_name == "overrides" else store.blackholes

    now = time.time()
    merged: list[tuple] = [
        (ent, "global") for ent in _pick(global_store)
    ]
    if armor_store is not None and active_name:
        merged.extend((ent, f"armor:{active_name}") for ent in _pick(armor_store))

    pulse_map = _rule_pulse(_load_policy_hits(state_root))

    def _applies(ent, origin: str) -> tuple[bool, str]:
        if ent.is_expired(now):
            return False, "expired"
        if origin.startswith("armor:"):
            if not active_name:
                return False, "armor not equipped"
            # If the origin armor differs from active, it wouldn't be in
            # merged at all — equipped-only is what we collect.
        if ent.scope and ent.scope.startswith("session:"):
            return False, f"session-scoped ({ent.scope})"
        return True, ""

    rows: list[str] = []
    for ent, origin in merged:
        applies, why = _applies(ent, origin)
        applies_cell = (
            '<span class="rh-badge" '
            'style="background:color-mix(in srgb,var(--tertiary) 25%,transparent)">'
            'YES</span>'
            if applies else
            '<span class="rh-badge" '
            'style="background:color-mix(in srgb,var(--error) 18%,transparent)">'
            f'NO — {e(why)}</span>'
        )
        origin_cell = (
            '<span class="rh-badge">global</span>'
            if origin == "global"
            else f'<span class="rh-badge">{e(origin)}</span>'
        )
        remove_action = (
            f"/{e(kind_name)}/remove"
            if origin == "global"
            else f"/armors/{e(active_name or '')}/{e(kind_name)}/remove"
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{e(ent.id)}</td>'
            f"<td>{origin_cell}</td>"
            f"<td>{applies_cell}</td>"
            f"<td>{e(ent.kind)}</td>"
            f"<td>{e(ent.scope)}</td>"
            f"<td>{e(_fmt_ago(ent.added_at))}</td>"
            f'<td class="mono">{e(ent.pattern)}</td>'
            f"<td>{e(ent.reason or '')}</td>"
            f'<td class="ara-pulse-cell">{_render_pulse_cell(ent.id, pulse_map.get(ent.id), token)}</td>'
            f"<td>"
            f'  <form method="post" action="{remove_action}?t={e(token)}">'
            f'    <input type="hidden" name="id" value="{e(ent.id)}">'
            '    <button class="ara-btn ara-btn-ghost">remove</button>'
            "  </form>"
            "</td>"
            "</tr>"
        )

    eyebrow = "Always-allow" if kind_name == "overrides" else "Always-deny"
    subtitle = (
        "Wins over any deny wall for matching resources. "
        "Merged view: global entries plus per-armor entries from the "
        f"{'currently equipped armor' if active_name else '(no armor equipped)'}."
        if kind_name == "overrides"
        else "Wins over everything, including overrides. "
        "Merged view: global entries plus per-armor entries from the "
        f"{'currently equipped armor' if active_name else '(no armor equipped)'}."
    )
    body = [
        '<section>',
        f'<div class="ara-eyebrow">{e(eyebrow)}</div>',
        f'<h2 class="ara-h">{e(kind_name.capitalize())}</h2>',
        f'<p class="ara-sub">{e(subtitle)}</p>',
        (
            '<div class="ara-empty">(empty)</div>'
            if not rows
            else '<table class="ara-table">'
                 '<thead><tr><th>ID</th><th>Origin</th><th>Applies now?</th>'
                 '<th>Kind</th><th>Scope</th>'
                 '<th>Age</th><th>Pattern</th><th>Reason</th>'
                 '<th>Pulse (1h)</th><th></th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table>'
        ),
        '<h3 class="ara-eyebrow" style="margin-top:22px">Add entry</h3>',
        f'<form class="ara-form" method="post" action="/{e(kind_name)}/add?t={e(token)}">',
        '  <label>kind'
        '    <select name="kind">'
        '      <option value="path">path</option>'
        '      <option value="command">command</option>'
        '    </select>'
        '  </label>',
        '  <label>pattern<input type="text" name="pattern" required></label>',
        '  <label>reason<input type="text" name="reason"></label>',
        '  <button class="ara-btn ara-btn-primary" type="submit">add</button>',
        "</form>",
        "</section>",
    ]
    return _page(kind_name.capitalize(), kind_name, "".join(body), token)


def render_danger(
    state_root: Path, token: str, *, flash: str | None = None,
    flash_level: str = "info",
) -> str:
    """Surface danger-classified target actions (both tiers): contents, why
    flagged, tier, whether auto-added to the armor bulwark, and an operator
    toggle to add/remove the bulwark deny."""
    from .. import danger_registry as _dr
    active_armor = armors_mod.active_armor_name(state_root) or ""
    flags = _dr.list_flags(state_root)
    flash_html = ""
    if flash:
        cls = ("--tertiary" if flash_level == "ok"
               else "--error" if flash_level == "bad" else "--primary")
        flash_html = (
            '<div class="ara-callout" '
            f'style="border-left:3px solid var({cls});'
            f'margin-bottom:14px">{e(flash)}</div>')

    def _badge(text: str, color: str, filled: bool) -> str:
        if filled:
            style = (f'background:color-mix(in srgb,var({color}) 22%,'
                     f'transparent);color:var({color})')
        else:
            style = 'opacity:.55'
        return (
            f'<span style="{style};padding:2px 8px;border-radius:'
            f'var(--radius-sm);font-size:10px;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:.04em">{e(text)}</span>')

    def _card(f: dict) -> str:
        tier = f.get("tier") or ""
        target = f.get("target") or ""
        reason = f.get("reason") or ""
        op = f.get("op") or ""
        layer = f.get("layer") or ""
        method = f.get("method") or ""
        typ = (f"{layer} · {method}" if layer and method
               else layer or op)
        added = bool(f.get("added_to_bulwark"))
        is_irr = tier == "irreversible"
        color = "--error" if is_irr else "--primary"
        tier_label = ("Irreversible — hard-denied" if is_irr
                      else "Provable — recorded")
        badge = (_badge("in bulwark", "--tertiary", True) if added
                 else _badge("not in bulwark", "--on-surface", False))
        add_val = "0" if added else "1"
        btn_label = "Remove from bulwark" if added else "Add to armor bulwark"
        btn_cls = "ara-btn-ghost" if added else "ara-btn-primary"
        form = (
            f'<form method="post" action="/danger/bulwark?t={e(token)}" '
            'style="margin:0">'
            f'<input type="hidden" name="op" value="{e(op)}">'
            f'<input type="hidden" name="target" value="{e(target)}">'
            f'<input type="hidden" name="tier" value="{e(tier)}">'
            f'<input type="hidden" name="add" value="{add_val}">'
            f'<button type="submit" class="ara-btn {btn_cls}" '
            f'style="padding:4px 10px;font-size:11px">{e(btn_label)}</button>'
            '</form>')
        return (
            '<div class="spoils-card">'
            '<div class="spoils-head">'
            f'<code style="font-size:12px;word-break:break-all">{e(target)}'
            '</code>'
            f'{_badge(tier_label, color, True)}'
            '</div>'
            f'<p class="ara-sub" style="margin:6px 0">{e(reason)} · type '
            f'<code>{e(typ)}</code></p>'
            '<div style="display:flex;gap:10px;align-items:center;'
            f'flex-wrap:wrap">{badge}{form}</div>'
            '</div>')

    irr = [f for f in flags if f.get("tier") == "irreversible"]
    prov = [f for f in flags if f.get("tier") == "provable"]

    def _section(title: str, sub: str, items: list) -> str:
        head = (
            f'<h3 class="ara-eyebrow" style="margin-top:22px">{e(title)} '
            f'<span class="spoils-count">{len(items)}</span></h3>'
            f'<p class="ara-sub">{e(sub)}</p>')
        if not items:
            return head + '<p class="ara-sub" style="opacity:.6">None.</p>'
        return head + "".join(_card(f) for f in items)

    body = (
        '<section>'
        '<div class="ara-eyebrow">Policy</div>'
        '<h2 class="ara-h">Danger flags</h2>'
        '<p class="ara-sub">Mechanically-classified target actions for armor '
        f'<strong>{e(active_armor or "—")}</strong>. Irreversible ops are '
        'hard-denied and auto-added to the bulwark; provable ops are recorded '
        'and allowed through. Toggle any entry in or out of the current '
        "armor's bulwark.</p>"
        + flash_html
        + _section(
            "Irreversible",
            "Unrecoverable without performing the act — always blocked.", irr)
        + _section(
            "Provable",
            "State-changing but potentially reversible — recorded, not "
            "blocked.", prov)
        + '</section>')
    return _page("Danger", "danger", body, token)


def render_settings(
    state_root: Path, token: str, *, viewer_token: str = "",
    flash: str | None = None, flash_level: str = "info",
) -> str:
    """Consolidates token-share + housekeeping purge + approval
    timeout env lookup in one place. Things that used to be scattered
    on /status."""
    import os as _os
    flash_html = ""
    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        flash_html = (
            '<div class="ara-callout" '
            f'style="border-left:3px solid var({cls});margin-bottom:14px">{e(flash)}</div>'
        )
    approval_env = _os.environ.get("ARA_APPROVAL_TIMEOUT_S") or "120"
    share = ""
    if viewer_token:
        share = (
            '<h3 class="ara-eyebrow" style="margin-top:22px">'
            'Read-only share link</h3>'
            '<p class="ara-sub">Anyone with this URL can view every page '
            'but cannot POST any mutation. Delete '
            '<code>armory-viewer-token</code> under the state root to '
            'rotate it.</p>'
            f'<input type="text" readonly value="?t={e(viewer_token)}" '
            'onclick="this.select()" class="mono" '
            'style="width:100%;max-width:520px;padding:6px 10px;'
            'background:var(--surface-container-lowest);'
            'border:0;border-radius:var(--radius-sm);'
            'font-family:var(--font-mono);font-size:11px;">'
        )
    tokens = (
        '<h3 class="ara-eyebrow" style="margin-top:22px">Auth tokens</h3>'
        '<p class="ara-sub">Admin token: <code>armory-token</code> under '
        'the state root (full access). Viewer token: '
        '<code>armory-viewer-token</code> (read-only). Rotate either '
        'by deleting the file — the Armory regenerates on next request.</p>'
        '<form method="post" action="/logout" style="display:inline;margin:0">'
        '<button type="submit" class="ara-btn ara-btn-ghost">'
        'Sign out this browser</button>'
        '</form>'
    )
    purge = (
        '<h3 class="ara-eyebrow" style="margin-top:22px">Housekeeping</h3>'
        '<p class="ara-sub">Bulk-delete archived session directories '
        'older than the chosen number of days. Still-running sessions '
        'are skipped.</p>'
        f'<form method="post" action="/sessions/cleanup?t={e(token)}" '
        'style="display:flex;gap:8px;align-items:center;flex-wrap:wrap" '
        'onsubmit="return confirm(\'Purge archived sessions older than '
        "'+this.days.value+' days?\')\">"
        '<span class="ara-sub">Purge older than</span>'
        '<input type="number" name="days" value="30" min="1" step="1" '
        'style="width:7ch;padding:3px 8px;font-size:11px">'
        '<span class="ara-sub">days</span>'
        '<button class="ara-btn ara-btn-bad" type="submit" '
        'style="padding:4px 10px;font-size:11px">Purge</button>'
        '</form>'
    )
    from .. import approvals as _approvals_mod
    _ag_on = _approvals_mod.autogrant_enabled(state_root)
    _ag_s = _approvals_mod.autogrant_timeout_s(state_root)
    approvals = (
        '<h3 class="ara-eyebrow" style="margin-top:22px">Approval timeout</h3>'
        '<p class="ara-sub">How long a Rhema <code>action=approval</code> '
        'turn blocks before failing closed. Global default is '
        f'<strong>{e(approval_env)} s</strong> — set the '
        '<code>ARA_APPROVAL_TIMEOUT_S</code> env var on the agent '
        'launch (openclaw or hermes) to change it. Individual rules can '
        'override via the '
        '<code>timeout_s</code> field on the add-rule form.</p>'
        '<h3 class="ara-eyebrow" style="margin-top:22px">'
        'Timeout auto-grant</h3>'
        '<p class="ara-sub">When on, a <em>routine</em> approval that no '
        'operator answers is auto-granted after the grace period below, so an '
        'IPC/approval blip cannot stall an autonomous run. '
        '<strong>Danger-classified endpoints are never auto-granted</strong> — '
        'irreversible ops are hard-denied, provable ops still need a human or '
        'crumb-backed proof. Every auto-grant is audited as '
        '<code>APPROVAL_AUTO_GRANTED</code>.</p>'
        f'<form method="post" action="/settings/approvals?t={e(token)}" '
        'style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
        '<label class="ara-sub" '
        'style="display:flex;gap:6px;align-items:center">'
        '<input type="checkbox" name="autogrant" value="on"'
        f'{" checked" if _ag_on else ""}> Enable auto-grant</label>'
        '<span class="ara-sub">grace</span>'
        f'<input type="number" name="autogrant_timeout_s" '
        f'value="{e(str(int(_ag_s)))}" min="1" step="1" '
        'style="width:7ch;padding:3px 8px;font-size:11px">'
        '<span class="ara-sub">s</span>'
        '<button class="ara-btn ara-btn-primary" type="submit" '
        'style="padding:4px 10px;font-size:11px">Save</button>'
        '</form>'
    )
    dashboard_url = _os.environ.get("HERMES_DASHBOARD_URL") or "http://127.0.0.1:9119"
    debug = (
        '<details class="settings-debug-card">'
        '<summary>'
        '<span class="ara-eyebrow">Debug</span>'
        '<strong>Backup Hermes chat</strong>'
        '<small>emergency side channel only</small>'
        '</summary>'
        '<div class="settings-debug-body">'
        '<p class="ara-sub">Use this only when the normal gateway or task '
        'surface is degraded. Hermes exposes a supported dashboard transport; '
        'Armory should treat it as a backup operator side channel, not as '
        'the default way to run ARA.</p>'
        '<div class="settings-debug-grid">'
        '<div><span>Dashboard</span>'
        f'<code>{e(dashboard_url)}</code></div>'
        '<div><span>Structured websocket</span><code>/api/ws</code></div>'
        '<div><span>Submit method</span><code>prompt.submit</code></div>'
        '<div><span>Terminal fallback</span><code>/api/pty</code></div>'
        '</div>'
        '<p class="ara-sub">Expected launch when needed: '
        '<code>hermes dashboard --host 127.0.0.1 --port 9119 --no-open</code>. '
        'If this becomes an active Armory control later, it should proxy '
        '<code>/api/ws</code>, resume or create a Hermes session, and route '
        'messages with <code>prompt.submit</code>.</p>'
        '</div>'
        '</details>'
    )
    body = (
        '<section>'
        '<div class="ara-eyebrow">Admin</div>'
        '<h2 class="ara-h">Settings</h2>'
        '<p class="ara-sub">Token rotation, share link, housekeeping, '
        'approval timeouts. Scoped to this state root.</p>'
        + flash_html
        + tokens + share + purge + approvals + debug
        + '</section>'
    )
    return _page("Settings", "settings", body, token)


def render_overrides(state_root: Path, token: str) -> str:
    # Legacy — kept only for backward-compat callers; the /overrides
    # route now 302s to /allowlist?tab=allow. This renderer still
    # works if something links directly to it.
    return _render_entry_page("overrides", state_root, token)


def render_blackholes(state_root: Path, token: str) -> str:
    return _render_entry_page("blackholes", state_root, token)


def render_allowlist(
    state_root: Path, token: str, *, tab: str = "allow",
) -> str:
    """Unified allow/deny page. Two tabs rendered client-side by
    toggling which kind's table/form is visible; the "Applies now?"
    merged view already works across both lists so no logic change."""
    active_name = armors_mod.active_armor_name(state_root)
    allow_body = _render_entry_page_inner("overrides", state_root, token)
    deny_body = _render_entry_page_inner("blackholes", state_root, token)
    # Tab pills + the two bodies in siblings that CSS toggles via
    # :target or active class.
    def _pill(key: str, label: str, color: str) -> str:
        is_active = (tab or "allow") == key
        style = (
            f'background:color-mix(in srgb,{color} 70%,transparent);'
            f'color:#fff;border:1px solid {color};'
            if is_active else
            f'background:color-mix(in srgb,{color} 12%,transparent);'
            f'color:{color};'
            f'border:1px solid color-mix(in srgb,{color} 35%,transparent);'
        )
        return (
            f'<a href="/allowlist?t={e(token)}&tab={key}" '
            f'style="text-decoration:none;padding:6px 14px;font-size:12px;'
            f'border-radius:14px;font-family:var(--font-mono);'
            f'letter-spacing:0.04em;text-transform:uppercase;{style}">'
            f'{e(label)}</a>'
        )
    pills = (
        '<div style="display:flex;gap:8px;margin:8px 0 14px 0">'
        + _pill("allow", "always allow", "#22c55e")
        + _pill("deny", "always deny", "#ef4444")
        + '</div>'
    )
    body = (
        '<section>'
        '<div class="ara-eyebrow">Policy</div>'
        '<h2 class="ara-h">Allowlist</h2>'
        '<p class="ara-sub">Resource-level always-on rules. '
        '<strong style="color:#22c55e">Always allow</strong> wins over '
        'matching deny walls; <strong style="color:#ef4444">always '
        'deny</strong> wins over everything including allows. Merged '
        'view shows global entries plus per-armor entries from the '
        f'{"currently equipped armor" if active_name else "(no armor equipped)"}.'
        '</p>'
        + pills
        + (allow_body if (tab or "allow") == "allow" else deny_body)
        + '</section>'
    )
    return _page("Allowlist", "allowlist", body, token)


def _render_entry_page_inner(
    kind_name: str, state_root: Path, token: str,
) -> str:
    """Body-only version of ``_render_entry_page`` — drops the outer
    <section>/eyebrow/heading so it can be embedded inside the unified
    /allowlist page. Reuses the same merged-view + add-form logic."""
    global_store = overrides_mod.load(state_root)
    active_name = armors_mod.active_armor_name(state_root)
    armor_store = None
    if active_name:
        armor = armors_mod.load_armor(state_root, active_name)
        if armor is not None:
            armor_store = overrides_mod.load_at(armor.overrides_path)

    def _pick(store) -> list:
        if store is None:
            return []
        return store.overrides if kind_name == "overrides" else store.blackholes

    now = time.time()
    merged: list[tuple] = [(ent, "global") for ent in _pick(global_store)]
    if armor_store is not None and active_name:
        merged.extend((ent, f"armor:{active_name}") for ent in _pick(armor_store))

    pulse_map = _rule_pulse(_load_policy_hits(state_root))

    def _applies(ent, origin: str) -> tuple[bool, str]:
        if ent.is_expired(now):
            return False, "expired"
        if origin.startswith("armor:") and not active_name:
            return False, "armor not equipped"
        if ent.scope and ent.scope.startswith("session:"):
            return False, f"session-scoped ({ent.scope})"
        return True, ""

    rows: list[str] = []
    for ent, origin in merged:
        applies, why = _applies(ent, origin)
        applies_cell = (
            '<span class="rh-badge" style="background:'
            'color-mix(in srgb,var(--tertiary) 25%,transparent)">YES</span>'
            if applies else
            '<span class="rh-badge" style="background:'
            f'color-mix(in srgb,var(--error) 18%,transparent)">NO — {e(why)}</span>'
        )
        origin_cell = (
            '<span class="rh-badge">global</span>' if origin == "global"
            else f'<span class="rh-badge">{e(origin)}</span>'
        )
        remove_action = (
            f"/{e(kind_name)}/remove" if origin == "global"
            else f"/armors/{e(active_name or '')}/{e(kind_name)}/remove"
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{e(ent.id)}</td>'
            f'<td>{origin_cell}</td>'
            f'<td>{applies_cell}</td>'
            f'<td>{e(ent.kind)}</td>'
            f'<td>{e(ent.scope)}</td>'
            f'<td>{e(_fmt_ago(ent.added_at))}</td>'
            f'<td class="mono">{e(ent.pattern)}</td>'
            f'<td>{e(ent.reason or "")}</td>'
            f'<td class="ara-pulse-cell">{_render_pulse_cell(ent.id, pulse_map.get(ent.id), token)}</td>'
            '<td>'
            f'<form method="post" action="{remove_action}?t={e(token)}">'
            f'<input type="hidden" name="id" value="{e(ent.id)}">'
            '<button class="ara-btn ara-btn-ghost">remove</button>'
            '</form>'
            '</td>'
            '</tr>'
        )
    empty_hint = (
        'No always-allow entries yet. Add one below to pre-authorize a '
        'resource that the deny walls would otherwise block.'
        if kind_name == "overrides" else
        'No always-deny entries yet. Add one below to hard-block a '
        'resource regardless of any allow rules.'
    )
    table = (
        f'<div class="ara-empty">{empty_hint}</div>' if not rows
        else '<table class="ara-table">'
             '<thead><tr><th>ID</th><th>Origin</th><th>Applies now?</th>'
             '<th>Kind</th><th>Scope</th>'
             '<th>Age</th><th>Pattern</th><th>Reason</th>'
             '<th>Pulse (1h)</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>'
    )
    form = (
        '<h3 class="ara-eyebrow" style="margin-top:22px">Add entry</h3>'
        f'<form class="ara-form" method="post" action="/{e(kind_name)}/add?t={e(token)}">'
        '<label>kind<select name="kind">'
        '<option value="path">path</option>'
        '<option value="command">command</option>'
        '</select></label>'
        '<label>pattern<input type="text" name="pattern" required></label>'
        '<label>reason<input type="text" name="reason"></label>'
        '<button class="ara-btn ara-btn-primary" type="submit">add</button>'
        '</form>'
    )
    return table + form


# ---------- audit ----------

def _load_policy_hits(
    state_root: Path, *, window_s: float = 3600.0, session_limit: int = 20,
) -> list[dict]:
    """Read policy_hits.jsonl from the N most-recent sessions, keep entries
    newer than ``window_s`` seconds old. Lightweight — no sorting beyond
    what's already file-order."""
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    cutoff = time.time() - window_s
    out: list[dict] = []
    for s in sorted(
        [p for p in sessions_dir.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:session_limit]:
        f = s / "policy_hits.jsonl"
        if not f.exists():
            continue
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if (r.get("ts") or 0) < cutoff:
                continue
            out.append(r)
    return out


def _rule_pulse(
    hits: list[dict], *, buckets: int = 30, window_s: float = 3600.0,
) -> dict[str, dict]:
    """Group hits by rule_id and build sparkline buckets over the window.

    Returns: { rule_id: {"count", "last_ts", "buckets": [int,...], "verdict_counts": {verdict: n}} }.
    """
    now = time.time()
    start = now - window_s
    bucket_size = window_s / buckets
    out: dict[str, dict] = {}
    for r in hits:
        rid = r.get("rule_id")
        if not rid:
            continue
        ts = r.get("ts") or 0
        if ts < start:
            continue
        entry = out.get(rid)
        if entry is None:
            entry = {
                "count": 0, "last_ts": 0.0,
                "buckets": [0] * buckets,
                "verdict_counts": {},
            }
            out[rid] = entry
        entry["count"] += 1
        if ts > entry["last_ts"]:
            entry["last_ts"] = ts
        idx = min(buckets - 1, max(0, int((ts - start) / bucket_size)))
        entry["buckets"][idx] += 1
        v = r.get("verdict") or ""
        entry["verdict_counts"][v] = entry["verdict_counts"].get(v, 0) + 1
    return out


def _sparkline_svg(
    counts: list[int], *, width: int = 90, height: int = 18,
    stroke: str = "currentColor",
) -> str:
    """Inline SVG sparkline (no deps). Returns empty string for empty input."""
    if not counts or max(counts) == 0:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            'class="ara-spark ara-spark-empty" aria-hidden="true">'
            f'<line x1="0" y1="{height-1}" x2="{width}" y2="{height-1}" '
            'stroke="currentColor" stroke-opacity="0.25" stroke-width="1"/></svg>'
        )
    n = len(counts)
    peak = max(counts) or 1
    step = width / (n - 1 if n > 1 else 1)
    pts = []
    for i, c in enumerate(counts):
        x = i * step
        y = height - 1 - (c / peak) * (height - 2)
        pts.append(f"{x:.1f},{y:.1f}")
    # Area fill beneath the polyline, then the line on top.
    area_pts = [f"0,{height}"] + pts + [f"{width},{height}"]
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        'class="ara-spark" aria-hidden="true">'
        f'<polygon points="{" ".join(area_pts)}" fill="{stroke}" fill-opacity="0.18"/>'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{stroke}" '
        'stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/>'
        '</svg>'
    )


def _render_pulse_cell(rid: str, pulse: dict | None, token: str | None = None) -> str:
    """Per-rule pulse summary cell: count · last-seen · sparkline.
    If token is given, wraps the whole cell in a link to /rules/<rid>."""
    if pulse is None or pulse.get("count", 0) == 0:
        return (
            '<span class="ara-sub" style="opacity:0.55">— no hits</span>'
        )
    count = pulse["count"]
    last_ago = _fmt_ago(pulse["last_ts"]) or "now"
    vc = pulse.get("verdict_counts") or {}
    top_verdict = max(vc, key=lambda k: vc[k]) if vc else ""
    color = (
        "var(--error)" if top_verdict == "deny"
        else "#d4a53a" if top_verdict == "approval"
        else "var(--tertiary)"
    )
    inner = (
        _sparkline_svg(pulse["buckets"], stroke=color)
        + f'<span class="mono" style="font-size:11px">{count}</span>'
        + f'<span class="ara-sub" style="font-size:11px">{e(last_ago)}</span>'
    )
    if token and rid:
        import urllib.parse as _up
        href = f'/rules/{_up.quote(rid, safe="")}?t={e(token)}'
        return (
            f'<a class="ara-pulse" href="{href}" '
            'style="display:inline-flex;gap:8px;align-items:center;'
            'text-decoration:none;color:inherit">'
            + inner
            + '</a>'
        )
    return (
        '<span class="ara-pulse" style="display:inline-flex;gap:8px;align-items:center">'
        + inner
        + '</span>'
    )


def _iter_recent_events(state_root: Path, limit: int = 200) -> Iterable[dict]:
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    rows: list[dict] = []
    for s in sorted(
        [p for p in sessions_dir.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:5]:
        for name in ("events.jsonl", "policy_hits.jsonl"):
            f = s / name
            if not f.exists():
                continue
            for line in f.read_text().splitlines()[-limit:]:
                try:
                    r = json.loads(line)
                    r["_session"] = s.name[:10]
                    r["_source"] = name
                    rows.append(r)
                except Exception:
                    continue
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit]


def render_audit_row(r: dict) -> str:
    """One audit table row, shared between initial server render and SSE.
    Data attributes (kind/verdict/session/source) let the client-side
    filter bar hide/show without a re-render."""
    kind = r.get("kind") or r.get("op") or ""
    verdict = r.get("verdict", "") or ""
    session = r.get("_session") or ""
    source = r.get("_source") or ""
    # Flag-style events (no verdict field) get pseudo-verdicts that the
    # filter chips still honour.
    if not verdict and kind in (
        "classifier_hit", "canary_hit", "mcp_scan_finding",
        "bulwark_deny", "skill_changed", "gateway_unsupervised",
    ):
        verdict = {
            "classifier_hit":      "flag",
            "canary_hit":          "flag",
            "mcp_scan_finding":    "flag",
            "skill_changed":       "flag",
            "gateway_unsupervised":"flag",
            "bulwark_deny":        "deny",
        }[kind]
    verdict_cls = (
        "verdict-deny" if verdict == "deny"
        else "verdict-approval" if verdict == "approval"
        else "verdict-allow" if verdict == "allow"
        else "verdict-flag" if verdict == "flag"
        else ""
    )
    return (
        f'<tr data-kind="{e(kind)}" data-verdict="{e(verdict)}"'
        f' data-session="{e(session)}" data-source="{e(source)}">'
        f'<td class="mono" data-label="Time">{e(_fmt_ts(r.get("ts")))}</td>'
        f'<td class="mono" data-label="Session">{e(session)}</td>'
        f'<td data-label="Source">{e(source)}</td>'
        f'<td data-label="Kind">{e(kind)}</td>'
        f'<td class="{e(verdict_cls)}" data-label="Verdict">{e(verdict)}</td>'
        f'<td class="mono" data-label="Reason">{e(r.get("reason") or "")}</td>'
        f'<td class="mono" data-label="Target">{e(r.get("path") or r.get("command") or r.get("touched") or "")}</td>'
        "</tr>"
    )


_AUDIT_STREAM_JS = """
<script>
(function() {
  const tbody = document.getElementById('audit-body');
  const status = document.getElementById('audit-stream-status');
  if (!tbody) return;
  const token = new URLSearchParams(location.search).get('t') || '';
  const MAX_ROWS = 500;
  const search = document.getElementById('audit-search');
  const verdictChips = document.querySelectorAll('.audit-chip-group[data-group="verdict"] .audit-chip');
  const hideHbCb = document.getElementById('audit-hide-hb-cb');
  const hideHbLabel = document.getElementById('audit-hide-hb');
  const sessionSel = document.getElementById('audit-session-filter');
  const visibleCount = document.getElementById('audit-visible-count');
  const pauseBtn = document.getElementById('audit-pause-btn');
  const bufferedCount = document.getElementById('audit-buffered-count');

  const limitChips = document.querySelectorAll('.audit-chip-group[data-group="limit"] .audit-chip');
  const filters = { q: '', verdict: '', hideHb: true, session: '', limit: 5 };
  let paused = false;
  const buffered = [];  // rows held while paused

  function rowMatches(row) {
    if (filters.hideHb && row.dataset.kind === 'heartbeat') return false;
    if (filters.verdict && row.dataset.verdict !== filters.verdict) return false;
    if (filters.session && row.dataset.session !== filters.session) return false;
    if (filters.q) {
      if (!row.textContent.toLowerCase().includes(filters.q)) return false;
    }
    return true;
  }

  function applyFilters() {
    let matched = 0;
    let shown = 0;
    const cap = filters.limit | 0;  // 0 == unlimited
    tbody.querySelectorAll('tr').forEach(r => {
      const m = rowMatches(r);
      if (!m) { r.style.display = 'none'; return; }
      matched++;
      if (cap && matched > cap) { r.style.display = 'none'; return; }
      r.style.display = '';
      shown++;
    });
    if (visibleCount) {
      const total = tbody.children.length;
      const capLabel = cap ? `last ${cap}` : 'all';
      if (matched === total && !cap) {
        visibleCount.textContent = `${total} events`;
      } else {
        visibleCount.textContent =
          `showing ${shown} (${capLabel}) of ${matched} matching · ${total} total`;
      }
    }
  }

  function ensureSessionOption(sid) {
    if (!sid || !sessionSel) return;
    for (const o of sessionSel.options) if (o.value === sid) return;
    const opt = document.createElement('option');
    opt.value = sid; opt.textContent = sid;
    sessionSel.appendChild(opt);
  }

  function prependRow(row) {
    row.classList.add('ara-row-new');
    tbody.insertBefore(row, tbody.firstChild);
    while (tbody.children.length > MAX_ROWS) {
      tbody.removeChild(tbody.lastChild);
    }
    setTimeout(() => row.classList.remove('ara-row-new'), 1200);
    ensureSessionOption(row.dataset.session);
  }

  function flushBuffered() {
    // Insert oldest first so newest-buffered ends up on top.
    while (buffered.length) prependRow(buffered.pop());
    updateBufferedCount();
    applyFilters();
  }

  function updateBufferedCount() {
    if (!bufferedCount) return;
    if (!paused || !buffered.length) {
      bufferedCount.hidden = true; bufferedCount.textContent = '';
      return;
    }
    bufferedCount.hidden = false;
    bufferedCount.textContent = `${buffered.length} new · click resume to show`;
  }

  function setPaused(v) {
    paused = v;
    if (pauseBtn) {
      pauseBtn.textContent = paused ? '▶ resume' : '⏸ pause';
      pauseBtn.classList.toggle('audit-chip-on', paused);
    }
    if (!paused) flushBuffered();
  }

  if (pauseBtn) pauseBtn.addEventListener('click', () => setPaused(!paused));
  if (search) search.addEventListener('input', () => {
    filters.q = search.value.trim().toLowerCase();
    applyFilters();
  });
  verdictChips.forEach(c => c.addEventListener('click', () => {
    verdictChips.forEach(x => x.classList.remove('audit-chip-on'));
    c.classList.add('audit-chip-on');
    filters.verdict = c.dataset.verdict || '';
    applyFilters();
  }));
  if (hideHbCb) hideHbCb.addEventListener('change', () => {
    filters.hideHb = hideHbCb.checked;
    hideHbLabel?.classList.toggle('audit-chip-on', hideHbCb.checked);
    applyFilters();
  });
  if (sessionSel) sessionSel.addEventListener('change', () => {
    filters.session = sessionSel.value;
    applyFilters();
  });
  limitChips.forEach(c => c.addEventListener('click', () => {
    limitChips.forEach(x => x.classList.remove('audit-chip-on'));
    c.classList.add('audit-chip-on');
    filters.limit = parseInt(c.dataset.limit || '0', 10);
    applyFilters();
  }));

  // Seed visible count on load.
  applyFilters();

  // --- SSE wiring ------------------------------------------------------
  let es = null;
  let retries = 0;

  function setStatus(state, text) {
    if (!status) return;
    status.dataset.state = state;
    status.textContent = text;
  }

  function connect() {
    setStatus('connecting', 'connecting...');
    es = new EventSource('/events?t=' + encodeURIComponent(token));
    es.onopen = () => { retries = 0; setStatus('live', 'live'); };
    es.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg.html) return;
      const tmp = document.createElement('tbody');
      tmp.innerHTML = msg.html;
      const row = tmp.firstChild;
      if (!row) return;
      if (paused) {
        buffered.push(row);
        updateBufferedCount();
      } else {
        prependRow(row);
        if (!rowMatches(row)) row.style.display = 'none';
        applyFilters();
      }
      if (typeof window.__ARA_TIMELINE_INGEST__ === 'function' && msg.verdict) {
        window.__ARA_TIMELINE_INGEST__({ verdict: msg.verdict });
      }
    };
    es.onerror = () => {
      setStatus('reconnecting', 'reconnecting...');
      es.close();
      retries = Math.min(retries + 1, 6);
      setTimeout(connect, 500 * Math.pow(2, retries));
    };
  }
  connect();
})();
</script>
<style>
  #audit-stream-status { font-family: var(--font-mono); font-size: 11px; }
  #audit-stream-status[data-state="live"]         { color: var(--tertiary); }
  #audit-stream-status[data-state="connecting"],
  #audit-stream-status[data-state="reconnecting"] { color: var(--warn, #d4a53a); }
  tr.ara-row-new {
    animation: ara-row-flash 1.2s ease-out;
  }
  @keyframes ara-row-flash {
    from { background: color-mix(in srgb, var(--primary) 22%, transparent); }
    to   { background: transparent; }
  }
  .audit-filters {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    margin: 16px 0 10px;
    padding: 10px 12px;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--surface-container-lowest) 55%, transparent);
    border: 1px solid color-mix(in srgb, var(--outline-variant) 18%, transparent);
  }
  #audit-search {
    flex: 1; min-width: 200px;
    padding: 6px 10px; border-radius: 6px;
    background: var(--surface-container-lowest);
    border: 1px solid color-mix(in srgb, var(--outline-variant) 25%, transparent);
    color: var(--on-surface); font-family: var(--font-body); font-size: 13px;
  }
  #audit-search:focus { outline: 1px solid var(--primary); border-color: var(--primary); }
  .audit-chip-group {
    display: flex; gap: 2px;
    background: var(--surface-container-lowest);
    border-radius: 999px; padding: 2px;
    border: 1px solid color-mix(in srgb, var(--outline-variant) 20%, transparent);
  }
  .audit-chip {
    appearance: none; border: 0; cursor: pointer;
    padding: 4px 12px; border-radius: 999px;
    font-family: var(--font-body); font-size: 12px;
    background: transparent; color: var(--text-mute);
    display: inline-flex; align-items: center; gap: 6px;
  }
  .audit-chip:hover { color: var(--on-surface); }
  .audit-chip.audit-chip-on {
    background: color-mix(in srgb, var(--primary) 22%, transparent);
    color: var(--on-surface);
  }
  label.audit-chip { border: 1px solid color-mix(in srgb, var(--outline-variant) 20%, transparent); }
  label.audit-chip input { accent-color: var(--primary); margin: 0; }
  .audit-select {
    padding: 4px 8px; border-radius: 6px;
    background: var(--surface-container-lowest);
    color: var(--on-surface); font-family: var(--font-mono); font-size: 12px;
    border: 1px solid color-mix(in srgb, var(--outline-variant) 25%, transparent);
  }
  #audit-visible-count {
    font-family: var(--font-mono); font-size: 11px;
    color: var(--text-mute);
  }
  #audit-pause-btn {
    font-family: var(--font-mono); font-size: 11px;
    margin-left: 4px;
  }
  .audit-buffered {
    margin-left: 6px;
    font-family: var(--font-mono); font-size: 11px;
    padding: 2px 8px; border-radius: 999px;
    background: color-mix(in srgb, #d4a53a 25%, transparent);
    color: #d4a53a;
  }
  @media (max-width: 640px) {
    .audit-filters { flex-direction: column; align-items: stretch; }
    #audit-search { width: 100%; }
  }
</style>
"""


_TIMELINE_JS = """
<script>
(function() {
  const host = document.getElementById('audit-timeline');
  if (!host) return;
  const token = new URLSearchParams(location.search).get('t') || '';

  // Config pulled from the host dataset; server sets the same values in the
  // initial histogram so buckets align.
  const BUCKETS = parseInt(host.dataset.buckets, 10) || 60;
  const WINDOW_S = parseInt(host.dataset.windowS, 10) || 900;
  const BUCKET_S = WINDOW_S / BUCKETS;
  const COLORS = {
    allow:   'rgba(78,222,163,0.70)',
    approval:'rgba(212,165,58,0.72)',
    deny:    'rgba(221,96,84,0.78)',
  };

  // Series state: three parallel arrays of length BUCKETS, newest on the right.
  const state = {
    allow:    new Array(BUCKETS).fill(0),
    approval: new Array(BUCKETS).fill(0),
    deny:     new Array(BUCKETS).fill(0),
    anchor:   Math.floor(Date.now() / 1000),
  };

  // Hydrate from server-rendered initial data.
  try {
    const raw = host.dataset.initial;
    if (raw) {
      const parsed = JSON.parse(raw);
      state.allow = parsed.allow || state.allow;
      state.approval = parsed.approval || state.approval;
      state.deny = parsed.deny || state.deny;
      state.anchor = parsed.anchor || state.anchor;
    }
  } catch (e) { /* fall back to empty */ }

  function render() {
    const w = host.clientWidth || 800;
    const h = 120;
    const stepX = w / BUCKETS;
    const totals = new Array(BUCKETS).fill(0);
    for (let i = 0; i < BUCKETS; i++) {
      totals[i] = state.allow[i] + state.approval[i] + state.deny[i];
    }
    const peak = Math.max(1, ...totals);

    function areaPath(series, offsetFn) {
      const topPts = [];
      const botPts = [];
      for (let i = 0; i < BUCKETS; i++) {
        const x = i * stepX;
        const bot = offsetFn(i);
        const top = bot + series[i];
        const yTop = h - (top / peak) * h;
        const yBot = h - (bot / peak) * h;
        topPts.push(x + ',' + yTop);
        botPts.push(x + ',' + yBot);
      }
      const last = (BUCKETS - 1) * stepX;
      return `M${topPts.join(' L')} L${last},${h} L0,${h} Z`;
    }

    // Stack order: deny (bottom), approval, allow (top).
    const denyPath = areaPath(state.deny, () => 0);
    const apprPath = areaPath(state.approval, (i) => state.deny[i]);
    const allowPath = areaPath(state.allow, (i) => state.deny[i] + state.approval[i]);

    // Grid: a subtle mid-line.
    host.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" class="ara-timeline">
        <line x1="0" y1="${h/2}" x2="${w}" y2="${h/2}"
              stroke="currentColor" stroke-opacity="0.08" stroke-width="1"/>
        <path d="${denyPath}"  fill="${COLORS.deny}"/>
        <path d="${apprPath}"  fill="${COLORS.approval}"/>
        <path d="${allowPath}" fill="${COLORS.allow}"/>
      </svg>
      <div class="ara-timeline-legend">
        <span><i style="background:${COLORS.allow}"></i>allow</span>
        <span><i style="background:${COLORS.approval}"></i>approval</span>
        <span><i style="background:${COLORS.deny}"></i>deny</span>
        <span style="margin-left:auto" class="ara-sub">
          ${WINDOW_S/60}m window · ${BUCKETS} buckets · peak ${peak}/bucket
        </span>
      </div>
    `;
  }

  function shiftIfNeeded() {
    const now = Math.floor(Date.now() / 1000);
    const drift = now - state.anchor;
    if (drift < BUCKET_S) return;
    const shift = Math.min(BUCKETS, Math.floor(drift / BUCKET_S));
    for (const k of ['allow','approval','deny']) {
      state[k].splice(0, shift);
      for (let i = 0; i < shift; i++) state[k].push(0);
    }
    state.anchor += shift * BUCKET_S;
  }

  window.__ARA_TIMELINE_INGEST__ = function(event) {
    shiftIfNeeded();
    const v = event.verdict || '';
    if (!(v in state)) return;
    state[v][BUCKETS - 1] += 1;
    render();
  };

  render();
  // Shift every second so the chart breathes even without events.
  setInterval(() => { shiftIfNeeded(); render(); }, 1000);
  // Redraw on resize for responsive width.
  window.addEventListener('resize', render);
})();
</script>
<style>
  #audit-timeline {
    width: 100%;
    margin: 8px 0 22px 0;
    padding: 8px 10px 4px 10px;
    background: color-mix(in srgb, var(--surface-container-lowest) 80%, transparent);
    border-radius: var(--radius-md);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline-variant) 18%, transparent);
  }
  .ara-timeline { display: block; color: var(--on-surface); }
  .ara-timeline-legend {
    display: flex; gap: 14px; align-items: center;
    padding: 6px 4px 2px 4px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--on-surface);
  }
  .ara-timeline-legend i {
    display: inline-block; width: 10px; height: 10px;
    border-radius: 2px; margin-right: 6px;
    vertical-align: -1px;
  }
</style>
"""


def _build_timeline_initial(
    events: list[dict], *, buckets: int = 60, window_s: int = 900,
) -> dict:
    """Bucket recent events into allow/approval/deny series."""
    now = time.time()
    start = now - window_s
    bucket_s = window_s / buckets
    allow = [0] * buckets
    appr = [0] * buckets
    deny = [0] * buckets
    for r in events:
        ts = r.get("ts") or 0
        if ts < start:
            continue
        idx = min(buckets - 1, max(0, int((ts - start) / bucket_s)))
        v = r.get("verdict") or ""
        if v == "allow":
            allow[idx] += 1
        elif v == "approval":
            appr[idx] += 1
        elif v == "deny":
            deny[idx] += 1
    return {
        "allow": allow, "approval": appr, "deny": deny,
        "anchor": int(now),
    }


def render_audit(state_root: Path, token: str, *, tab: str = "stream") -> str:
    """Legacy wrapper — /audit now redirects to /courtyard?tab=live."""
    if tab == "approvals":
        return _page("Courtyard · Approvals", "courtyard",
                     _render_approvals_body(state_root, token), token)
    return _page("Courtyard · Live stream", "courtyard",
                 _render_audit_stream_body(state_root, token), token)


def _render_audit_stream_body(state_root: Path, token: str) -> str:
    """Body fragment (no page chrome) for the live-audit view. Used by
    /courtyard's Live stream tab and the legacy /audit redirect."""
    _unused_guard = None  # keep the indent level reference
    events = list(_iter_recent_events(state_root, limit=1000))
    rows = [render_audit_row(r) for r in events[:200]]
    timeline_initial = _build_timeline_initial(events)
    import html as _html
    initial_attr = _html.escape(json.dumps(timeline_initial), quote=True)

    # Build session dropdown options from the initial slice (short ids,
    # newest first). SSE rows will add more via the JS handler.
    sessions_seen: list[str] = []
    for ev in events[:200]:
        sid = ev.get("_session") or ""
        if sid and sid not in sessions_seen:
            sessions_seen.append(sid)
    session_opts = "".join(
        f'<option value="{e(s)}">{e(s)}</option>' for s in sessions_seen[:30]
    )

    # Kinds seen in the visible slice (short, useful chip set).
    kinds_seen: list[str] = []
    for ev in events[:200]:
        k = ev.get("kind") or ev.get("op") or ""
        if k and k not in kinds_seen:
            kinds_seen.append(k)

    # Row-count limiter. Default = 5 (skimmable); operators can expand as
    # needed without reloading.
    limit_chips = (
        '<div class="audit-chip-group" data-group="limit" title="Rows to show">'
        '<button class="audit-chip audit-chip-on" data-limit="5">5</button>'
        '<button class="audit-chip" data-limit="10">10</button>'
        '<button class="audit-chip" data-limit="30">30</button>'
        '<button class="audit-chip" data-limit="50">50</button>'
        '<button class="audit-chip" data-limit="100">100</button>'
        '<button class="audit-chip" data-limit="0">ALL</button>'
        '</div>'
    )

    filter_bar = (
        '<div class="audit-filters">'
        '<input type="search" id="audit-search" placeholder="search target / reason / kind…" '
        'autocomplete="off">'
        '<div class="audit-chip-group" data-group="verdict">'
        '<button class="audit-chip audit-chip-on" data-verdict="">all</button>'
        '<button class="audit-chip" data-verdict="deny">deny</button>'
        '<button class="audit-chip" data-verdict="flag" '
        'title="Classifier hits, canary hits, and MCP-Scan findings">'
        'flag</button>'
        '<button class="audit-chip" data-verdict="approval">approval</button>'
        '<button class="audit-chip" data-verdict="allow">allow</button>'
        '</div>'
        '<label class="audit-chip audit-chip-on" id="audit-hide-hb">'
        '<input type="checkbox" id="audit-hide-hb-cb" checked> hide heartbeats'
        '</label>'
        + limit_chips
        + (
            '<select id="audit-session-filter" class="audit-select">'
            '<option value="">all sessions</option>'
            + session_opts +
            '</select>'
            if session_opts else ''
        )
        + '<span style="margin-left:auto" id="audit-visible-count"></span>'
        + '</div>'
    )

    # (tab bar is rendered by render_courtyard — this body produces
    # just the stream content fragment.)

    body = [
        '<section class="courtyard-live-shell">',
        '<div class="courtyard-tab-panel">',
        '<div class="courtyard-panel-head">',
        '<div><div class="ara-eyebrow">Live stream</div>',
        '<h2 class="ara-h">Audit fireline</h2></div>',
        '<p class="ara-sub">',
        f'Last {len(rows)} events across recent sessions · ',
        '<span id="audit-stream-status" data-state="connecting">connecting...</span>',
        ' · <button id="audit-pause-btn" class="audit-chip">⏸ pause</button>',
        ' <span id="audit-buffered-count" class="audit-buffered" hidden></span>',
        '</p>',
        '</div>',
        '<div class="courtyard-stream-rail">',
        '<article class="courtyard-stream-card courtyard-stream-timeline">',
        '<div class="ara-eyebrow">Pressure</div>',
        '<div id="audit-timeline" data-buckets="60" data-window-s="900"'
        f' data-initial="{initial_attr}"></div>',
        _TIMELINE_JS,
        '</article>',
        '<article class="courtyard-stream-card courtyard-stream-filters">',
        '<div class="ara-eyebrow">Filters</div>',
        filter_bar,
        '</article>',
        '</div>',
        (
            '<div class="ara-empty">No audit events yet.</div>'
            if not rows
            else '<table class="ara-table">'
                 '<thead><tr>'
                 '<th>Timestamp</th><th>Session</th><th>Source</th>'
                 '<th>Kind</th><th>Verdict</th><th>Reason</th><th>Target</th>'
                 '</tr></thead>'
                 '<tbody id="audit-body">'
                 + "".join(rows)
                 + '</tbody></table>'
        ),
        '</div></section>',
        _AUDIT_STREAM_JS,
    ]
    if not rows:
        body.insert(-1,
            '<table class="ara-table" style="margin-top:12px">'
            '<thead><tr>'
            '<th>Timestamp</th><th>Session</th><th>Source</th>'
            '<th>Kind</th><th>Verdict</th><th>Reason</th><th>Target</th>'
            '</tr></thead>'
            '<tbody id="audit-body"></tbody></table>'
        )
    return "".join(body)


# ---------- armor sets ----------

_TIER_ROMAN = ["", "I", "II", "III", "IV", "V"]
_TIER_LABEL = {
    1: "MINIMAL", 2: "LIGHT", 3: "STANDARD", 4: "HEAVY", 5: "FORTIFIED",
}
_TIER_RARITY = {
    1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "epic",
}
_TIER_RARITY_COLOR = {
    1: "#8a98a8", 2: "#4edea3", 3: "#4aa6cc", 4: "#cc66ff", 5: "#ffd166",
}

_ARMOR_SIGILS = {
    "shield":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M12 2 L20 6 V13 C20 18 16 21 12 22 C8 21 4 18 4 13 V6 Z"/><path d="M9 12l2 2 4-4" stroke-linecap="round"/></svg>',
    "castle":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3 21V9l3 3V9l3-3 3 3V6l3-3 3 3v3l3 3v9z"/><path d="M10 21v-4h4v4"/></svg>',
    "flask":   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M9 3v5L4 19a2 2 0 002 3h12a2 2 0 002-3l-5-11V3"/><path d="M8 3h8"/></svg>',
    "hammer":  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 15l6-6M9 15l6-6M18 2l4 4-6 6-4-4zM3 20l4-4" stroke-linecap="round"/></svg>',
    "lock":    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>',
}


def _render_channel_pills(channels: list[str], *, compact: bool = False) -> str:
    """Render a row of channel-scope pills for an armor. Empty list →
    one neutral "GLOBAL" pill (the default ARA semantics for unscoped
    armors). Each known channel id from ``CHANNEL_REGISTRY`` gets its
    brand color; unknown ids fall through to a generic gray.

    ``compact`` shrinks the pill (font + padding) for use inside dense
    rows like /absolute armor tabs.
    """
    pad = "1px 6px" if compact else "2px 8px"
    fs = "9px" if compact else "10px"
    if not channels:
        return (
            f'<span class="ara-channel-pill" '
            f'style="background:color-mix(in srgb,var(--outline-variant) 35%,transparent);'
            f'color:var(--on-surface);padding:{pad};font-size:{fs};'
            f'border-radius:10px;font-family:var(--font-mono);'
            f'letter-spacing:0.05em;text-transform:uppercase">'
            f'global</span>'
        )
    out = []
    for cid in channels:
        color = armors_mod.channel_color(cid)
        label = armors_mod.channel_label(cid)
        out.append(
            f'<span class="ara-channel-pill" title="Scoped to {e(label)}" '
            f'style="background:color-mix(in srgb,{color} 25%,transparent);'
            f'color:{color};padding:{pad};font-size:{fs};'
            f'border-radius:10px;font-family:var(--font-mono);'
            f'letter-spacing:0.05em;text-transform:uppercase;'
            f'border:1px solid color-mix(in srgb,{color} 45%,transparent)">'
            f'{e(label)}</span>'
        )
    return ' '.join(out)


def _render_armor_card(a, token: str, hero: bool = False) -> str:
    tier = a.stats.tier
    rarity = _TIER_RARITY[tier]
    tier_roman = _TIER_ROMAN[tier]
    tier_label = _TIER_LABEL[tier]
    tier_color = _TIER_RARITY_COLOR[tier]
    sigil = _ARMOR_SIGILS.get(a.meta.sigil or "shield", _ARMOR_SIGILS["shield"])
    equipped = a.equipped

    # Card level = tier × 10 + total rules (a rough progress number).
    level = tier * 10 + min(a.stats.total, 99 - tier * 10)
    # XP progress toward next tier.
    tier_mins = [0, 0, 6, 16, 31, 60]
    lower = tier_mins[tier]
    upper = tier_mins[tier + 1] if tier < 5 else max(lower + 50, a.stats.total + 10)
    xp_total = a.stats.total - lower
    xp_needed = upper - lower
    xp_pct = max(5, min(100, int(xp_total / max(1, xp_needed) * 100)))
    # Coverage: share of slots populated (arbitrary ceiling of 50 for a full kit).
    cov_pct = min(100, int(a.stats.total / 50 * 100))

    level_subtitle = (a.meta.description[:28] or "UNDESCRIBED").upper()

    action_form = (
        f'<form method="post" action="/armors/{e(a.name)}/equip?t={e(token)}" '
        f'style="padding:10px 20px 16px;display:flex;gap:8px">'
        + (
            '<button class="ara-btn ara-btn-ghost" disabled '
            'style="opacity:0.6;cursor:default">EQUIPPED</button>'
            if equipped
            else '<button class="ara-btn ara-btn-primary" type="submit">EQUIP</button>'
        )
        + f'<a class="ara-btn ara-btn-ghost" href="/armors/{e(a.name)}?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">VIEW</a>'
        '</form>'
    )

    return (
        f'<div class="bc-card bc-rarity-{rarity}"'
        f' style="{"width:100%;max-width:420px;" if hero else "width:100%"}">'
        '<div class="bc-card-inner">'
        '<div class="bc-holo-shimmer"></div>'
        '<div class="bc-rarity-banner">'
        '<div class="bc-rarity-stars">'
        + ("&#10022; " * tier).strip()
        + '</div>'
        f'<div class="bc-rarity-label" style="color:{tier_color}">'
        f'TIER {tier_roman} &bull; {tier_label}</div>'
        '</div>'
        '<div class="bc-card-art">'
        f'{_render_pixel_art("armor", a.name, a.name)}'
        f'<div class="bc-sigil">{sigil}</div>'
        '<div class="bc-name-plate">'
        f'<h2>{e(a.name.upper())}</h2>'
        f'<div class="bc-level">&#9670; {e(level_subtitle)} &bull; LVL {level} &#9670;</div>'
        f'<div style="margin-top:6px;display:flex;gap:4px;justify-content:center;flex-wrap:wrap">'
        f'{_render_channel_pills(a.meta.channels)}</div>'
        '</div></div>'
        '<div class="bc-stats-row">'
        f'<div class="bc-stat-box"><div class="bc-stat-value">{a.stats.rules}</div>'
        '<div class="bc-stat-label">RUL</div><div class="bc-stat-sublabel">RULES</div></div>'
        f'<div class="bc-stat-box"><div class="bc-stat-value">{a.stats.deny}</div>'
        '<div class="bc-stat-label">DNY</div><div class="bc-stat-sublabel">DENY</div></div>'
        f'<div class="bc-stat-box"><div class="bc-stat-value">{a.stats.approvals}</div>'
        '<div class="bc-stat-label">APR</div><div class="bc-stat-sublabel">APPROVAL</div></div>'
        f'<div class="bc-stat-box" title="'
        f'{a.stats.canary_seeded} canary(ies) seeded, {a.stats.canary_hits} hit(s). '
        f'Hits fire on /audit as canary_hit events.">'
        f'<div class="bc-stat-value" '
        f'style="color:{"var(--error)" if a.stats.canary_hits > 0 else "inherit"}">'
        f'{a.stats.canary_hits}</div>'
        '<div class="bc-stat-label">CAN</div>'
        f'<div class="bc-stat-sublabel">CANARY/{a.stats.canary_seeded}</div></div>'
        '</div>'
        '<div class="bc-card-xp">'
        '<div class="bc-card-xp-label">'
        f'<span>RULES</span><span>{a.stats.total} / {upper}</span></div>'
        f'<div class="bc-card-xp-bar"><div class="bc-card-xp-fill" style="width:{xp_pct}%"></div></div>'
        '</div>'
        '<div class="bc-collection">'
        '<span class="bc-collection-label">COVERAGE</span>'
        f'<div class="bc-collection-bar"><div class="bc-collection-fill" style="width:{cov_pct}%"></div></div>'
        f'<span class="bc-collection-pct">{a.stats.total} / 50 &bull; {cov_pct}%</span>'
        '</div>'
        + action_form
        + '</div></div>'
    )


_ARMOR_EMPTY_HINT = """
<div class="ara-empty">
  <div><strong>No armor sets yet.</strong></div>
  <div style="margin-top:8px;font-family:var(--font-body)">Create your first one:</div>
  <pre class="ara-code" style="margin-top:8px;background:var(--surface-container)">ara armor create default \\
    --from-walls WALLS.md \\
    --description "baseline protection"
ara armor equip default</pre>
</div>
"""


_PIXEL_PALETTES = [
    {
        "metal": "#ffd166",
        "shade": "#9b5c42",
        "trim": "#d9836b",
        "glow": "#8cd0e9",
        "cloth": "#2f5f67",
        "outline": "#17120f",
    },
    {
        "metal": "#8cd0e9",
        "shade": "#34556c",
        "trim": "#7dd3a8",
        "glow": "#dfe2f1",
        "cloth": "#314b3f",
        "outline": "#121820",
    },
    {
        "metal": "#d9836b",
        "shade": "#643b32",
        "trim": "#ffd166",
        "glow": "#b48cff",
        "cloth": "#4a2e48",
        "outline": "#18110f",
    },
    {
        "metal": "#dfe2f1",
        "shade": "#697184",
        "trim": "#8cd0e9",
        "glow": "#ffd166",
        "cloth": "#3a4152",
        "outline": "#11131a",
    },
    {
        "metal": "#7dd3a8",
        "shade": "#395f46",
        "trim": "#d1c5b1",
        "glow": "#d9836b",
        "cloth": "#233c30",
        "outline": "#101812",
    },
]


def _pixel_seed(kind: str, seed: str) -> int:
    digest = hashlib.sha256(f"{kind}:{seed}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _pixel_palette(n: int) -> dict[str, str]:
    return _PIXEL_PALETTES[n % len(_PIXEL_PALETTES)]


def _pixel_rects(kind: str, n: int) -> tuple[str, list[tuple[int, int, int, int, str]]]:
    variant = n % 6
    if kind == "weapon":
        if variant in (0, 3):
            rects = [
                (15, 1, 2, 1, "glow"), (14, 2, 4, 2, "outline"),
                (15, 3, 2, 15, "metal"), (14, 5, 1, 11, "shade"),
                (17, 5, 1, 10, "glow"), (13, 18, 6, 3, "outline"),
                (10, 20, 12, 3, "trim"), (12, 21, 8, 1, "glow"),
                (15, 23, 2, 6, "shade"), (14, 28, 4, 2, "outline"),
                (13, 30, 6, 1, "trim"), (18, 11, 2, 2, "trim"),
                (12, 14, 2, 2, "trim"), (20, 6, 1, 1, "glow"),
                (10, 9, 1, 1, "glow"), (22, 18, 2, 2, "outline"),
                (8, 18, 2, 2, "outline"), (16, 1, 1, 1, "metal"),
            ]
            return ("steamblade", rects)
        if variant == 1:
            rects = [
                (15, 6, 3, 22, "outline"), (16, 7, 1, 20, "shade"),
                (9, 4, 8, 3, "outline"), (7, 7, 11, 4, "outline"),
                (6, 11, 8, 4, "outline"), (8, 6, 8, 2, "metal"),
                (8, 9, 8, 2, "metal"), (7, 12, 6, 2, "trim"),
                (12, 5, 2, 6, "glow"), (18, 10, 4, 3, "outline"),
                (20, 13, 3, 3, "trim"), (17, 20, 8, 2, "outline"),
                (18, 21, 6, 1, "glow"), (14, 27, 5, 3, "outline"),
                (15, 28, 3, 1, "trim"), (5, 14, 2, 1, "glow"),
                (10, 3, 1, 1, "glow"), (21, 9, 1, 1, "metal"),
            ]
            return ("gearaxe", rects)
        if variant == 2:
            rects = [
                (15, 8, 3, 20, "outline"), (16, 9, 1, 18, "shade"),
                (7, 5, 18, 6, "outline"), (8, 6, 16, 4, "metal"),
                (7, 11, 4, 3, "outline"), (21, 11, 4, 3, "outline"),
                (10, 6, 3, 4, "trim"), (19, 6, 3, 4, "trim"),
                (12, 4, 8, 2, "glow"), (14, 3, 4, 1, "metal"),
                (13, 20, 7, 3, "outline"), (14, 21, 5, 1, "trim"),
                (14, 27, 5, 3, "outline"), (15, 28, 3, 1, "trim"),
                (6, 7, 1, 1, "glow"), (25, 8, 1, 1, "glow"),
                (9, 14, 2, 2, "shade"), (21, 14, 2, 2, "shade"),
            ]
            return ("clockhammer", rects)
        if variant == 4:
            rects = [
                (15, 4, 3, 24, "outline"), (16, 5, 1, 22, "shade"),
                (13, 1, 7, 5, "outline"), (14, 2, 5, 3, "metal"),
                (15, 0, 3, 2, "glow"), (12, 6, 9, 2, "trim"),
                (13, 10, 7, 2, "trim"), (12, 14, 9, 2, "trim"),
                (14, 18, 5, 2, "glow"), (10, 22, 13, 3, "outline"),
                (12, 23, 9, 1, "metal"), (14, 27, 5, 3, "outline"),
                (15, 28, 3, 1, "trim"), (11, 3, 1, 1, "glow"),
                (21, 3, 1, 1, "glow"), (10, 17, 2, 2, "outline"),
                (21, 17, 2, 2, "outline"), (16, 1, 1, 1, "metal"),
            ]
            return ("rune-staff", rects)
        rects = [
            (15, 4, 3, 23, "outline"), (16, 5, 1, 21, "shade"),
            (14, 1, 5, 5, "outline"), (15, 2, 3, 3, "metal"),
            (13, 6, 7, 2, "trim"), (11, 8, 11, 3, "outline"),
            (12, 9, 9, 1, "metal"), (10, 12, 13, 2, "trim"),
            (12, 15, 9, 2, "outline"), (14, 17, 5, 2, "glow"),
            (13, 22, 7, 3, "outline"), (14, 23, 5, 1, "trim"),
            (14, 27, 5, 3, "outline"), (15, 28, 3, 1, "trim"),
            (8, 8, 2, 2, "glow"), (23, 12, 1, 1, "glow"),
            (13, 0, 1, 1, "glow"), (19, 0, 1, 1, "glow"),
        ]
        return ("spear-wrench", rects)
    if kind == "trim":
        if variant in (0, 4):
            rects = [
                (5, 4, 22, 2, "outline"), (6, 5, 20, 1, "trim"),
                (4, 6, 3, 20, "outline"), (25, 6, 3, 20, "outline"),
                (6, 7, 2, 18, "cloth"), (24, 7, 2, 18, "cloth"),
                (8, 25, 16, 3, "outline"), (9, 26, 14, 1, "trim"),
                (10, 8, 3, 3, "glow"), (19, 8, 3, 3, "glow"),
                (12, 12, 8, 2, "trim"), (13, 15, 6, 2, "glow"),
                (11, 19, 10, 2, "shade"), (8, 22, 2, 2, "metal"),
                (22, 22, 2, 2, "metal"), (3, 27, 3, 1, "glow"),
                (26, 27, 3, 1, "glow"), (15, 6, 2, 18, "shade"),
            ]
            return ("ward-cloak", rects)
        if variant == 1:
            rects = [
                (7, 7, 18, 2, "outline"), (6, 9, 2, 12, "outline"),
                (24, 9, 2, 12, "outline"), (7, 21, 18, 2, "outline"),
                (8, 8, 16, 1, "glow"), (7, 10, 1, 10, "glow"),
                (24, 10, 1, 10, "glow"), (8, 21, 16, 1, "glow"),
                (11, 11, 3, 3, "trim"), (18, 11, 3, 3, "trim"),
                (11, 18, 3, 3, "trim"), (18, 18, 3, 3, "trim"),
                (15, 12, 2, 8, "shade"), (12, 15, 8, 2, "shade"),
                (3, 15, 2, 2, "glow"), (27, 15, 2, 2, "glow"),
                (15, 3, 2, 2, "glow"), (15, 27, 2, 2, "glow"),
            ]
            return ("aether-frame", rects)
        if variant == 2:
            rects = [
                (6, 4, 20, 3, "outline"), (7, 5, 18, 1, "metal"),
                (8, 7, 4, 18, "outline"), (20, 7, 4, 18, "outline"),
                (9, 8, 2, 16, "cloth"), (21, 8, 2, 16, "cloth"),
                (12, 10, 8, 3, "trim"), (13, 13, 6, 2, "glow"),
                (10, 25, 12, 3, "outline"), (11, 26, 10, 1, "metal"),
                (5, 11, 3, 2, "shade"), (24, 11, 3, 2, "shade"),
                (4, 17, 4, 2, "trim"), (24, 17, 4, 2, "trim"),
                (13, 18, 6, 2, "shade"), (15, 20, 2, 4, "glow"),
                (6, 3, 2, 1, "glow"), (24, 3, 2, 1, "glow"),
            ]
            return ("banner-rig", rects)
        rects = [
            (10, 4, 12, 2, "outline"), (9, 6, 2, 18, "outline"),
            (21, 6, 2, 18, "outline"), (10, 24, 12, 2, "outline"),
            (11, 5, 10, 1, "metal"), (10, 7, 1, 16, "trim"),
            (21, 7, 1, 16, "trim"), (11, 24, 10, 1, "metal"),
            (13, 8, 6, 2, "glow"), (12, 12, 8, 2, "shade"),
            (13, 16, 6, 2, "glow"), (14, 20, 4, 2, "trim"),
            (6, 9, 3, 3, "outline"), (23, 9, 3, 3, "outline"),
            (5, 10, 2, 1, "glow"), (25, 10, 2, 1, "glow"),
            (8, 27, 4, 1, "glow"), (20, 27, 4, 1, "glow"),
        ]
        return ("circuit-tabard", rects)
    rects = [
        (12, 2, 8, 2, "outline"), (11, 4, 10, 4, "outline"),
        (12, 5, 8, 2, "metal"), (13, 7, 6, 1, "glow"),
        (14, 8, 4, 2, "shade"), (8, 9, 6, 5, "outline"),
        (18, 9, 6, 5, "outline"), (9, 10, 4, 3, "metal"),
        (19, 10, 4, 3, "metal"), (10, 13, 12, 3, "outline"),
        (11, 14, 10, 2, "trim"), (9, 16, 14, 8, "outline"),
        (10, 17, 12, 6, "metal"), (11, 18, 10, 2, "shade"),
        (14, 17, 4, 7, "trim"), (7, 17, 3, 7, "outline"),
        (22, 17, 3, 7, "outline"), (8, 18, 2, 5, "shade"),
        (22, 18, 2, 5, "shade"), (10, 24, 12, 3, "outline"),
        (11, 25, 10, 1, "trim"), (11, 27, 4, 3, "outline"),
        (17, 27, 4, 3, "outline"), (12, 28, 2, 1, "shade"),
        (18, 28, 2, 1, "shade"), (13, 11, 2, 2, "glow"),
        (17, 11, 2, 2, "glow"),
    ]
    if n & 2:
        rects += [
            (7, 7, 4, 2, "outline"), (21, 7, 4, 2, "outline"),
            (8, 7, 2, 1, "trim"), (22, 7, 2, 1, "trim"),
        ]
    if n & 32:
        rects += [
            (10, 1, 2, 3, "trim"), (20, 1, 2, 3, "trim"),
            (9, 0, 3, 1, "glow"), (20, 0, 3, 1, "glow"),
        ]
    if n & 128:
        rects += [
            (5, 14, 3, 3, "outline"), (24, 14, 3, 3, "outline"),
            (6, 15, 1, 1, "glow"), (25, 15, 1, 1, "glow"),
        ]
    return ("plate-rig", rects)


def _pixel_readable_rects(
    kind: str,
    n: int,
) -> tuple[str, list[tuple[str, list[tuple[int, int, int, int, str]]]]]:
    """Large, readable pixel silhouettes.

    The card art is seen at a distance and often tilted, so recognizability
    matters more than micro-detail. These templates use broad shapes first,
    then sparse trim/highlight pixels from the item seed.
    """
    variant = n % 6
    if kind == "weapon":
        if variant == 0:
            return ("sword", [
                ("shadow", [(18, 3, 4, 22, "shadow")]),
                ("outline", [
                    (15, 1, 5, 2, "outline"), (14, 3, 7, 12, "outline"),
                    (13, 15, 9, 3, "outline"), (11, 18, 13, 3, "outline"),
                    (14, 21, 7, 3, "outline"), (15, 24, 5, 6, "outline"),
                    (14, 30, 7, 1, "outline"),
                ]),
                ("fill", [
                    (16, 2, 3, 1, "glow"), (15, 4, 5, 11, "metal"),
                    (16, 5, 2, 10, "highlight"), (14, 16, 7, 1, "trim"),
                    (12, 19, 11, 1, "trim"), (16, 22, 3, 7, "shade"),
                    (15, 30, 5, 1, "trim"),
                ]),
                ("spark", [(22, 4, 2, 2, "glow"), (9, 12, 2, 2, "trim")]),
            ])
        if variant == 1:
            return ("axe", [
                ("shadow", [(17, 5, 4, 23, "shadow")]),
                ("outline", [
                    (15, 5, 5, 24, "outline"), (8, 2, 12, 4, "outline"),
                    (6, 6, 14, 7, "outline"), (8, 13, 8, 4, "outline"),
                    (19, 8, 6, 5, "outline"), (13, 21, 10, 3, "outline"),
                    (14, 28, 7, 3, "outline"),
                ]),
                ("fill", [
                    (16, 6, 2, 22, "shade"), (9, 3, 9, 2, "metal"),
                    (7, 7, 11, 5, "metal"), (9, 13, 6, 2, "trim"),
                    (20, 9, 4, 3, "trim"), (14, 22, 8, 1, "glow"),
                    (15, 29, 5, 1, "trim"),
                ]),
                ("spark", [(5, 5, 2, 2, "glow"), (24, 14, 2, 2, "glow")]),
            ])
        if variant == 2:
            return ("hammer", [
                ("shadow", [(17, 8, 4, 21, "shadow")]),
                ("outline", [
                    (7, 4, 19, 7, "outline"), (6, 11, 6, 4, "outline"),
                    (21, 11, 6, 4, "outline"), (15, 11, 5, 17, "outline"),
                    (13, 20, 9, 4, "outline"), (14, 28, 7, 3, "outline"),
                ]),
                ("fill", [
                    (8, 5, 17, 5, "metal"), (9, 6, 5, 3, "highlight"),
                    (7, 12, 4, 2, "trim"), (22, 12, 4, 2, "trim"),
                    (16, 12, 2, 16, "shade"), (14, 21, 7, 2, "glow"),
                    (15, 29, 5, 1, "trim"),
                ]),
                ("spark", [(12, 2, 2, 2, "glow"), (27, 7, 2, 2, "trim")]),
            ])
        if variant == 3:
            return ("staff", [
                ("shadow", [(17, 4, 4, 25, "shadow")]),
                ("outline", [
                    (12, 0, 9, 7, "outline"), (10, 6, 13, 4, "outline"),
                    (14, 10, 5, 18, "outline"), (12, 18, 9, 4, "outline"),
                    (13, 28, 7, 3, "outline"),
                ]),
                ("fill", [
                    (14, 1, 5, 5, "glow"), (11, 7, 11, 2, "trim"),
                    (15, 10, 3, 18, "shade"), (13, 19, 7, 2, "metal"),
                    (14, 29, 5, 1, "trim"),
                ]),
                ("spark", [(9, 2, 2, 2, "glow"), (22, 2, 2, 2, "glow")]),
            ])
        if variant == 4:
            return ("spear", [
                ("shadow", [(18, 4, 3, 24, "shadow")]),
                ("outline", [
                    (13, 0, 7, 8, "outline"), (14, 8, 5, 18, "outline"),
                    (10, 10, 13, 4, "outline"), (13, 20, 7, 4, "outline"),
                    (13, 26, 7, 5, "outline"),
                ]),
                ("fill", [
                    (15, 1, 3, 6, "metal"), (16, 2, 1, 4, "highlight"),
                    (15, 8, 3, 18, "shade"), (11, 11, 11, 2, "trim"),
                    (14, 21, 5, 2, "glow"), (14, 27, 5, 3, "trim"),
                ]),
                ("spark", [(21, 5, 2, 2, "glow"), (8, 14, 2, 2, "trim")]),
            ])
        return ("crossbow", [
            ("shadow", [(7, 10, 20, 13, "shadow"), (14, 7, 5, 22, "shadow")]),
            ("outline", [
                (5, 8, 22, 5, "outline"), (3, 11, 9, 5, "outline"),
                (20, 11, 9, 5, "outline"), (13, 6, 7, 22, "outline"),
                (10, 17, 13, 5, "outline"), (12, 27, 9, 4, "outline"),
            ]),
            ("fill", [
                (6, 9, 20, 3, "metal"), (4, 12, 7, 3, "trim"),
                (21, 12, 7, 3, "trim"), (15, 7, 3, 20, "shade"),
                (11, 18, 11, 3, "glow"), (13, 28, 7, 2, "trim"),
            ]),
            ("spark", [(6, 6, 2, 2, "glow"), (25, 6, 2, 2, "glow")]),
        ])
    if kind == "trim":
        if variant in (0, 4):
            return ("cloak", [
                ("shadow", [(7, 5, 20, 24, "shadow")]),
                ("outline", [
                    (7, 3, 18, 4, "outline"), (5, 7, 7, 19, "outline"),
                    (20, 7, 7, 19, "outline"), (8, 25, 16, 5, "outline"),
                    (12, 10, 8, 6, "outline"),
                ]),
                ("fill", [
                    (8, 4, 16, 2, "trim"), (7, 8, 4, 17, "cloth"),
                    (21, 8, 4, 17, "cloth"), (9, 26, 14, 3, "trim"),
                    (13, 11, 6, 4, "glow"), (14, 17, 4, 5, "shade"),
                ]),
                ("spark", [(4, 24, 2, 2, "glow"), (26, 24, 2, 2, "glow")]),
            ])
        if variant == 1:
            return ("rune-frame", [
                ("shadow", [(6, 6, 21, 21, "shadow")]),
                ("outline", [
                    (6, 6, 20, 4, "outline"), (6, 10, 4, 16, "outline"),
                    (22, 10, 4, 16, "outline"), (6, 24, 20, 4, "outline"),
                    (12, 12, 8, 8, "outline"),
                ]),
                ("fill", [
                    (8, 7, 16, 2, "glow"), (7, 11, 2, 13, "glow"),
                    (23, 11, 2, 13, "glow"), (8, 25, 16, 2, "glow"),
                    (13, 13, 6, 6, "trim"), (15, 11, 2, 10, "shade"),
                ]),
                ("spark", [(3, 15, 2, 2, "glow"), (27, 15, 2, 2, "glow")]),
            ])
        if variant == 2:
            return ("banner", [
                ("shadow", [(8, 5, 18, 24, "shadow")]),
                ("outline", [
                    (6, 3, 21, 5, "outline"), (8, 8, 6, 18, "outline"),
                    (19, 8, 6, 18, "outline"), (10, 24, 13, 5, "outline"),
                    (12, 11, 9, 5, "outline"),
                ]),
                ("fill", [
                    (8, 4, 17, 3, "metal"), (10, 9, 3, 15, "cloth"),
                    (20, 9, 3, 15, "cloth"), (11, 25, 11, 3, "trim"),
                    (13, 12, 7, 3, "glow"), (14, 17, 5, 4, "shade"),
                ]),
                ("spark", [(5, 10, 2, 2, "trim"), (26, 11, 2, 2, "trim")]),
            ])
        return ("tabard", [
            ("shadow", [(9, 5, 17, 23, "shadow")]),
            ("outline", [
                (9, 4, 14, 4, "outline"), (8, 8, 5, 18, "outline"),
                (19, 8, 5, 18, "outline"), (10, 24, 12, 4, "outline"),
                (12, 11, 8, 3, "outline"), (12, 18, 8, 3, "outline"),
            ]),
            ("fill", [
                (11, 5, 10, 2, "metal"), (10, 9, 2, 15, "trim"),
                (20, 9, 2, 15, "trim"), (11, 25, 10, 2, "metal"),
                (13, 12, 6, 1, "glow"), (13, 19, 6, 1, "glow"),
                (14, 15, 4, 2, "shade"),
            ]),
            ("spark", [(6, 7, 2, 2, "glow"), (24, 22, 2, 2, "glow")]),
        ])
    if variant == 0:
        return ("helm-plate", [
            ("shadow", [(9, 5, 17, 23, "shadow")]),
            ("outline", [
                (11, 2, 10, 3, "outline"), (9, 5, 14, 6, "outline"),
                (7, 9, 6, 6, "outline"), (19, 9, 6, 6, "outline"),
                (8, 15, 16, 10, "outline"), (10, 25, 12, 5, "outline"),
                (12, 11, 8, 3, "outline"), (13, 18, 6, 5, "outline"),
            ]),
            ("fill", [
                (12, 3, 8, 1, "metal"), (10, 6, 12, 4, "metal"),
                (12, 8, 8, 2, "highlight"), (8, 10, 4, 4, "trim"),
                (20, 10, 4, 4, "trim"), (10, 16, 12, 8, "metal"),
                (11, 17, 10, 2, "highlight"), (14, 18, 4, 5, "shade"),
                (11, 26, 10, 3, "trim"), (14, 12, 4, 1, "glow"),
                (7, 15, 3, 2, "shade"), (22, 15, 3, 2, "shade"),
            ]),
            ("spark", [(6, 6, 2, 2, "glow"), (24, 6, 2, 2, "glow")]),
        ])
    if variant == 1:
        return ("tower-shield", [
            ("shadow", [(8, 4, 18, 25, "shadow")]),
            ("outline", [
                (9, 2, 15, 4, "outline"), (7, 6, 19, 5, "outline"),
                (6, 11, 21, 13, "outline"), (8, 24, 17, 5, "outline"),
                (11, 29, 11, 2, "outline"), (12, 9, 8, 4, "outline"),
                (13, 16, 6, 5, "outline"),
            ]),
            ("fill", [
                (10, 3, 13, 2, "metal"), (8, 7, 17, 3, "metal"),
                (9, 12, 7, 11, "metal"), (17, 12, 8, 11, "shade"),
                (10, 25, 13, 3, "trim"), (13, 30, 7, 1, "trim"),
                (13, 10, 6, 2, "glow"), (14, 17, 4, 3, "highlight"),
                (6, 13, 2, 8, "trim"), (25, 13, 2, 8, "trim"),
            ]),
            ("spark", [(5, 8, 2, 2, "glow"), (25, 7, 2, 2, "glow")]),
        ])
    if variant == 2:
        return ("cloak-mail", [
            ("shadow", [(6, 5, 21, 24, "shadow")]),
            ("outline", [
                (10, 2, 12, 4, "outline"), (7, 6, 18, 5, "outline"),
                (5, 10, 7, 17, "outline"), (20, 10, 7, 17, "outline"),
                (9, 12, 14, 13, "outline"), (10, 25, 12, 5, "outline"),
                (12, 8, 8, 3, "outline"), (13, 16, 6, 5, "outline"),
            ]),
            ("fill", [
                (11, 3, 10, 2, "metal"), (8, 7, 16, 3, "cloth"),
                (7, 11, 4, 15, "cloth"), (21, 11, 4, 15, "cloth"),
                (10, 13, 12, 11, "metal"), (11, 26, 10, 3, "trim"),
                (13, 9, 6, 1, "glow"), (14, 17, 4, 3, "shade"),
                (11, 15, 10, 1, "highlight"), (14, 21, 4, 2, "glow"),
            ]),
            ("spark", [(5, 24, 2, 2, "glow"), (25, 24, 2, 2, "glow")]),
        ])
    if variant == 3:
        return ("cog-cuirass", [
            ("shadow", [(9, 4, 17, 25, "shadow")]),
            ("outline", [
                (11, 1, 10, 5, "outline"), (8, 6, 17, 6, "outline"),
                (6, 11, 7, 7, "outline"), (20, 11, 7, 7, "outline"),
                (9, 17, 15, 8, "outline"), (10, 25, 13, 5, "outline"),
                (13, 12, 7, 4, "outline"), (13, 19, 7, 4, "outline"),
            ]),
            ("fill", [
                (12, 2, 8, 3, "metal"), (9, 7, 15, 4, "metal"),
                (7, 12, 5, 5, "trim"), (21, 12, 5, 5, "trim"),
                (10, 18, 13, 6, "metal"), (11, 26, 11, 3, "shade"),
                (14, 13, 5, 2, "glow"), (14, 20, 5, 2, "glow"),
                (11, 18, 3, 3, "highlight"), (19, 18, 3, 3, "shade"),
            ]),
            ("spark", [(7, 5, 2, 2, "glow"), (24, 5, 2, 2, "trim")]),
        ])
    if variant == 4:
        return ("sentinel-helm", [
            ("shadow", [(10, 3, 16, 26, "shadow")]),
            ("outline", [
                (9, 1, 14, 5, "outline"), (7, 6, 18, 7, "outline"),
                (8, 13, 16, 5, "outline"), (7, 18, 18, 7, "outline"),
                (10, 25, 12, 5, "outline"), (5, 11, 5, 4, "outline"),
                (22, 11, 5, 4, "outline"), (13, 15, 6, 4, "outline"),
            ]),
            ("fill", [
                (10, 2, 12, 3, "metal"), (8, 7, 16, 5, "metal"),
                (9, 14, 14, 3, "trim"), (9, 19, 15, 5, "metal"),
                (11, 26, 10, 3, "trim"), (6, 12, 3, 2, "shade"),
                (23, 12, 3, 2, "shade"), (13, 8, 6, 2, "highlight"),
                (14, 16, 4, 2, "glow"), (14, 21, 4, 2, "shade"),
            ]),
            ("spark", [(8, 0, 2, 2, "glow"), (22, 0, 2, 2, "glow")]),
        ])
    return ("runic-mail", [
        ("shadow", [(8, 5, 19, 24, "shadow")]),
        ("outline", [
            (11, 2, 10, 4, "outline"), (8, 6, 17, 6, "outline"),
            (6, 12, 8, 7, "outline"), (18, 12, 8, 7, "outline"),
            (9, 17, 15, 9, "outline"), (10, 26, 12, 4, "outline"),
            (12, 9, 8, 3, "outline"), (12, 20, 8, 3, "outline"),
        ]),
        ("fill", [
            (12, 3, 8, 2, "trim"), (9, 7, 15, 4, "metal"),
            (8, 13, 5, 5, "metal"), (19, 13, 5, 5, "metal"),
            (10, 18, 13, 7, "shade"), (11, 27, 10, 2, "trim"),
            (13, 10, 6, 1, "glow"), (13, 21, 6, 1, "glow"),
            (14, 15, 4, 2, "highlight"), (15, 18, 2, 6, "glow"),
        ]),
        ("spark", [(5, 15, 2, 2, "glow"), (25, 15, 2, 2, "trim")]),
    ])


def _render_pixel_art(kind: str, seed: str, label: str) -> str:
    n = _pixel_seed(kind, seed)
    palette = _pixel_palette(n)
    palette = {
        **palette,
        "highlight": "#fff1b6",
        "shadow": "rgba(0,0,0,0.36)",
    }
    variant, groups = _pixel_readable_rects(kind, n)
    cells: list[str] = []
    for layer, rects in groups:
        cells.append(f'<g class="px-layer px-{e(layer)}">')
        for x, y, w, h, color in rects:
            cells.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                f'fill="{palette[color]}"/>'
            )
        cells.append('</g>')
    spark_x = n % 24 + 4
    spark_y = (n >> 5) % 24 + 3
    cells.append(
        '<g class="px-layer px-seed">'
        f'<rect x="{spark_x}" y="{spark_y}" width="2" height="2" fill="{palette["glow"]}"/>'
        f'<rect x="{31 - spark_x}" y="{max(2, spark_y - 2)}" width="2" height="2" fill="{palette["trim"]}"/>'
        f'<rect x="{max(2, spark_x - 3)}" y="{min(28, spark_y + 5)}" width="1" height="1" fill="{palette["highlight"]}"/>'
        f'<rect x="{min(29, spark_x + 5)}" y="{max(2, spark_y - 5)}" width="1" height="1" fill="{palette["glow"]}"/>'
        f'<rect x="{max(2, 29 - spark_x)}" y="{min(29, spark_y + 1)}" width="1" height="1" fill="{palette["trim"]}"/>'
        '</g>'
    )
    return (
        f'<svg class="ara-pixel-art ara-pixel-art-{e(kind)}" '
        f'data-kit-kind="{e(kind)}" data-pixel-quality="32" '
        'data-pixel-style="readable-silhouette" '
        f'data-pixel-variant="{e(variant)}" '
        f'aria-label="{e(label)} {e(variant)} pixel visual" '
        'role="img" viewBox="0 0 32 32" shape-rendering="crispEdges">'
        + "".join(cells) +
        '</svg>'
    )


def _render_pixel_loadout_agent(
    armor_seed: str,
    weapon_seed: str,
    trim_seed: str,
    temper_seed: str,
    label: str,
) -> str:
    armor_palette = _pixel_palette(_pixel_seed("armor", armor_seed))
    weapon_palette = _pixel_palette(_pixel_seed("weapon", weapon_seed))
    trim_palette = _pixel_palette(_pixel_seed("trim", trim_seed))
    temper_palette = _pixel_palette(_pixel_seed("temper", temper_seed))
    armor_n = _pixel_seed("armor-agent", armor_seed)
    weapon_n = _pixel_seed("weapon-agent", weapon_seed)
    trim_n = _pixel_seed("trim-overlay", trim_seed)
    temper_n = _pixel_seed("temper-overlay", temper_seed)
    trim_y = 24 + (trim_n % 8)
    trim_x = 18 + ((trim_n >> 3) % 6)
    temper_x = 24 + (temper_n % 12)
    temper_y = 12 + ((temper_n >> 4) % 10)
    trim_enabled = trim_seed and not trim_seed.lower().startswith("no trim")
    temper_enabled = temper_seed and not temper_seed.lower().startswith("no temper")

    weapon_head = [
        # hammer / axe-like
        '<rect x="45" y="9" width="13" height="6"/>'
        '<rect x="48" y="6" width="7" height="3"/>',
        # sword-like
        '<rect x="49" y="6" width="5" height="19"/>'
        '<rect x="47" y="9" width="9" height="3"/>',
        # spear-like
        '<rect x="49" y="5" width="5" height="7"/>'
        '<rect x="48" y="12" width="7" height="4"/>',
        # wrench/mace-like
        '<rect x="47" y="7" width="10" height="4"/>'
        '<rect x="51" y="4" width="5" height="8"/>',
    ][weapon_n % 4]
    armor_sigil_x = 27 + (armor_n % 5)
    armor_sigil_y = 24 + ((armor_n >> 4) % 7)
    trim_sash = (
        f'<g class="px-agent-trim-ribbons" data-loadout-kind="trim" '
        f'fill="{trim_palette["trim"]}">'
        f'<rect x="{14 + (trim_n % 5)}" y="19" width="5" height="34"/>'
        f'<rect x="{37 + ((trim_n >> 3) % 5)}" y="19" width="5" height="34"/>'
        f'<rect x="{trim_x}" y="{trim_y}" width="28" height="2"/>'
        f'<rect x="{trim_x + 3}" y="{trim_y + 5}" width="18" height="2"/>'
        f'<rect x="{52 - trim_x}" y="{trim_y + 1}" width="3" height="8"/>'
        '</g>'
        if trim_enabled else
        '<g class="px-agent-trim-ribbons" data-loadout-kind="trim" data-empty="true"></g>'
    )
    temper_runes = (
        f'<g class="px-agent-temper-runes" data-loadout-kind="temper" '
        f'fill="{temper_palette["glow"]}">'
        f'<rect x="{temper_x}" y="{temper_y}" width="4" height="4"/>'
        f'<rect x="{temper_x + 9}" y="{temper_y + 7}" width="3" height="3"/>'
        f'<rect x="{temper_x - 4}" y="{temper_y + 17}" width="3" height="3"/>'
        f'<rect x="{18 + ((temper_n >> 6) % 7)}" y="44" width="3" height="3"/>'
        f'<rect x="{39 + ((temper_n >> 9) % 7)}" y="32" width="3" height="3"/>'
        '</g>'
        if temper_enabled else
        '<g class="px-agent-temper-runes" data-loadout-kind="temper" data-empty="true"></g>'
    )
    trim_cloak = (
        f'<g class="px-agent-trim" data-loadout-kind="trim" fill="{trim_palette["cloth"]}">'
        '<rect x="16" y="13" width="32" height="5"/>'
        '<rect x="12" y="18" width="10" height="34"/>'
        '<rect x="42" y="18" width="10" height="34"/>'
        '<rect x="19" y="50" width="26" height="6"/>'
        '</g>'
        f'<g class="px-agent-trim-outline" data-loadout-kind="trim" fill="{trim_palette["outline"]}">'
        '<rect x="14" y="11" width="36" height="3"/>'
        '<rect x="10" y="16" width="5" height="37"/>'
        '<rect x="49" y="16" width="5" height="37"/>'
        '<rect x="17" y="55" width="30" height="3"/>'
        '</g>'
        if trim_enabled else
        '<g class="px-agent-trim" data-loadout-kind="trim" data-empty="true"></g>'
        '<g class="px-agent-trim-outline" data-loadout-kind="trim" data-empty="true"></g>'
    )

    return (
        '<svg class="ara-pixel-loadout-agent" data-loadout-style="paper-doll" '
        f'data-active-armor="{e(armor_seed)}" data-active-weapon="{e(weapon_seed)}" '
        f'data-trim-seed="{e(trim_seed)}" data-temper-seed="{e(temper_seed)}" '
        f'aria-label="{e(label)} wearing active ARA loadout" role="img" '
        'viewBox="0 0 64 64" shape-rendering="crispEdges">'
        '<g class="px-agent-shadow" fill="rgba(0,0,0,0.35)">'
        '<rect x="20" y="10" width="22" height="42"/>'
        '<rect x="13" y="20" width="10" height="27"/>'
        '<rect x="41" y="18" width="13" height="34"/>'
        '</g>'
        + trim_cloak +
        '<g class="px-agent-base" fill="#191c22">'
        '<rect x="24" y="7" width="16" height="12"/>'
        '<rect x="21" y="19" width="22" height="29"/>'
        '<rect x="18" y="48" width="8" height="11"/>'
        '<rect x="38" y="48" width="8" height="11"/>'
        '</g>'
        f'<g class="px-agent-armor-outline" data-loadout-kind="armor" fill="{armor_palette["outline"]}">'
        '<rect x="22" y="5" width="20" height="5"/>'
        '<rect x="20" y="10" width="24" height="12"/>'
        '<rect x="14" y="21" width="12" height="12"/>'
        '<rect x="38" y="21" width="12" height="12"/>'
        '<rect x="19" y="31" width="26" height="18"/>'
        '</g>'
        f'<g class="px-agent-armor" data-loadout-kind="armor" fill="{armor_palette["metal"]}">'
        '<rect x="24" y="6" width="16" height="3"/>'
        '<rect x="22" y="11" width="20" height="9"/>'
        '<rect x="17" y="23" width="8" height="8"/>'
        '<rect x="39" y="23" width="8" height="8"/>'
        '<rect x="22" y="33" width="20" height="14"/>'
        '</g>'
        f'<g class="px-agent-armor-trim" data-loadout-kind="armor" fill="{armor_palette["trim"]}">'
        '<rect x="25" y="14" width="14" height="2"/>'
        '<rect x="25" y="36" width="14" height="3"/>'
        '<rect x="30" y="40" width="4" height="7"/>'
        '</g>'
        f'<g class="px-agent-armor-sigil" data-loadout-kind="armor" fill="{armor_palette["glow"]}">'
        f'<rect x="{armor_sigil_x}" y="{armor_sigil_y}" width="3" height="3"/>'
        f'<rect x="{armor_sigil_x + 5}" y="{armor_sigil_y + 3}" width="3" height="3"/>'
        f'<rect x="{armor_sigil_x - 3}" y="{armor_sigil_y + 8}" width="3" height="3"/>'
        '</g>'
        + trim_sash +
        '<g class="px-agent-trim-overlay" data-loadout-kind="trim"></g>'
        + temper_runes +
        '<g class="px-agent-temper-overlay" data-loadout-kind="temper"></g>'
        f'<g class="px-agent-glow" fill="{armor_palette["glow"]}">'
        '<rect x="27" y="17" width="4" height="2"/>'
        '<rect x="34" y="17" width="4" height="2"/>'
        '<rect x="28" y="28" width="8" height="2"/>'
        '</g>'
        f'<g class="px-agent-weapon-outline" data-loadout-kind="weapon" fill="{weapon_palette["outline"]}">'
        '<rect x="48" y="8" width="7" height="34"/>'
        '<rect x="44" y="25" width="15" height="5"/>'
        '<rect x="47" y="42" width="9" height="14"/>'
        '</g>'
        f'<g class="px-agent-weapon" data-loadout-kind="weapon" fill="{weapon_palette["metal"]}">'
        '<rect x="50" y="10" width="3" height="31"/>'
        '<rect x="45" y="26" width="13" height="2"/>'
        '<rect x="49" y="44" width="5" height="10"/>'
        '</g>'
        f'<g class="px-agent-weapon-head" data-loadout-kind="weapon" fill="{weapon_palette["metal"]}">'
        f'{weapon_head}'
        '</g>'
        f'<g class="px-agent-weapon-trim" data-loadout-kind="weapon" fill="{weapon_palette["trim"]}">'
        '<rect x="47" y="22" width="9" height="3"/>'
        '<rect x="48" y="55" width="8" height="2"/>'
        '</g>'
        '</svg>'
    )


def _render_loadout_card(
    kind: str,
    title: str,
    value: str,
    detail: str,
    href: str | None,
    token: str,
    *,
    glyph: str,
    meta_rows: list[tuple[str, str]] | None = None,
) -> str:
    tag = "a" if href else "div"
    href_attr = f' href="{href}?t={e(token)}"' if href else ""
    rarity = {
        "weapon": "rare",
        "trim": "uncommon",
    }.get(kind, "common")
    stars = {
        "weapon": "✦ ✦ ✦",
        "trim": "✦ ✦",
    }.get(kind, "✦")
    rows = "".join(
        '<div class="bc-stat-box">'
        f'<div class="bc-stat-value">{e(val)}</div>'
        f'<div class="bc-stat-label">{e(label)}</div>'
        '</div>'
        for label, val in (meta_rows or [])
    )
    return (
        f'<{tag} class="bc-card bc-rarity-{rarity} ara-equipment-card '
        f'ara-equipment-card-{e(kind)}"{href_attr}>'
        '<div class="bc-card-inner">'
        '<div class="bc-holo-shimmer"></div>'
        '<div class="bc-rarity-banner">'
        f'<div class="bc-rarity-stars">{stars}</div>'
        f'<div class="bc-rarity-label">{e(title)}</div>'
        '</div>'
        '<div class="bc-card-art">'
        f'{_render_pixel_art(kind, value, value)}'
        f'<div class="bc-sigil ara-equipment-glyph">{e(glyph)}</div>'
        '<div class="bc-name-plate">'
        f'<h2>{e(value)}</h2>'
        f'<div class="bc-level">{e(detail[:44])}</div>'
        '</div>'
        '</div>'
        f'<div class="bc-stats-row">{rows}</div>'
        '</div>'
        f'</{tag}>'
    )


def _render_creation_preview(
    kind: str,
    title: str,
    seed: str,
    detail: str,
    *,
    glyph: str,
    rows: list[tuple[str, str]] | None = None,
) -> str:
    stats = "".join(
        '<div><dt>{}</dt><dd>{}</dd></div>'.format(e(label), e(value))
        for label, value in (rows or [])
    )
    return (
        f'<section class="ara-creation-preview ara-creation-preview-{e(kind)}">'
        '<div class="ara-creation-preview-art">'
        f'{_render_pixel_art(kind, seed, title)}'
        f'<span aria-hidden="true">{e(glyph)}</span>'
        '</div>'
        '<div class="ara-creation-preview-copy">'
        f'<div class="ara-eyebrow">{e(title)}</div>'
        f'<h3>{e(seed)}</h3>'
        f'<p>{e(detail)}</p>'
        f'<dl>{stats}</dl>'
        '</div>'
        '</section>'
    )


_PIXEL_GALLERY_SEEDS = {
    "armor": [
        "armor-gallery-0", "armor-gallery-1", "armor-gallery-2",
        "armor-gallery-4", "armor-gallery-11", "armor-gallery-14",
        "hermes-default", "brass-cuirass",
    ],
    "weapon": [
        "weapon-gallery-0", "weapon-gallery-1", "weapon-gallery-2",
        "weapon-gallery-3", "weapon-gallery-4", "weapon-gallery-5",
        "blacksmith-hammer", "flocker",
    ],
    "trim": [
        "trim-gallery-0", "trim-gallery-1", "trim-gallery-4",
        "trim-gallery-9", "acme-engagement", "ward-cloak",
        "signal-tabard", "circuit-tabard",
    ],
}


def render_pixel_gallery(token: str) -> str:
    labels = {
        "armor": ("Armor", "Defensive shell generated from armor identity."),
        "weapon": ("Weapon", "Forge output generated from weapon identity."),
        "trim": ("Trim", "Task overlay generated from trim identity."),
    }
    parts = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section class="pixel-gallery">',
        '<div class="ara-eyebrow">Generator QA</div>',
        '<h2 class="ara-h">Pixel Loadout Gallery</h2>',
        '<p class="ara-sub">Seeded armor, weapon, and trim identities rendered '
        'through the same deterministic card art used by live ARA pages.</p>',
        '<div class="pixel-gallery-links">',
        f'<a class="ara-btn ara-btn-ghost" href="/armors?t={e(token)}">Armor</a>',
        f'<a class="ara-btn ara-btn-ghost" href="/weapons?t={e(token)}">Weapons</a>',
        f'<a class="ara-btn ara-btn-ghost" href="/trims?t={e(token)}">Trims</a>',
        '</div>',
    ]
    for kind, seeds in _PIXEL_GALLERY_SEEDS.items():
        title, detail = labels[kind]
        parts.append(
            f'<section class="pixel-gallery-section" data-pixel-gallery-kind="{e(kind)}">'
            f'<div class="pixel-gallery-section-head"><h3>{e(title)}</h3>'
            f'<p>{e(detail)}</p></div>'
            '<div class="pixel-gallery-grid">'
        )
        for seed in seeds:
            card = _render_loadout_card(
                kind,
                title,
                seed,
                detail,
                None,
                token,
                glyph=title[:1],
                meta_rows=[("seed", seed[:12]), ("kind", kind)],
            )
            parts.append(f'<article class="pixel-gallery-card">{card}</article>')
        parts.append('</div></section>')
    parts.append('</section>')
    return _page("Pixel Gallery", "pixel-gallery", "".join(parts), token)


def _active_trim_for_loadout(state_root: Path, armor_name: str) -> tuple[str, str, str | None, list[tuple[str, str]]]:
    trim_id = trims_mod.active_trim_id(state_root, armor_name)
    if not trim_id:
        return (
            "No trim active",
            "Task overlay not applied.",
            "/trims",
            [("state", "standby")],
        )
    meta = trims_mod.load_trim(state_root, armor_name, trim_id)
    if meta is None:
        return (
            trim_id,
            "Active trim pointer exists, but metadata is missing.",
            "/trims",
            [("state", "missing")],
        )
    detail = meta.target or meta.engagement_id or "Task scope applied."
    rows = [
        ("env", meta.env or "nonprod"),
        ("depth", meta.depth or "recon"),
    ]
    return (
        trim_id,
        detail,
        f"/trims/{e(armor_name)}/{e(trim_id)}",
        rows,
    )


def _equipped_weapon_for_loadout(state_root: Path) -> tuple[str, str, str | None, list[tuple[str, str]]]:
    weapon_id = weapons_mod.equipped_weapon_id(state_root)
    if not weapon_id:
        return (
            "No weapon equipped",
            "Forge output is not bound to this run.",
            "/weapons",
            [("state", "standby")],
        )
    exists = weapons_mod.weapon_exists(state_root, weapon_id)
    detail = (
        "Sealed weapon policy is equipped."
        if exists else
        "Equipped pointer exists, but seal.json is missing."
    )
    rows = [
        ("seal", "sealed" if exists else "missing"),
        ("source", "blacksmith"),
    ]
    return (
        weapon_id,
        detail,
        f"/weapons/{e(weapon_id)}" if exists else "/weapons",
        rows,
    )


def _active_temper_for_loadout(state_root: Path, armor_name: str) -> tuple[str, str, str | None, list[tuple[str, str]]]:
    proposal_id = temper_mod.active_temper_id(state_root, armor_name)
    if not proposal_id:
        return (
            "No Temper active",
            "No sealed Temper selected for this task.",
            "/temper",
            [("state", "standby")],
        )
    proposal = temper_mod.load_proposal(state_root, armor_name, proposal_id)
    if proposal is None:
        return (
            proposal_id,
            "Active Temper pointer exists, but proposal is missing.",
            "/temper",
            [("state", "missing")],
        )
    return (
        proposal_id,
        f"{len(proposal.items)} sealed controls ready.",
        f"/temper/{e(armor_name)}/{e(proposal_id)}",
        [
            ("status", proposal.status),
            ("items", str(len(proposal.items))),
        ],
    )


def _render_loadout_stage(armor, state_root: Path, token: str) -> str:
    weapon_name, weapon_detail, weapon_href, weapon_rows = (
        _equipped_weapon_for_loadout(state_root)
    )
    trim_name, trim_detail, trim_href, trim_rows = (
        _active_trim_for_loadout(state_root, armor.name)
    )
    temper_name, temper_detail, temper_href, temper_rows = (
        _active_temper_for_loadout(state_root, armor.name)
    )
    return (
        '<section class="ara-loadout-stage" aria-label="Active ARA loadout">'
        '<div class="ara-loadout-figure">'
        '<div class="ara-agent-sprite" aria-label="Agent wearing active ARA loadout" role="img">'
        f'{_render_pixel_loadout_agent(armor.name, weapon_name, trim_name, temper_name, armor.name)}'
        '</div>'
        '<div class="ara-loadout-caption">'
        '<span class="ara-eyebrow">Active Loadout</span>'
        f'<strong>{e(armor.name)}</strong>'
        '<div class="ara-gateway-row">'
        '<span>Gateways:</span>'
        f'<span>{_render_channel_pills(armor.meta.channels)}</span>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="ara-loadout-hand"><div class="ara-kit-grid">'
        + _render_armor_card(armor, token, hero=False)
        + _render_loadout_card(
            "weapon",
            "Weapon",
            weapon_name,
            weapon_detail,
            weapon_href,
            token,
            glyph="W",
            meta_rows=weapon_rows,
        )
        + _render_loadout_card(
            "trim",
            "Trim",
            trim_name,
            trim_detail,
            trim_href,
            token,
            glyph="T",
            meta_rows=trim_rows,
        )
        + _render_loadout_card(
            "temper",
            "Temper",
            temper_name,
            temper_detail,
            temper_href,
            token,
            glyph="T",
            meta_rows=temper_rows,
        )
        + '</div></div>'
        '</section>'
    )


def _count_md_sections(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip().startswith("#"))


def _render_armor_info_card(
    title: str,
    value: str,
    detail: str,
    rows: list[tuple[str, str]],
    *,
    glyph: str,
) -> str:
    return (
        '<article class="ara-armor-info-card">'
        '<div class="ara-armor-info-head">'
        f'<span>{e(title)}</span><b aria-hidden="true">{e(glyph)}</b>'
        '</div>'
        f'<h3>{e(value)}</h3>'
        f'<p>{e(detail)}</p>'
        '<dl>'
        + "".join(
            f'<div><dt>{e(k)}</dt><dd>{v}</dd></div>'
            for k, v in rows
        )
        + '</dl>'
        '</article>'
    )


def _armor_detail_context(state_root: Path, a):
    walls_text = a.walls_path.read_text() if a.walls_path.exists() else ""
    absolute_text = a.absolute_path.read_text() if a.absolute_path.exists() else ""
    armor_store = overrides_mod.load_at(a.overrides_path)
    parsed_walls: Walls | None = None
    parse_error: str | None = None
    if a.walls_path.exists():
        try:
            parsed_walls = parse_file(a.walls_path)
        except Exception as _exc:  # noqa: BLE001
            parse_error = str(_exc)
    pulse_map = _rule_pulse(_load_policy_hits(state_root))
    sigil_options = "".join(
        f'<option value="{e(s)}"{" selected" if s == a.meta.sigil else ""}>{e(s)}</option>'
        for s in _SIGIL_OPTIONS
    )
    return {
        "walls_text": walls_text,
        "absolute_text": absolute_text,
        "armor_store": armor_store,
        "parsed_walls": parsed_walls,
        "parse_error": parse_error,
        "pulse_map": pulse_map,
        "sigil_options": sigil_options,
    }


def _armor_detail_fragments(
    state_root: Path,
    a,
    token: str,
    *,
    absolute_edit: bool = False,
    walls_edit: bool = False,
) -> dict[str, str]:
    ctx = _armor_detail_context(state_root, a)
    walls_text = ctx["walls_text"]
    absolute_text = ctx["absolute_text"]
    armor_store = ctx["armor_store"]
    parsed_walls = ctx["parsed_walls"]
    parse_error = ctx["parse_error"]
    pulse_map = ctx["pulse_map"]
    sigil_options = ctx["sigil_options"]

    identity_html = (
        '<dl class="ara-dl">'
        f'<dt>tier</dt><dd>{_TIER_ROMAN[a.stats.tier]} ({_TIER_LABEL[a.stats.tier]})</dd>'
        f'<dt>walls</dt><dd>{a.stats.walls}</dd>'
        f'<dt>commands</dt><dd>{a.stats.commands}</dd>'
        f'<dt>approvals</dt><dd>{a.stats.approvals}</dd>'
        f'<dt>overrides</dt><dd>{a.stats.overrides}</dd>'
        f'<dt>blackholes</dt><dd>{a.stats.blackholes}</dd>'
        f'<dt>root</dt><dd class="mono" style="font-size:12px">{e(a.root)}</dd>'
        f'<dt>mode</dt><dd>{"SHADOW (dry-run)" if a.meta.shadow_mode else "ENFORCING"}</dd>'
        '</dl>'
        + '<h3 class="ara-eyebrow" style="margin-top:22px">Metadata</h3>'
        + f'<form class="ara-form" method="post" action="/armors/{e(a.name)}/meta?t={e(token)}">'
        f'<label>description<input type="text" name="description" value="{e(a.meta.description)}" size="40"></label>'
        f'<label>sigil<select name="sigil">{sigil_options}</select></label>'
        f'<label>tags<input type="text" name="tags" value="{e(",".join(a.meta.tags))}" placeholder="comma,separated"></label>'
        f'<label class="ara-checkbox">'
        f'<input type="checkbox" name="shadow_mode" value="1"{" checked" if a.meta.shadow_mode else ""}>'
        f' shadow-mode (dry-run: denies become allow+audit)'
        f'</label>'
        '<button class="ara-btn ara-btn-primary" type="submit">Save</button>'
        '</form>'
        + '<h3 class="ara-eyebrow" style="margin-top:22px">Channel scope</h3>'
        + '<p class="ara-sub">Which chat / messaging channels this armor '
        'applies to. Leave all unchecked for the default — applies '
        'globally to every channel. When scoped, the MITM detects the '
        'channel per request and only fires the equipped armor\'s '
        'ABSOLUTE (and any armor-scoped policy) when the detected '
        'channel matches. Each transcript turn is tagged with the '
        'detected channel; use the pill bar on <code>/shadows</code> '
        'and <code>/rhema</code> to filter by it.</p>'
        + f'<div style="margin-bottom:8px">{_render_channel_pills(a.meta.channels)}</div>'
        + f'<form class="ara-form" method="post" '
        f'action="/armors/{e(a.name)}/channels?t={e(token)}" '
        'style="flex-wrap:wrap;gap:10px">'
        + ''.join(
            (
                '<label class="ara-checkbox" '
                f'style="display:inline-flex;align-items:center;gap:6px;'
                f'padding:4px 8px;border-radius:6px;'
                f'border:1px solid color-mix(in srgb,{c["color"]} 35%,transparent);'
                f'background:color-mix(in srgb,{c["color"]} 10%,transparent)">'
                f'<input type="checkbox" name="channels" value="{e(c["id"])}"'
                f'{" checked" if c["id"] in a.meta.channels else ""}>'
                f'<span style="color:{c["color"]};font-family:var(--font-mono);'
                f'font-size:11px;text-transform:uppercase;letter-spacing:0.04em">'
                f'{e(c["label"])}</span>'
                '</label>'
            )
            for c in armors_mod.CHANNEL_REGISTRY
        )
        + '<button class="ara-btn ara-btn-primary" type="submit">Save channels</button>'
        '</form>'
    )

    policy_html = (
        '<h3 class="ara-eyebrow">Rule breakdown</h3>'
        + _render_rules_breakdown(parsed_walls, parse_error, pulse_map, token)
        + _render_editable_file(
            heading="WALLS.md",
            header_open="details",
            summary_label="Raw WALLS.md",
            contents=walls_text,
            edit=walls_edit,
            edit_url=f"/armors/{e(a.name)}?t={e(token)}&tab=policy&walls_edit=1",
            view_url=f"/armors/{e(a.name)}?t={e(token)}&tab=policy",
            save_action=f"/armors/{e(a.name)}/walls/save?t={e(token)}",
        )
        + _render_armor_entry_block(a.name, "overrides", armor_store.overrides, token, pulse_map)
        + _render_armor_entry_block(a.name, "blackholes", armor_store.blackholes, token, pulse_map)
    )

    envelope_html = _render_editable_file(
        heading="ABSOLUTE.md",
        header_open=None,
        summary_label=None,
        contents=absolute_text,
        edit=absolute_edit,
        edit_url=f"/armors/{e(a.name)}?t={e(token)}&tab=envelope&absolute_edit=1",
        view_url=f"/armors/{e(a.name)}?t={e(token)}&tab=envelope",
        save_action=f"/armors/{e(a.name)}/absolute/save?t={e(token)}",
    )

    delete_confirm = (
        "Delete this armor set? This removes WALLS, ABSOLUTE, overrides, and meta. "
        "This action cannot be undone."
    )
    manage_html = (
        '<p class="ara-sub">Mutating actions. Clone duplicates the entire '
        'armor (WALLS + ABSOLUTE + overrides + meta) under a new name. '
        'Delete removes the armor directory irreversibly. Export wraps '
        'the armor as a portable .tgz for transfer.</p>'
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:10px">'
        f'<form class="ara-form" method="post" action="/armors/{e(a.name)}/clone?t={e(token)}" style="margin:0">'
        f'<label>clone to<input type="text" name="dst" required pattern="[a-zA-Z0-9._-]+"></label>'
        '<button class="ara-btn ara-btn-ghost" type="submit">Clone</button>'
        '</form>'
        f'<form method="post" action="/armors/{e(a.name)}/delete?t={e(token)}" style="margin:0"'
        f' onsubmit="return confirm(\'{e(delete_confirm)}\')">'
        '<button class="ara-btn ara-btn-bad" type="submit">Delete armor</button>'
        '</form>'
        f'<a class="ara-btn ara-btn-ghost" href="/armors/{e(a.name)}/export?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        'Export .tgz</a>'
        '</div>'
    )

    return {
        "identity": identity_html,
        "policy": policy_html,
        "envelope": envelope_html,
        "manage": manage_html,
    }


def _render_armor_sets_focus(state_root: Path, armor, token: str) -> str:
    ctx = _armor_detail_context(state_root, armor)
    walls_text = ctx["walls_text"]
    absolute_text = ctx["absolute_text"]
    armor_store = ctx["armor_store"]
    parsed_walls = ctx["parsed_walls"]
    parse_error = ctx["parse_error"]
    fragments = _armor_detail_fragments(state_root, armor, token)
    rule_total = armor.stats.rules
    deny_total = armor.stats.deny
    approval_total = armor.stats.approvals
    absolute_sections = _count_md_sections(absolute_text)
    policy_state = (
        "parse error"
        if parse_error else
        "loaded" if parsed_walls is not None else
        "empty"
    )
    return (
        '<section class="ara-armor-focus" aria-label="Equipped armor set">'
        '<div class="ara-armor-focus-card">'
        '<div>'
        '<div class="ara-eyebrow">Equipped armor</div>'
        + _render_armor_card(armor, token, hero=True)
        + '</div>'
        '<div class="ara-armor-focus-copy">'
        '<span class="ara-eyebrow">Active protection set</span>'
        f'<h3>{e(armor.name)}</h3>'
        f'<p>{e(armor.meta.description or "No description")}</p>'
        '<div class="ara-gateway-row">'
        '<span>Gateways:</span>'
        f'<span>{_render_channel_pills(armor.meta.channels)}</span>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="ara-armor-info-grid">'
        + _render_armor_info_card(
            "Identity",
            f"Tier {_TIER_ROMAN[armor.stats.tier]}",
            "Runtime identity, scope, mode, and location.",
            [
                ("mode", "SHADOW" if armor.meta.shadow_mode else "ENFORCING"),
                ("channels", _render_channel_pills(armor.meta.channels)),
                ("root", f'<code>{e(armor.root.name)}</code>'),
            ],
            glyph="I",
        )
        + _render_armor_info_card(
            "Policy",
            f"{rule_total} rules",
            "Bulwark/WALLS posture plus armor-scoped overrides.",
            [
                ("deny", str(deny_total)),
                ("approval", str(approval_total)),
                ("overrides", str(len(armor_store.overrides))),
                ("blackholes", str(len(armor_store.blackholes))),
                ("state", e(policy_state)),
            ],
            glyph="P",
        )
        + _render_armor_info_card(
            "Envelope",
            f"{absolute_sections} sections",
            "ABSOLUTE prompt material injected for this armor.",
            [
                ("chars", f"{len(absolute_text):,}"),
                ("file", '<code>ABSOLUTE.md</code>'),
                ("walls", f"{len(walls_text):,} chars"),
            ],
            glyph="E",
        )
        + '</div>'
        '<details class="ara-armor-detail-inline">'
        '<summary><span class="ara-eyebrow">Detail</span>'
        '<strong>View armor internals</strong></summary>'
        '<details><summary>Identity</summary>'
        f'<div>{fragments["identity"]}</div></details>'
        '<details><summary>Policy</summary>'
        f'<div>{fragments["policy"]}</div></details>'
        '<details><summary>Envelope</summary>'
        f'<div>{fragments["envelope"]}</div></details>'
        '<details><summary>Manage</summary>'
        f'<div>{fragments["manage"]}</div></details>'
        '</details>'
        '</section>'
    )


def render_armors(state_root: Path, token: str) -> str:
    armors_list = armors_mod.list_armors(state_root)
    active_name = armors_mod.active_armor_name(state_root)

    # Pull the equipped one out for the hero slot.
    hero = None
    rest: list = []
    for a in armors_list:
        if a.equipped:
            hero = a
        else:
            rest.append(a)
    if hero is None and armors_list:
        hero = armors_list[0]
        rest = armors_list[1:]

    parts = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section>',
        '<div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:8px">',
        '<div>',
        '<div class="ara-eyebrow">Loadouts</div>',
        '<h2 class="ara-h" style="margin-bottom:4px">Armor Sets</h2>',
        '</div>',
        f'<a class="ara-btn ara-btn-primary" href="/armors/new?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">+ New</a>',
        '</div>',
        '<p class="ara-sub">Named rule loadouts. Equip one to hot-swap WALLS, '
        'overrides, and envelope together.</p>',
    ]

    if not armors_list:
        parts.append(_ARMOR_EMPTY_HINT)
        if active_name:
            parts.append(
                f'<div class="ara-empty" style="margin-top:12px">'
                f'Active pointer is set to <code>{e(active_name)}</code> '
                'but that armor directory is missing.</div>'
            )
        parts.append('</section>')
        return _page("Armor Sets", "armors", "".join(parts), token)

    parts.append(_render_armor_sets_focus(state_root, hero, token))

    if rest:
        parts.append(
            '<div class="ara-eyebrow" style="margin-top:32px">Other sets</div>'
        )
        parts.append(
            '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));'
            'gap:24px;margin-top:8px">'
        )
        for a in rest:
            parts.append(_render_armor_card(a, token, hero=False))
        parts.append('</div>')

    parts.append('</section>')
    return _page("Armor Sets", "armors", "".join(parts), token)


_SIGIL_OPTIONS = ["shield", "castle", "flask", "hammer", "lock"]


def render_armor_new(token: str, *, flash: str | None = None) -> str:
    sigil_options = "".join(
        f'<option value="{e(s)}">{e(s)}</option>' for s in _SIGIL_OPTIONS
    )
    body = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section>',
        '<div class="ara-eyebrow">New</div>',
        '<h2 class="ara-h">Create Armor Set</h2>',
        '<p class="ara-sub">Empty WALLS and ABSOLUTE scaffolded; fill them in after creation.</p>',
        _render_creation_preview(
            "armor",
            "Create armor preview",
            "new-armor-preview",
            "The saved armor card is generated from the final armor name.",
            glyph="A",
            rows=[("seed", "name"), ("state", "draft")],
        ),
    ]
    if flash:
        body.append(
            '<div class="ara-empty" style="border-left:3px solid var(--error);'
            f'margin-bottom:14px">{e(flash)}</div>'
        )
    body.extend([
        f'<form class="ara-form" method="post" action="/armors/new?t={e(token)}">',
        '<label>name<input type="text" name="name" required pattern="[a-zA-Z0-9._-]+"></label>',
        '<label>description<input type="text" name="description"></label>',
        f'<label>sigil<select name="sigil">{sigil_options}</select></label>',
        '<button class="ara-btn ara-btn-primary" type="submit">Create</button>',
        f'<a class="ara-btn ara-btn-ghost" href="/armors?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Cancel</a>',
        '</form>',
        '</section>',
    ])
    return _page("New Armor", "armors", "".join(body), token)


def _render_rules_breakdown(
    walls: "Walls | None", parse_error: str | None,
    pulse: dict[str, dict] | None = None,
    token: str | None = None,
) -> str:
    if parse_error is not None:
        return (
            '<div class="ara-empty" style="border-left:3px solid var(--error)">'
            f'WALLS parse failed: {e(parse_error)}</div>'
        )
    if walls is None:
        return '<div class="ara-empty">No WALLS.md in this armor.</div>'

    approval_paths = [r for r in walls.approval_rules if r.kind == "path"]
    approval_cmds = [r for r in walls.approval_rules if r.kind == "command"]
    approval_nets = [r for r in walls.approval_rules if r.kind == "net"]
    p = pulse or {}

    def _table(heading: str, sub: str, rules: list) -> str:
        head = (
            f'<h4 class="ara-eyebrow" style="margin-top:16px">{e(heading)} '
            f'<span style="opacity:.6">· {len(rules)}</span></h4>'
            f'<p class="ara-sub" style="margin:0 0 6px 0">{e(sub)}</p>'
        )
        if not rules:
            return head + '<div class="ara-empty">(none)</div>'
        import urllib.parse as _up
        rows = "".join(
            '<tr>'
            f'<td class="mono" style="width:22ch">'
            + (
                f'<a href="/rules/{_up.quote(r.rule_id, safe="")}?t={e(token)}" '
                f'style="color:inherit">{e(r.rule_id)}</a>'
                if token else e(r.rule_id)
            )
            + '</td>'
            f'<td class="mono">{e(r.pattern)}</td>'
            f'<td class="ara-pulse-cell">{_render_pulse_cell(r.rule_id, p.get(r.rule_id), token)}</td>'
            '</tr>'
            for r in rules
        )
        return (
            head +
            '<table class="ara-table">'
            '<thead><tr><th>rule_id</th><th>pattern</th>'
            '<th>pulse (1h)</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )

    return (
        _table("deny_path", "Hard deny on matching path access.", list(walls.path_rules))
        + _table("deny_command", "Hard deny on matching command spawn.", list(walls.command_rules))
        + _table("deny_net", "Hard deny on outbound connect to matching host:port.", list(walls.net_rules))
        + _table("approval_path", "Prompts operator before allowing path access.", approval_paths)
        + _table("approval_command", "Prompts operator before spawning command.", approval_cmds)
        + _table("approval_net", "Prompts operator before outbound connect.", approval_nets)
    )


def _render_armor_entry_block(
    armor_name: str,
    kind_name: str,           # "overrides" | "blackholes"
    entries: list,
    token: str,
    pulse_map: dict[str, dict] | None = None,
) -> str:
    p = pulse_map or {}
    title = "Overrides (armor-scoped)" if kind_name == "overrides" else "Blackholes (armor-scoped)"
    subtitle = (
        "Always-allow entries that apply only while this armor is equipped."
        if kind_name == "overrides"
        else "Always-deny entries that apply only while this armor is equipped. Win over overrides."
    )
    rows: list[str] = []
    for ent in entries:
        rows.append(
            "<tr>"
            f'<td class="mono">{e(ent.id)}</td>'
            f"<td>{e(ent.kind)}</td>"
            f"<td>{e(ent.scope)}</td>"
            f"<td>{e(_fmt_ago(ent.added_at))}</td>"
            f'<td class="mono">{e(ent.pattern)}</td>'
            f"<td>{e(ent.reason or '')}</td>"
            f'<td class="ara-pulse-cell">{_render_pulse_cell(ent.id, p.get(ent.id), token)}</td>'
            "<td>"
            f'<form method="post" action="/armors/{e(armor_name)}/{e(kind_name)}/remove?t={e(token)}">'
            f'<input type="hidden" name="id" value="{e(ent.id)}">'
            '<button class="ara-btn ara-btn-ghost">remove</button>'
            "</form>"
            "</td>"
            "</tr>"
        )
    table = (
        '<div class="ara-empty">(empty)</div>'
        if not rows
        else '<table class="ara-table">'
             '<thead><tr><th>ID</th><th>Kind</th><th>Scope</th><th>Age</th>'
             '<th>Pattern</th><th>Reason</th><th>Pulse (1h)</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>'
    )
    return (
        f'<h3 class="ara-eyebrow" style="margin-top:22px">{e(title)}</h3>'
        f'<p class="ara-sub">{e(subtitle)}</p>'
        + table
        + f'<form class="ara-form" method="post" action="/armors/{e(armor_name)}/{e(kind_name)}/add?t={e(token)}">'
        '<label>kind<select name="kind">'
        '<option value="path">path</option>'
        '<option value="command">command</option>'
        '</select></label>'
        '<label>pattern<input type="text" name="pattern" required></label>'
        '<label>reason<input type="text" name="reason"></label>'
        '<button class="ara-btn ara-btn-primary" type="submit">add</button>'
        '</form>'
    )


def _render_editable_file(
    *, heading: str, contents: str, edit: bool, edit_url: str, view_url: str,
    save_action: str, header_open: str | None = None,
    summary_label: str | None = None,
) -> str:
    """Generic view/edit block for per-armor text files.

    ``header_open='details'`` wraps the view-mode block in a <details>
    disclosure using ``summary_label`` — used for WALLS where the parsed
    breakdown is the primary view and raw source is secondary.
    """
    if edit:
        return (
            f'<h3 class="ara-eyebrow" style="margin-top:22px">{e(heading)}</h3>'
            f'<form method="post" action="{save_action}">'
            '<textarea name="contents" spellcheck="false" '
            'style="width:100%;min-height:260px;padding:14px 16px;'
            'background:var(--surface-container-lowest);color:var(--on-surface);'
            'border:0;border-radius:var(--radius-md);resize:vertical;'
            'font-family:var(--font-mono);font-size:12px;line-height:1.55;'
            'box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--outline-variant) 20%,transparent);">'
            f'{e(contents)}</textarea>'
            '<div style="margin-top:10px;display:flex;gap:8px">'
            '<button class="ara-btn ara-btn-primary" type="submit">Save</button>'
            f'<a class="ara-btn ara-btn-ghost" href="{view_url}" '
            'style="text-decoration:none;display:inline-flex;align-items:center">'
            'Cancel</a>'
            '</div>'
            '<p class="ara-sub" style="margin-top:10px">'
            'Saves atomically. Supervisor hot-reload picks up WALLS changes '
            'automatically; ABSOLUTE.md is read on next turn.</p>'
            '</form>'
        )
    edit_link = (
        f'<a class="ara-btn ara-btn-ghost" href="{edit_url}" '
        'style="text-decoration:none;font-size:10px;padding:4px 10px">Edit</a>'
    )
    if header_open == "details":
        return (
            '<details style="margin-top:12px">'
            f'<summary class="ara-sub" style="cursor:pointer">'
            f'{e(summary_label or heading)} · '
            '</summary>'
            '<div style="margin-top:8px;display:flex;gap:8px;align-items:baseline">'
            + edit_link +
            '</div>'
            f'<pre class="ara-code">{e(contents)}</pre>'
            '</details>'
        )
    return (
        '<div style="margin-top:22px;display:flex;gap:8px;align-items:baseline">'
        f'<h3 class="ara-eyebrow" style="margin:0">{e(heading)}</h3>'
        + edit_link +
        '</div>'
        f'<pre class="ara-code">{e(contents) or "(empty)"}</pre>'
    )


def render_armor_detail(
    state_root: Path, name: str, token: str, *,
    flash: str | None = None, flash_level: str = "info",
    absolute_edit: bool = False, walls_edit: bool = False,
    tab: str = "identity",
) -> str | None:
    a = armors_mod.load_armor(state_root, name)
    if a is None:
        return None
    walls_text = a.walls_path.read_text() if a.walls_path.exists() else ""
    absolute_text = a.absolute_path.read_text() if a.absolute_path.exists() else ""
    armor_store = overrides_mod.load_at(a.overrides_path)
    parsed_walls: Walls | None = None
    parse_error: str | None = None
    if a.walls_path.exists():
        try:
            parsed_walls = parse_file(a.walls_path)
        except Exception as _exc:  # noqa: BLE001
            parse_error = str(_exc)
    pulse_map = _rule_pulse(_load_policy_hits(state_root))
    equip_button = (
        '<button class="ara-btn ara-btn-ghost" disabled '
        'style="opacity:0.6;cursor:default">EQUIPPED</button>'
        if a.equipped
        else '<button class="ara-btn ara-btn-primary" type="submit">EQUIP</button>'
    )
    sigil_options = "".join(
        f'<option value="{e(s)}"{" selected" if s == a.meta.sigil else ""}>{e(s)}</option>'
        for s in _SIGIL_OPTIONS
    )
    flash_html = ""
    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        flash_html = (
            '<div class="ara-empty" '
            f'style="border-left:3px solid var({cls});margin-bottom:16px">{e(flash)}</div>'
        )
    delete_confirm = (
        "Delete this armor set? This removes WALLS, ABSOLUTE, overrides, and meta. "
        "This action cannot be undone."
    )
    # Tab bar. URL-based so deep links work + back button behaves.
    tabs = [
        ("identity", "Identity"),
        ("policy", "Policy"),
        ("envelope", "Envelope"),
        ("manage", "Manage"),
    ]
    if tab not in {k for k, _ in tabs}:
        tab = "identity"

    def _tab_pill(key: str, label: str) -> str:
        active = (key == tab)
        style = (
            'background:color-mix(in srgb,var(--primary) 70%,transparent);'
            'color:#fff;border:1px solid var(--primary);'
            if active else
            'background:color-mix(in srgb,var(--outline-variant) 18%,transparent);'
            'color:var(--on-surface);border:1px solid '
            'color-mix(in srgb,var(--outline-variant) 25%,transparent);'
        )
        return (
            f'<a href="/armors/{e(a.name)}?t={e(token)}&tab={key}" '
            f'style="text-decoration:none;padding:6px 14px;font-size:12px;'
            f'border-radius:14px;font-family:var(--font-mono);'
            f'letter-spacing:0.04em;text-transform:uppercase;{style}">'
            f'{e(label)}</a>'
        )
    tab_bar = (
        '<div style="display:flex;gap:8px;margin:4px 0 16px 0;flex-wrap:wrap">'
        + "".join(_tab_pill(k, l) for k, l in tabs)
        + '</div>'
    )

    # --- Identity tab ---
    identity_html = (
        '<dl class="ara-dl">'
        f'<dt>tier</dt><dd>{_TIER_ROMAN[a.stats.tier]} ({_TIER_LABEL[a.stats.tier]})</dd>'
        f'<dt>walls</dt><dd>{a.stats.walls}</dd>'
        f'<dt>commands</dt><dd>{a.stats.commands}</dd>'
        f'<dt>approvals</dt><dd>{a.stats.approvals}</dd>'
        f'<dt>overrides</dt><dd>{a.stats.overrides}</dd>'
        f'<dt>blackholes</dt><dd>{a.stats.blackholes}</dd>'
        f'<dt>root</dt><dd class="mono" style="font-size:12px">{e(a.root)}</dd>'
        f'<dt>mode</dt><dd>{"SHADOW (dry-run)" if a.meta.shadow_mode else "ENFORCING"}</dd>'
        '</dl>'
        # Meta editor — part of Identity tab.
        + '<h3 class="ara-eyebrow" style="margin-top:22px">Metadata</h3>'
        + f'<form class="ara-form" method="post" action="/armors/{e(a.name)}/meta?t={e(token)}">'
        f'<label>description<input type="text" name="description" value="{e(a.meta.description)}" size="40"></label>'
        f'<label>sigil<select name="sigil">{sigil_options}</select></label>'
        f'<label>tags<input type="text" name="tags" value="{e(",".join(a.meta.tags))}" placeholder="comma,separated"></label>'
        f'<label class="ara-checkbox">'
        f'<input type="checkbox" name="shadow_mode" value="1"{" checked" if a.meta.shadow_mode else ""}>'
        f' shadow-mode (dry-run: denies become allow+audit)'
        f'</label>'
        '<button class="ara-btn ara-btn-primary" type="submit">Save</button>'
        '</form>'
        # Channel scoping (Phase 1: display + edit only; enforcement
        # gating lands in Phase 2 — empty list = global / applies to
        # every channel).
        + '<h3 class="ara-eyebrow" style="margin-top:22px">Channel scope</h3>'
        + '<p class="ara-sub">Which chat / messaging channels this armor '
        'applies to. Leave all unchecked for the default — applies '
        'globally to every channel. When scoped, the MITM detects the '
        'channel per request and only fires the equipped armor\'s '
        'ABSOLUTE (and any armor-scoped policy) when the detected '
        'channel matches. Each transcript turn is tagged with the '
        'detected channel; use the pill bar on <code>/shadows</code> '
        'and <code>/rhema</code> to filter by it.</p>'
        + f'<div style="margin-bottom:8px">{_render_channel_pills(a.meta.channels)}</div>'
        + f'<form class="ara-form" method="post" '
        f'action="/armors/{e(a.name)}/channels?t={e(token)}" '
        'style="flex-wrap:wrap;gap:10px">'
        + ''.join(
            (
                '<label class="ara-checkbox" '
                f'style="display:inline-flex;align-items:center;gap:6px;'
                f'padding:4px 8px;border-radius:6px;'
                f'border:1px solid color-mix(in srgb,{c["color"]} 35%,transparent);'
                f'background:color-mix(in srgb,{c["color"]} 10%,transparent)">'
                f'<input type="checkbox" name="channels" value="{e(c["id"])}"'
                f'{" checked" if c["id"] in a.meta.channels else ""}>'
                f'<span style="color:{c["color"]};font-family:var(--font-mono);'
                f'font-size:11px;text-transform:uppercase;letter-spacing:0.04em">'
                f'{e(c["label"])}</span>'
                '</label>'
            )
            for c in armors_mod.CHANNEL_REGISTRY
        )
        + '<button class="ara-btn ara-btn-primary" type="submit">Save channels</button>'
        '</form>'
    )

    # --- Policy tab: WALLS + per-armor overrides/blackholes ---
    policy_html = (
        '<h3 class="ara-eyebrow">Rule breakdown</h3>'
        + _render_rules_breakdown(parsed_walls, parse_error, pulse_map, token)
        + _render_editable_file(
            heading="WALLS.md",
            header_open="details",
            summary_label="Raw WALLS.md",
            contents=walls_text,
            edit=walls_edit,
            edit_url=f"/armors/{e(a.name)}?t={e(token)}&tab=policy&walls_edit=1",
            view_url=f"/armors/{e(a.name)}?t={e(token)}&tab=policy",
            save_action=f"/armors/{e(a.name)}/walls/save?t={e(token)}",
        )
        + _render_armor_entry_block(a.name, "overrides", armor_store.overrides, token, pulse_map)
        + _render_armor_entry_block(a.name, "blackholes", armor_store.blackholes, token, pulse_map)
    )

    # --- Envelope tab: ABSOLUTE.md ---
    envelope_html = (
        _render_editable_file(
            heading="ABSOLUTE.md",
            header_open=None,
            summary_label=None,
            contents=absolute_text,
            edit=absolute_edit,
            edit_url=f"/armors/{e(a.name)}?t={e(token)}&tab=envelope&absolute_edit=1",
            view_url=f"/armors/{e(a.name)}?t={e(token)}&tab=envelope",
            save_action=f"/armors/{e(a.name)}/absolute/save?t={e(token)}",
        )
    )

    # --- Manage tab: clone / delete / export ---
    manage_html = (
        '<p class="ara-sub">Mutating actions. Clone duplicates the entire '
        'armor (WALLS + ABSOLUTE + overrides + meta) under a new name. '
        'Delete removes the armor directory irreversibly. Export wraps '
        'the armor as a portable .tgz for transfer.</p>'
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:10px">'
        f'<form class="ara-form" method="post" action="/armors/{e(a.name)}/clone?t={e(token)}" style="margin:0">'
        f'<label>clone to<input type="text" name="dst" required pattern="[a-zA-Z0-9._-]+"></label>'
        '<button class="ara-btn ara-btn-ghost" type="submit">Clone</button>'
        '</form>'
        f'<form method="post" action="/armors/{e(a.name)}/delete?t={e(token)}" style="margin:0"'
        f' onsubmit="return confirm(\'{e(delete_confirm)}\')">'
        '<button class="ara-btn ara-btn-bad" type="submit">Delete armor</button>'
        '</form>'
        f'<a class="ara-btn ara-btn-ghost" href="/armors/{e(a.name)}/export?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">'
        'Export .tgz</a>'
        '</div>'
    )

    tab_content = {
        "identity": identity_html,
        "policy": policy_html,
        "envelope": envelope_html,
        "manage": manage_html,
    }[tab]

    body = (
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">'
        '<section class="ara-armor-focus ara-armor-detail-focus" aria-label="Armor visual identity">'
        '<div class="ara-armor-focus-card">'
        '<div>'
        '<div class="ara-eyebrow">Armor identity</div>'
        + _render_armor_card(a, token, hero=True)
        + '</div>'
        '<div class="ara-armor-focus-copy">'
        '<span class="ara-eyebrow">Manage protection set</span>'
        f'<h3>{e(a.name)}</h3>'
        f'<p>{e(a.meta.description or "No description")}</p>'
        '<div class="ara-gateway-row" style="justify-content:flex-start">'
        '<span>Gateways:</span>'
        f'<span>{_render_channel_pills(a.meta.channels)}</span>'
        '</div>'
        '</div>'
        '</div>'
        '</section>'
        '<section>'
        '<div class="ara-eyebrow">Armor Set</div>'
        f'<h2 class="ara-h">{e(a.name)}</h2>'
        f'<p class="ara-sub">{e(a.meta.description or "no description")}</p>'
        + flash_html
        # Top action row — equip + back (shown on every tab)
        + f'<form method="post" action="/armors/{e(a.name)}/equip?t={e(token)}" '
        'style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">'
        + equip_button
        + f'<a class="ara-btn ara-btn-ghost" href="/armors?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Back to Sets</a>'
        + f'<a class="ara-btn ara-btn-ghost" href="/temper/create?t={e(token)}&amp;armor={e(a.name)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Temper Armor</a>'
        '</form>'
        + tab_bar
        + tab_content
        + '</section>'
    )
    return _page(f"Armor — {a.name}", "armors", body, token)


# ---------- session detail ----------

_SESSION_STREAM_JS = """
<script>
(function() {
  const tbody = document.getElementById('audit-body');
  const status = document.getElementById('audit-stream-status');
  if (!tbody) return;
  const token = new URLSearchParams(location.search).get('t') || '';
  const sid = tbody.dataset.session || '';
  const MAX_ROWS = 500;
  let es = null;
  let retries = 0;

  function setStatus(state, text) {
    if (!status) return;
    status.dataset.state = state;
    status.textContent = text;
  }

  function connect() {
    setStatus('connecting', 'connecting...');
    es = new EventSource('/events?t=' + encodeURIComponent(token));
    es.onopen = () => { retries = 0; setStatus('live', 'live'); };
    es.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg.html) return;
      // Client-side filter: session column is 2nd cell, its text begins with
      // the session prefix when the event matches this page's session.
      if (sid && msg.session && !msg.session.startsWith(sid)) return;
      const tmp = document.createElement('tbody');
      tmp.innerHTML = msg.html;
      const row = tmp.firstChild;
      if (!row) return;
      row.classList.add('ara-row-new');
      tbody.insertBefore(row, tbody.firstChild);
      while (tbody.children.length > MAX_ROWS) {
        tbody.removeChild(tbody.lastChild);
      }
      setTimeout(() => row.classList.remove('ara-row-new'), 1200);
    };
    es.onerror = () => {
      setStatus('reconnecting', 'reconnecting...');
      es.close();
      retries = Math.min(retries + 1, 6);
      setTimeout(connect, 500 * Math.pow(2, retries));
    };
  }
  connect();
})();
</script>
"""


def _render_transcript_section(state_root: Path, session_id: str) -> str:
    """Show the LLM-call transcript for a session: each turn collapsible,
    role-tinted. Empty state when the agent isn't ARA-aware (no Tier-A hook)."""
    p = state_root / "sessions" / session_id / "transcript.jsonl"
    if not p.exists():
        return (
            '<h3 class="ara-eyebrow" style="margin-top:22px">Transcript (LLM)</h3>'
            '<div class="ara-empty">'
            'No transcript captured. Set <code>ARA_TRANSCRIPT_PROTOCOL=v1</code> '
            'in the worker (ARA already does this on launch); the agent must '
            'call <code>ara.intercept()</code> before each LLM turn for thoughts '
            'to appear here.</div>'
        )
    rows: list[str] = []
    try:
        lines = p.read_text().splitlines()
    except OSError:
        lines = []
    for raw in lines[-100:]:
        try:
            r = json.loads(raw)
        except Exception:
            continue
        role = r.get("role") or "?"
        ts = _fmt_ts(r.get("ts"))
        model = r.get("model") or ""
        role_color = {
            "request":  "var(--primary)",
            "response": "var(--tertiary)",
            "tool_use": "#d4a53a",
            "tool_result": "#9461ff",
        }.get(role, "var(--text-mute)")
        # Reuse the Rhema readable/structured/raw tabs so the session
        # transcript gets the same ergonomics (instructions collapsed,
        # tools collapsed, user input front and center).
        detail = _render_rhema_detail(r)
        rows.append(
            '<details class="ara-transcript-turn" style="margin:6px 0">'
            f'<summary style="cursor:pointer;display:flex;gap:10px;align-items:baseline">'
            f'<span class="mono ara-sub" style="width:8ch">{e(ts)}</span>'
            f'<span class="mono" style="font-size:11px;text-transform:uppercase;'
            f'color:{role_color};width:9ch;display:inline-block">{e(role)}</span>'
            f'<span class="mono ara-sub" style="font-size:11px">{e(model)}</span>'
            '</summary>'
            f'<div style="margin-top:8px">{detail}</div>'
            '</details>'
        )
    return (
        '<h3 class="ara-eyebrow" style="margin-top:22px">Transcript (LLM)</h3>'
        '<p class="ara-sub">'
        f'{len(rows)} turn(s) · what the agent sent to and received from its model.'
        '</p>'
        + ("".join(rows) if rows else '<div class="ara-empty">(empty)</div>')
    )


def _render_session_drift_block(state_root: Path, session_id: str) -> str:
    """Summarize this session's drift against the most-recently-recorded
    baseline. Empty state when no baselines exist. Shows a percent,
    counts, and the first few new resources as hints."""
    try:
        from .. import baseline as _baseline
    except Exception:  # noqa: BLE001
        return ""
    names = _baseline.list_names(state_root)
    if not names:
        return ""
    # Newest by mtime so we pick "the baseline the operator most likely
    # cares about". Operators who need a specific baseline still have
    # the /baselines page + CLI diff.
    try:
        latest = max(
            names,
            key=lambda n: _baseline.path_for(state_root, n).stat().st_mtime,
        )
    except (OSError, ValueError):
        return ""
    try:
        bl = _baseline.load(state_root, latest)
    except Exception:  # noqa: BLE001
        return ""
    session_dir = state_root / "sessions" / session_id
    if not session_dir.is_dir():
        return ""
    score = _baseline.drift_score(bl, session_dir)
    pct = score["drift_ratio"] * 100.0
    color = (
        "var(--tertiary)" if score["new_count"] == 0 else
        "var(--error)"    if pct > 50 else
        "#d4a53a"
    )
    samples_block = ""
    def _samples(label: str, items: list[str]) -> str:
        if not items:
            return ""
        head = items[:6]
        more = "" if len(items) <= 6 else f" · +{len(items) - 6} more"
        return (
            f'<div style="margin-top:6px"><span class="ara-sub">'
            f'{e(label)}:</span> '
            + " ".join(f'<code class="mono">{e(x)}</code>' for x in head)
            + more + '</div>'
        )
    samples_block = (
        _samples("new paths", score["new_paths"])
        + _samples("new commands", score["new_commands"])
        + _samples("new hosts", score["new_hosts"])
    )
    return (
        '<h3 class="ara-eyebrow" style="margin-top:22px">'
        'Baseline drift</h3>'
        f'<p class="ara-sub">vs. baseline <span class="mono">{e(latest)}</span> '
        f'({score["baseline_total"]} items).</p>'
        '<div style="display:flex;gap:12px;align-items:baseline;flex-wrap:wrap">'
        f'<strong style="font-size:20px;color:{color}">{pct:.1f}%</strong>'
        f'<span class="ara-sub">{score["new_count"]} new resource(s) '
        'observed in this session</span>'
        '</div>'
        + samples_block
    )


def _collect_timeline_points(state_root: Path, session_id: str) -> list[dict]:
    """Walk the three per-session JSONL streams and return a flat list
    of ``{ts, lane, kind, title}`` dicts suitable for a compact
    timeline. Lanes:
      - "spawn"      — events.jsonl kind=spawn
      - "policy"     — every policy_hits.jsonl line
      - "transcript" — transcript.jsonl request/response turns
      - "approval"   — transcript.jsonl rhema_approval_* records
    Bounded to the first 2000 points per source so even long runs
    render instantly.
    """
    sdir = state_root / "sessions" / session_id
    points: list[dict] = []
    sources = [
        ("events.jsonl", "spawn"),
        ("policy_hits.jsonl", "policy"),
        ("transcript.jsonl", "transcript"),
    ]
    for name, default_lane in sources:
        f = sdir / name
        if not f.exists():
            continue
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for raw in lines[:2000]:
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            ts = r.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            kind = r.get("kind") or ""
            role = r.get("role") or ""
            if default_lane == "spawn":
                # events.jsonl carries spawns AND approval_pending/resolved.
                if kind == "spawn":
                    points.append({"ts": float(ts), "lane": "spawn",
                                   "kind": kind, "title": kind})
                elif kind in ("approval_pending", "approval_resolved"):
                    points.append({"ts": float(ts), "lane": "approval",
                                   "kind": kind, "title": kind})
                continue
            if default_lane == "policy":
                verdict = (r.get("verdict") or "").lower()
                title = (f"{kind or 'hit'} / {verdict}" if verdict
                         else (kind or "hit"))
                points.append({"ts": float(ts), "lane": "policy",
                               "kind": kind, "title": title})
                continue
            if default_lane == "transcript":
                # transcript.jsonl: `role` carries request/response (kind is
                # always "transcript"). rhema approvals, if present, too.
                if role in ("request", "response"):
                    points.append({"ts": float(ts), "lane": "transcript",
                                   "kind": role, "title": role})
                elif kind in ("rhema_approval_pending", "rhema_approval_resolved"):
                    points.append({"ts": float(ts), "lane": "approval",
                                   "kind": kind, "title": kind})
    points.sort(key=lambda p: p["ts"])
    return points


def _render_session_timeline(state_root: Path, session_id: str) -> str:
    """Compact inline SVG timeline — four lanes, one dot per event.
    No deps. Hover-tooltips via ``<title>`` so the browser renders them
    natively. Returns an empty-state div when there are no points."""
    points = _collect_timeline_points(state_root, session_id)
    if not points:
        return '<div class="ara-empty">No events recorded yet.</div>'
    t0 = points[0]["ts"]
    t1 = points[-1]["ts"]
    span = max(t1 - t0, 0.001)
    width = 900
    height = 120
    lanes = [
        ("spawn",      0,  "var(--primary)",  "Spawns"),
        ("policy",     1,  "var(--error)",    "Policy hits"),
        ("transcript", 2,  "var(--tertiary)", "Transcript"),
        ("approval",   3,  "#d4a53a",         "Approvals"),
    ]
    lane_y = {name: 24 + idx * 22 for name, idx, _, _ in lanes}
    lane_color = {name: color for name, _, color, _ in lanes}

    def _x(ts: float) -> float:
        return 80 + (ts - t0) / span * (width - 100)

    dots: list[str] = []
    for p in points:
        x = _x(p["ts"])
        y = lane_y.get(p["lane"], 80)
        color = lane_color.get(p["lane"], "var(--on-surface)")
        tip = f'{p["kind"] or p["lane"]} — {p["ts"] - t0:.2f}s'
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y}" r="3.2" fill="{color}" '
            f'fill-opacity="0.75"><title>{e(tip)}</title></circle>'
        )

    lane_labels = "".join(
        f'<text x="6" y="{24 + idx * 22 + 3}" '
        'font-size="10" fill="var(--on-surface)" '
        'style="font-family:var(--font-mono)">'
        f'{e(label)}</text>'
        for _, idx, _, label in lanes
    )
    import datetime as _dt
    tick_labels = (
        f'<text x="80" y="{height - 6}" font-size="10" '
        'fill="color-mix(in srgb,var(--on-surface) 55%,transparent)" '
        f'style="font-family:var(--font-mono)">'
        f'{e(_dt.datetime.fromtimestamp(t0).strftime("%H:%M:%S"))}</text>'
        f'<text x="{width - 6}" y="{height - 6}" font-size="10" '
        'text-anchor="end" fill="color-mix(in srgb,var(--on-surface) 55%,transparent)" '
        f'style="font-family:var(--font-mono)">'
        f'{e(_dt.datetime.fromtimestamp(t1).strftime("%H:%M:%S"))}</text>'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" class="ara-timeline" '
        'style="background:var(--surface-container-lowest);'
        'border-radius:var(--radius-sm);margin:6px 0">'
        + lane_labels
        + "".join(dots)
        + tick_labels
        + "</svg>"
        f'<p class="ara-sub">{len(points)} events across '
        f'{span:.2f}s · hover a dot for its kind/offset</p>'
    )


def render_canaries(
    state_root: Path, token: str,
    *, flash: str | None = None, flash_level: str = "info",
) -> str:
    """Canary tokens dashboard — list, verify, add."""
    from .. import canary as canary_mod
    store = canary_mod.load(state_root)
    rep = canary_mod.verify_all(state_root)

    flash_html = ""
    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        flash_html = (
            '<div class="ara-empty" '
            f'style="border-left:3px solid var({cls});margin-bottom:12px">{e(flash)}</div>'
        )

    body: list[str] = [
        '<section>',
        '<div class="ara-eyebrow">Evidence</div>',
        '<h2 class="ara-h">Canary tokens</h2>',
        '<p class="ara-sub">Seed realistic-looking but fake credentials at '
        'filesystem paths agents might read. When a canary value surfaces '
        'in an LLM reply, the MITM logs a <span class="mono">canary_hit</span> '
        'event — strongest signal there is of exfiltration intent.</p>',
        flash_html,
    ]

    def _chip(label: str, count: int, color: str) -> str:
        return (
            f'<span style="padding:3px 10px;border-radius:10px;'
            f'background:color-mix(in srgb,{color} 18%,transparent);'
            f'color:{color};font-size:11px;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:0.04em;'
            f'font-family:var(--font-mono)">{count}&nbsp;{e(label)}</span>'
        )

    body.append(
        '<div style="display:flex;gap:8px;margin:10px 0 14px;flex-wrap:wrap">'
        + _chip("seeded", rep["total"], "var(--primary)")
        + _chip("ok",       len(rep["ok"]),       "var(--tertiary)")
        + _chip("tampered", len(rep["tampered"]), "var(--error)")
        + _chip("missing",  len(rep["missing"]),  "#ffcf66")
        + '</div>'
    )

    if not store.canaries:
        body.append(
            '<div class="ara-empty">No canaries seeded yet. '
            'Use the form below or <span class="mono">ara canary add &lt;path&gt; '
            '--kind aws|stripe|github|openai|generic</span>.</div>'
        )
    else:
        body.append(
            '<table style="width:100%;border-collapse:collapse;font-size:12px;'
            'margin-top:4px">'
            '<thead><tr style="color:var(--text-mute);font-size:10px;'
            'text-transform:uppercase;letter-spacing:0.08em">'
            '<th style="text-align:left;padding:6px">status</th>'
            '<th style="text-align:left;padding:6px">kind</th>'
            '<th style="text-align:left;padding:6px">hits</th>'
            '<th style="text-align:left;padding:6px">value</th>'
            '<th style="text-align:left;padding:6px">path</th>'
            '<th style="text-align:left;padding:6px">note</th>'
            '<th style="text-align:right;padding:6px">action</th>'
            '</tr></thead><tbody>'
        )
        for c in store.canaries:
            if c.id in rep["tampered"]:
                status_html = '<span style="color:var(--error);font-weight:600">TAMPERED</span>'
            elif c.id in rep["missing"]:
                status_html = '<span style="color:#ffcf66;font-weight:600">MISSING</span>'
            else:
                status_html = '<span style="color:var(--tertiary);font-weight:600">OK</span>'
            hits_html = (
                f'<span style="color:var(--error);font-weight:700">{c.hit_count}</span>'
                if c.hit_count > 0 else f'<span>{c.hit_count}</span>'
            )
            body.append(
                '<tr style="border-top:1px solid var(--outline-variant)">'
                f'<td style="padding:6px">{status_html}</td>'
                f'<td style="padding:6px"><span class="mono">{e(c.kind)}</span></td>'
                f'<td style="padding:6px">{hits_html}</td>'
                f'<td style="padding:6px"><span class="mono" '
                f'title="{e(c.value)}">{e(c.value[:24])}…</span></td>'
                f'<td style="padding:6px"><span class="mono">{e(c.path)}</span></td>'
                f'<td style="padding:6px">{e(c.note or "")}</td>'
                '<td style="padding:6px;text-align:right">'
                '<form method="POST" action="/canaries/remove" style="display:inline;margin:0">'
                f'<input type="hidden" name="token" value="{e(token)}">'
                f'<input type="hidden" name="id" value="{e(c.id)}">'
                '<button type="submit" class="ara-btn ara-btn-ghost" '
                'style="padding:2px 8px;font-size:10px">remove</button>'
                '</form></td>'
                '</tr>'
            )
        body.append('</tbody></table>')

    input_css = (
        "padding:8px;font-family:var(--font-mono);font-size:12px;"
        "background:var(--surface-container-lowest);color:var(--on-surface);"
        "border:1px solid var(--outline-variant);border-radius:6px"
    )
    body.append(
        f'<div style="margin-top:22px;padding:14px 16px;border:1px solid var(--outline-variant);'
        f'border-radius:10px;background:color-mix(in srgb,var(--on-surface) 3%,transparent)">'
        '<h3 style="margin:0 0 10px;font-size:14px">Seed a canary</h3>'
        '<form method="POST" action="/canaries/add" '
        'style="display:grid;grid-template-columns:120px 1fr;gap:10px 14px;align-items:center">'
        f'<input type="hidden" name="token" value="{e(token)}">'
        '<label style="font-size:12px;color:var(--text-mute)" '
        'title="Pick a template. Each kind writes a differently-shaped credential '
        'file so the agent thinks it found a real secret.">Kind</label>'
        f'<select name="kind" style="{input_css}">'
        '<option value="aws">aws (AKIA…/secret)</option>'
        '<option value="stripe">stripe (sk_live_…)</option>'
        '<option value="github">github (ghp_…)</option>'
        '<option value="openai">openai (sk-…)</option>'
        '<option value="generic" selected>generic</option>'
        '</select>'
        '<label style="font-size:12px;color:var(--text-mute)" '
        'title="Absolute path. Must not already exist. '
        'Pick somewhere the agent is likely to look.">Path</label>'
        f'<input name="path" type="text" style="{input_css}" '
        f'placeholder="/home/user/.aws/credentials" required>'
        '<label style="font-size:12px;color:var(--text-mute)">Note</label>'
        f'<input name="note" type="text" style="{input_css}" '
        'placeholder="optional — e.g. &quot;staging sandbox&quot;">'
        '<div></div>'
        '<button type="submit" class="ara-btn ara-btn-primary" '
        'style="justify-self:start;margin-top:4px">Seed</button>'
        '</form></div>'
    )

    body.append('</section>')
    return _page("Canaries", "canaries", "\n".join(body), token)


def _render_session_evidence(session_dir: Path, token: str) -> str:
    """Render this session's captured evidence from the EvidenceBinder store
    (the artifacts findings actually cite: HTTP via MITM, shell/file via the
    shim, agent captures). Each id links to the raw-content viewer. Falls back
    to the legacy provenance index for sessions that used only record_artifact."""
    import sqlite3
    from collections import Counter as _Counter
    idx = session_dir / "evidence" / "index.db"
    rows_data: list = []
    if idx.is_file():
        try:
            con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows_data = con.execute(
                "SELECT id, source, source_metadata_json, summary, size_bytes, "
                "content_type, captured_at FROM evidence ORDER BY captured_at DESC"
            ).fetchall()
            con.close()
        except Exception:
            rows_data = []
    if not rows_data:
        return _render_session_provenance(session_dir, token)

    sid = session_dir.name
    by_src = _Counter(r["source"] for r in rows_data)
    chips = "".join(
        '<span style="padding:3px 10px;border-radius:10px;background:'
        'color-mix(in srgb,var(--primary) 16%,transparent);color:var(--primary);'
        f'font-size:11px;font-weight:600;margin-right:6px">{cnt}&nbsp;{e(str(src))}'
        '</span>' for src, cnt in by_src.most_common())
    out = ['<h3 class="ara-eyebrow">Captured evidence</h3>',
           '<p class="ara-sub">Every artifact captured for this session — the '
           'raw material findings cite. Click an id to view its content.</p>',
           f'<div style="margin:8px 0 14px">{len(rows_data)} records &nbsp; {chips}</div>',
           '<table style="width:100%;border-collapse:collapse;font-size:12px">'
           '<thead><tr style="color:var(--text-mute);font-size:10px;'
           'text-transform:uppercase;letter-spacing:0.08em">'
           '<th style="text-align:left;padding:6px">id</th>'
           '<th style="text-align:left;padding:6px">source</th>'
           '<th style="text-align:left;padding:6px">method / url / summary</th>'
           '<th style="text-align:left;padding:6px">type</th>'
           '<th style="text-align:left;padding:6px">size</th>'
           '</tr></thead><tbody>']
    for r in rows_data[:500]:
        try:
            meta = json.loads(r["source_metadata_json"] or "{}")
        except Exception:
            meta = {}
        murl = " ".join(str(meta[k]) for k in ("method", "url")
                        if meta.get(k)) or (r["summary"] or "")
        status = meta.get("status") or meta.get("status_code")
        if status:
            murl = f"[{status}] {murl}"
        out.append(
            '<tr style="border-top:1px solid var(--outline-variant)">'
            f'<td style="padding:6px"><a href="/engagement/evidence/{e(str(r["id"]))}'
            f'?t={e(token)}&session={e(sid)}"><span class="mono">{e(str(r["id"]))}'
            '</span></a></td>'
            f'<td style="padding:6px"><span class="mono">{e(str(r["source"]))}</span></td>'
            f'<td style="padding:6px"><span class="mono">{e(str(murl)[:90])}</span></td>'
            f'<td style="padding:6px"><span class="mono">{e(str(r["content_type"] or ""))}</span></td>'
            f'<td style="padding:6px">{int(r["size_bytes"] or 0):,}</td></tr>')
    out.append('</tbody></table>')
    if len(rows_data) > 500:
        out.append(f'<p class="ara-sub">Showing 500 of {len(rows_data)} '
                   '(newest first).</p>')
    return "\n".join(out)


def _render_session_provenance(session_dir: Path, token: str) -> str:
    """Legacy provenance index (evidence_index.jsonl via record_artifact).

    Each row: capture_id, status (ok/tampered/missing), tool, kind,
    sha256 prefix, size, absolute path. Uses ``evidence.verify`` so
    tampered/missing rows are flagged with a red/amber badge.
    """
    from .. import evidence as evidence_mod
    records = evidence_mod.load(session_dir)
    out = ['<h3 class="ara-eyebrow">Provenance index</h3>']
    out.append(
        '<p class="ara-sub">Append-only SHA256 index of every tool-produced '
        f'artifact for this session. Verify with '
        f'<span class="mono">ara evidence verify {e(str(session_dir))}</span>.</p>'
    )
    if not records:
        out.append(
            '<div class="ara-empty">No records yet. Harnesses that '
            'want forensic-grade traceability should call '
            '<span class="mono">ara.evidence.record_artifact(...)</span> '
            'at tool-call completion.</div>'
        )
        return "\n".join(out)

    # Summary strip.
    rep = evidence_mod.verify_session(session_dir)
    def _chip(label: str, count: int, color: str) -> str:
        return (
            f'<span style="padding:3px 10px;border-radius:10px;'
            f'background:color-mix(in srgb,{color} 18%,transparent);'
            f'color:{color};font-size:11px;font-weight:600;'
            f'font-family:var(--font-mono);text-transform:uppercase">'
            f'{count}&nbsp;{e(label)}</span>'
        )
    out.append(
        '<div style="display:flex;gap:8px;margin:8px 0 14px;flex-wrap:wrap">'
        + _chip("total", rep["total"], "var(--primary)")
        + _chip("ok", len(rep["ok"]), "var(--tertiary)")
        + _chip("tampered", len(rep["tampered"]), "var(--error)")
        + _chip("missing", len(rep["missing"]), "#ffcf66")
        + '</div>'
    )

    out.append(
        '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        '<thead><tr style="color:var(--text-mute);font-size:10px;'
        'text-transform:uppercase;letter-spacing:0.08em">'
        '<th style="text-align:left;padding:6px">status</th>'
        '<th style="text-align:left;padding:6px">tool</th>'
        '<th style="text-align:left;padding:6px">action</th>'
        '<th style="text-align:left;padding:6px">sha256</th>'
        '<th style="text-align:left;padding:6px">size</th>'
        '<th style="text-align:left;padding:6px">path</th>'
        '</tr></thead><tbody>'
    )
    for r in sorted(records, key=lambda r: r.recorded_at, reverse=True):
        if r.capture_id in rep["tampered"]:
            status_html = (
                '<span style="color:var(--error);font-weight:600">'
                'TAMPERED</span>'
            )
        elif r.capture_id in rep["missing"]:
            status_html = (
                '<span style="color:#ffcf66;font-weight:600">MISSING</span>'
            )
        else:
            status_html = (
                '<span style="color:var(--tertiary);font-weight:600">OK</span>'
            )
        out.append(
            '<tr style="border-top:1px solid var(--outline-variant)">'
            f'<td style="padding:6px">{status_html}</td>'
            f'<td style="padding:6px"><span class="mono">{e(r.tool_name)}</span></td>'
            f'<td style="padding:6px"><span class="mono">{e(r.action)}</span></td>'
            f'<td style="padding:6px"><span class="mono" title="{e(r.artifact_sha256)}">'
            f'{e(r.artifact_sha256[:16])}…</span></td>'
            f'<td style="padding:6px">{r.size_bytes:,}</td>'
            f'<td style="padding:6px"><span class="mono">{e(r.artifact_path)}</span></td>'
            '</tr>'
        )
    out.append('</tbody></table>')
    return "\n".join(out)


def _resolve_session_dir(state_root: Path, session_id: str) -> tuple[str, Path] | None:
    sessions = state_root / "sessions"
    direct = sessions / session_id
    if direct.is_dir():
        return session_id, direct
    if not sessions.is_dir() or not session_id:
        return None
    matches = [
        p for p in sessions.iterdir()
        if p.is_dir() and p.name.startswith(session_id)
    ]
    if len(matches) == 1:
        return matches[0].name, matches[0]
    return None


def render_session(
    state_root: Path, session_id: str, token: str,
    *, flash: str | None = None, flash_level: str = "info",
    tab: str = "activity",
) -> str | None:
    resolved = _resolve_session_dir(state_root, session_id)
    if resolved is None:
        return None
    session_id, s = resolved
    meta_path = s / "session.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            meta = {}
    prefix = session_id[:10]
    # Read THIS session's complete streams (events.jsonl for spawns/approvals +
    # policy_hits.jsonl for the allow/deny verdicts) — not the global recent
    # window, which for an older session shows only a stale sliver. Newest-first.
    events = []
    for _name in ("events.jsonl", "policy_hits.jsonl"):
        _ep = s / _name
        if not _ep.is_file():
            continue
        try:
            for _line in _ep.open():
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _r = json.loads(_line)
                    _r["_source"] = _name
                    events.append(_r)
                except Exception:
                    continue
        except OSError:
            continue
    events.sort(key=lambda r: r.get("ts", 0), reverse=True)

    # --- Summary breakdown ---
    counts: dict[str, int] = {}
    spawns: list[dict] = []
    touched_paths: set[str] = set()
    for r in events:
        verdict = r.get("verdict") or r.get("kind") or "other"
        counts[verdict] = counts.get(verdict, 0) + 1
        target = r.get("path") or r.get("touched")
        if target:
            touched_paths.add(target)
        if r.get("op") == "exec.spawn" or r.get("kind") == "spawn":
            spawns.append(r)
    pending_count = len([
        p for p in (s / "approvals" / "pending").glob("*.json")
    ]) if (s / "approvals" / "pending").is_dir() else 0

    summary_html = (
        '<dl class="ara-dl">'
        f'<dt>events (session)</dt><dd>{len(events)}</dd>'
        f'<dt>allow</dt><dd class="verdict-allow">{counts.get("allow", 0)}</dd>'
        f'<dt>deny</dt><dd class="verdict-deny">{counts.get("deny", 0)}</dd>'
        f'<dt>approval</dt><dd class="verdict-approval">'
        f'{counts.get("approval", 0) + counts.get("approval_pending", 0)}</dd>'
        f'<dt>spawns</dt><dd>{len(spawns)}</dd>'
        f'<dt>unique paths touched</dt><dd>{len(touched_paths)}</dd>'
        f'<dt>pending approvals</dt><dd>{pending_count}</dd>'
        '</dl>'
    )

    # --- Spawn tree ---
    # Sort spawns by timestamp asc; render as pid/ppid indented list keyed by ppid.
    spawns_sorted = sorted(spawns, key=lambda r: r.get("ts") or 0)
    if spawns_sorted:
        spawn_rows = []
        for r in spawns_sorted:
            pid = r.get("pid") or r.get("child_pid") or ""
            ppid = r.get("ppid") or ""
            command = r.get("command") or r.get("path") or ""
            args = r.get("args") or []
            argstr = " ".join(str(a) for a in args[:6])
            if len(args) > 6:
                argstr += " …"
            spawn_rows.append(
                "<tr>"
                f'<td class="mono">{e(_fmt_ts(r.get("ts")))}</td>'
                f'<td class="mono">{e(pid)}</td>'
                f'<td class="mono">{e(ppid)}</td>'
                f'<td class="mono">{e(command)}</td>'
                f'<td class="mono">{e(argstr)}</td>'
                "</tr>"
            )
        spawn_html = (
            '<table class="ara-table">'
            '<thead><tr><th>Time</th><th>pid</th><th>ppid</th>'
            '<th>command</th><th>args</th></tr></thead>'
            f'<tbody>{"".join(spawn_rows)}</tbody></table>'
        )
    else:
        spawn_html = '<div class="ara-empty">No spawns observed.</div>'

    # --- Audit table (initial render + live append via SSE filter) ---
    audit_rows = [render_audit_row(r) for r in events[:200]]

    pid = meta.get("pid")
    running = False
    if isinstance(pid, int) and pid > 0:
        try:
            import os as _os
            _os.kill(pid, 0)
            running = True
        except OSError:
            running = False
    kill_confirm = "Send SIGTERM to this session's supervisor? The worker will exit."
    kill_form = (
        f'<form method="post" action="/sessions/{e(session_id)}/kill?t={e(token)}" '
        f'style="display:inline" onsubmit="return confirm(\'{e(kill_confirm)}\')">'
        '<button class="ara-btn ara-btn-bad" type="submit">Terminate session</button>'
        '</form>'
        if running
        else '<span class="ara-sub">(process exited)</span>'
    )
    verify_form = (
        f'<form method="post" action="/sessions/{e(session_id)}/verify-audit?t={e(token)}" '
        'style="display:inline;margin-left:8px">'
        '<button class="ara-btn ara-btn-ghost" type="submit">Verify audit chain</button>'
        '</form>'
    )
    baseline_form = (
        f'<form method="post" action="/sessions/{e(session_id)}/baseline?t={e(token)}" '
        'style="display:inline;margin-left:8px">'
        '<input type="text" name="name" placeholder="baseline name" '
        'pattern="[a-zA-Z0-9._-]+" required '
        'style="font-size:11px;padding:3px 8px;width:14ch">'
        '<button class="ara-btn ara-btn-ghost" type="submit">Record baseline</button>'
        '</form>'
    )
    delete_form = (
        f'<form method="post" action="/sessions/{e(session_id)}/delete?t={e(token)}" '
        'style="display:inline;margin-left:8px" '
        'onsubmit="return confirm(\'Delete this archived session? '
        'Audit, transcript, and containment folder will be removed — '
        'this is irreversible.\')">'
        '<button class="ara-btn ara-btn-bad" type="submit">Delete session</button>'
        '</form>'
        if not running
        else ''
    )
    export_link = (
        f'<a class="ara-btn ara-btn-ghost" '
        f'href="/sessions/{e(session_id)}/export.tgz?t={e(token)}" '
        'style="text-decoration:none;margin-left:8px">Export .tgz</a>'
        f'<a class="ara-btn ara-btn-ghost" '
        f'href="/sessions/{e(session_id)}/export.sig?t={e(token)}" '
        'style="text-decoration:none;margin-left:4px" '
        'title="Detached HMAC signature — verify the downloaded .tgz '
        'with openssl dgst -sha256 -hmac $TOKEN export.tgz">.sig</a>'
    )
    flash_html = ""
    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        flash_html = (
            '<div class="ara-empty" '
            f'style="border-left:3px solid var({cls});margin-bottom:14px">{e(flash)}</div>'
        )
    # Tab bar — URL-driven for deep-linkability.
    tabs = [
        ("activity", "Activity"),
        ("audit", "Audit"),
        ("transcript", "Transcript"),
        ("evidence", "Evidence"),
        ("details", "Details"),
    ]
    if tab not in {k for k, _ in tabs}:
        tab = "activity"

    def _tab_pill(key: str, label: str) -> str:
        is_active = (key == tab)
        style = (
            'background:color-mix(in srgb,var(--primary) 70%,transparent);'
            'color:#fff;border:1px solid var(--primary);'
            if is_active else
            'background:color-mix(in srgb,var(--outline-variant) 18%,transparent);'
            'color:var(--on-surface);border:1px solid '
            'color-mix(in srgb,var(--outline-variant) 25%,transparent);'
        )
        return (
            f'<a href="/sessions/{e(session_id)}?t={e(token)}&tab={key}" '
            f'style="text-decoration:none;padding:6px 14px;font-size:12px;'
            f'border-radius:14px;font-family:var(--font-mono);'
            f'letter-spacing:0.04em;text-transform:uppercase;{style}">'
            f'{e(label)}</a>'
        )
    tab_bar = (
        '<div style="display:flex;gap:8px;margin:4px 0 16px 0;flex-wrap:wrap">'
        + "".join(_tab_pill(k, l) for k, l in tabs)
        + '</div>'
    )

    activity_html = (
        '<h3 class="ara-eyebrow">Breakdown</h3>' + summary_html
        + _render_session_drift_block(state_root, session_id)
        + '<h3 class="ara-eyebrow" style="margin-top:22px">Timeline</h3>'
        + _render_session_timeline(state_root, session_id)
        + '<h3 class="ara-eyebrow" style="margin-top:22px">Spawn tree</h3>'
        + spawn_html
    )
    audit_html = (
        '<h3 class="ara-eyebrow">Audit (live, filtered)</h3>'
        + (
            '<div class="ara-empty">No audit events yet.</div>'
            if not audit_rows
            else '<table class="ara-table">'
                 '<thead><tr><th>Timestamp</th><th>Session</th><th>Source</th>'
                 '<th>Kind</th><th>Verdict</th><th>Reason</th><th>Target</th>'
                 '</tr></thead>'
                 f'<tbody id="audit-body" data-session="{e(prefix)}">'
                 f'{"".join(audit_rows)}</tbody></table>'
        )
        + _SESSION_STREAM_JS
    )
    transcript_html = _render_transcript_section(state_root, session_id)
    details_html = (
        '<h3 class="ara-eyebrow">Metadata</h3>'
        f'<pre class="ara-code">{e(json.dumps(meta, indent=2, sort_keys=True))}</pre>'
        + _render_session_containment_block(state_root, session_id)
    )
    evidence_html = _render_session_evidence(s, token)
    tab_content = {
        "activity": activity_html,
        "audit": audit_html,
        "transcript": transcript_html,
        "evidence": evidence_html,
        "details": details_html,
    }[tab]

    body = [
        '<section>',
        '<div class="ara-eyebrow">Session</div>',
        f'<h2 class="ara-h">{e(session_id[:12])}</h2>',
        '<p class="ara-sub">'
        f'<span class="mono">{e(session_id)}</span> · '
        '<span id="audit-stream-status" data-state="connecting">connecting...</span>'
        '</p>',
        flash_html,
        '<div style="margin:8px 0 14px 0;display:flex;gap:8px;flex-wrap:wrap">',
        kill_form, verify_form, baseline_form, export_link, delete_form,
        '</div>',
        tab_bar,
        tab_content,
        '</section>',
    ]
    return _page(
        f"Session {session_id[:8]}", "courtyard", "".join(body), token,
    )


# ---------- docs ----------

_DOC_CARD_ASSETS = (
    '<link rel="stylesheet" href="/static/armor-card.css?t={tok}&v='
    + _STATIC_REV
    + '">'
    '<link rel="stylesheet" href="/static/docs.css?t={tok}&v='
    + _STATIC_REV
    + '">'
    '<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900'
    '&family=Rajdhani:wght@300;400;500;600;700&family=Press+Start+2P'
    '&display=swap" rel="stylesheet">'
)


def _doc_card(
    topic: "docs_content.DocTopic", token: str, *,
    hero: bool,
    wrap: str = "link",  # "link" | "button" | "none"
) -> str:
    rarity = topic.rarity if topic.rarity in (
        "common", "uncommon", "rare", "epic", "legendary",
    ) else "common"
    stars = {
        "common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5,
    }[rarity]
    abilities_html = "".join(
        '<div class="bc-ability-item">'
        '<div class="bc-ability-hex">◆</div>'
        '<div class="bc-ability-info">'
        f'<div class="bc-a-name">{e(name)}</div>'
        f'<div class="bc-a-level">{e(desc)}</div>'
        '</div></div>'
        for name, desc in topic.abilities
    )
    size_cls = "" if hero else " bc-card-compact"
    inner = (
        f'<div class="bc-card bc-rarity-{rarity}{size_cls}">'
        '<div class="bc-card-inner">'
        '<div class="bc-holo-shimmer"></div>'
        '<div class="bc-rarity-banner">'
        f'<div class="bc-rarity-stars">{"★" * stars}{"☆" * (5 - stars)}</div>'
        f'<div class="bc-rarity-label">{e(rarity)}</div>'
        '</div>'
        '<div class="bc-card-art">'
        f'<div class="bc-sigil">{topic.sigil}</div>'
        '<div class="bc-name-plate">'
        f'<h2>{e(topic.title)}</h2>'
        f'<div class="bc-level">TIER {e(topic.tier_roman)}</div>'
        '</div>'
        '</div>'
        '<div class="bc-abilities">'
        '<div class="bc-abilities-title">Abilities</div>'
        f'<div class="bc-ability-grid">{abilities_html}</div>'
        '</div>'
        '<div class="bc-passives">'
        f'<div class="bc-passives-title">{e(topic.blurb)}</div>'
        '</div>'
        '</div></div>'
    )
    if wrap == "link":
        return (
            f'<a class="bc-card-link" href="/docs/{e(topic.slug)}?t={e(token)}">'
            + inner + '</a>'
        )
    if wrap == "button":
        return (
            '<button type="button" class="bc-card-link bc-card-btn" '
            f'data-slug="{e(topic.slug)}" '
            f'aria-label="Open {e(topic.title)}">'
            + inner + '</button>'
        )
    return inner


_DOC_RARITY_ORDER = ["legendary", "epic", "rare", "uncommon", "common"]
_DOC_RARITY_BLURB = {
    "legendary": "Core pieces. Start here.",
    "epic":      "Operator-facing rule layers.",
    "rare":      "Supporting mechanisms.",
    "uncommon":  "Runtime & session plumbing.",
    "common":    "Reference sheets.",
}


def _doc_topic_template(topic: "docs_content.DocTopic", token: str) -> str:
    """Hidden <template> containing a topic's full view (card + prose).
    The index-page JS clones this into the top panel on click."""
    sections_html = "".join(
        '<section class="doc-section">'
        f'<h3 class="ara-eyebrow">{e(s.heading)}</h3>'
        f'<div class="doc-prose">{s.body}</div>'
        '</section>'
        for s in topic.sections
    )
    return (
        f'<template id="tpl-{e(topic.slug)}">'
        '<div class="doc-topic-layout">'
        '<div class="doc-topic-card">'
        + _doc_card(topic, token, hero=True, wrap="none") +
        '</div>'
        '<div class="doc-topic-body">'
        f'<div class="ara-eyebrow">Documentation</div>'
        f'<h2 class="ara-h" style="margin:0 0 4px 0">{e(topic.title)}</h2>'
        f'<p class="ara-sub" style="margin:0 0 16px 0">{e(topic.blurb)}</p>'
        f'{sections_html}'
        '</div>'
        '</div>'
        '</template>'
    )


_DOC_INDEX_JS = """
<script>
(function(){
  const panel = document.getElementById('doc-selected-panel');
  if (!panel) return;

  function select(slug, opts) {
    opts = opts || {};
    const tpl = document.getElementById('tpl-' + slug);
    if (!tpl) return false;
    panel.innerHTML = '';
    panel.appendChild(tpl.content.cloneNode(true));
    const closer = document.createElement('button');
    closer.type = 'button';
    closer.className = 'doc-close';
    closer.setAttribute('aria-label','Close');
    closer.textContent = '\\u00d7';
    closer.addEventListener('click', clear);
    panel.prepend(closer);
    panel.classList.add('is-open');
    if (!opts.skipHash) {
      history.replaceState(null, '', '#' + slug);
    }
    if (!opts.noScroll) {
      // Offset past the sticky header + mobile nav rail.
      const headerOffset = (window.innerWidth <= 900) ? 120 : 24;
      const y = panel.getBoundingClientRect().top
                + window.pageYOffset - headerOffset;
      window.scrollTo({top: y, behavior: 'smooth'});
    }
    document.querySelectorAll('.bc-card-btn').forEach(b => {
      b.classList.toggle('is-active', b.dataset.slug === slug);
    });
    return true;
  }
  function clear() {
    panel.innerHTML = '';
    panel.classList.remove('is-open');
    history.replaceState(null, '', location.pathname + location.search);
    document.querySelectorAll('.bc-card-btn.is-active')
      .forEach(b => b.classList.remove('is-active'));
  }
  document.addEventListener('click', function(e){
    const btn = e.target.closest('.bc-card-btn');
    if (btn) { e.preventDefault(); select(btn.dataset.slug); }
  });
  window.addEventListener('hashchange', function(){
    const s = location.hash.replace('#','');
    if (s) select(s, {skipHash:true});
    else clear();
  });
  const initial = location.hash.replace('#','');
  if (initial) select(initial, {skipHash:true, noScroll:true});
})();
</script>
"""


def render_docs_index(token: str) -> str:
    # Group topics by rarity, ordering within a group by original TOPICS order.
    by_rarity: dict[str, list] = {r: [] for r in _DOC_RARITY_ORDER}
    for t in docs_content.TOPICS:
        r = t.rarity if t.rarity in by_rarity else "common"
        by_rarity[r].append(t)

    groups_html: list[str] = []
    for rarity in _DOC_RARITY_ORDER:
        topics = by_rarity[rarity]
        if not topics:
            continue
        cards = "".join(
            _doc_card(t, token, hero=False, wrap="button") for t in topics
        )
        groups_html.append(
            '<details class="doc-rarity-group" open '
            f'data-rarity="{e(rarity)}">'
            '<summary>'
            f'<span class="doc-rarity-heading">{e(rarity)}</span>'
            f'<span class="doc-rarity-count">{len(topics)}</span>'
            '<span class="doc-rarity-sub">'
            f'{e(_DOC_RARITY_BLURB.get(rarity, ""))}</span>'
            '<span class="doc-rarity-chev" aria-hidden="true">▾</span>'
            '</summary>'
            f'<div class="doc-card-grid">{cards}</div>'
            '</details>'
        )
    templates_html = "".join(
        _doc_topic_template(t, token) for t in docs_content.TOPICS
    )
    body = (
        _DOC_CARD_ASSETS.format(tok=e(token))
        + '<section>'
        '<div class="ara-eyebrow">Documentation</div>'
        '<h2 class="ara-h" style="margin-bottom:4px">Codex of ARA</h2>'
        '<p class="ara-sub">Tap a card. It moves to the top and opens. '
        'Rarity groups are collapsible — click a heading to fold.</p>'
        '<div id="doc-selected-panel" class="doc-selected-panel" '
        'aria-live="polite"></div>'
        + "".join(groups_html)
        + templates_html
        + '</section>'
        + _DOC_INDEX_JS
    )
    return _page("Docs", "docs", body, token)


def render_docs_topic(token: str, slug: str) -> str | None:
    topic = docs_content.by_slug(slug)
    if topic is None:
        return None
    sections_html = "".join(
        '<section class="doc-section">'
        f'<h3 class="ara-eyebrow">{e(s.heading)}</h3>'
        f'<div class="doc-prose">{s.body}</div>'
        '</section>'
        for s in topic.sections
    )
    # One other-topic-strip so the reader can jump laterally without going back.
    others = [t for t in docs_content.TOPICS if t.slug != slug]
    strip = "".join(
        f'<a class="doc-strip-pill" href="/docs/{e(t.slug)}?t={e(token)}">'
        f'<span class="doc-strip-tier">{e(t.tier_roman)}</span> {e(t.title)}'
        '</a>'
        for t in others
    )
    body = (
        _DOC_CARD_ASSETS.format(tok=e(token))
        + '<section>'
        '<div class="ara-eyebrow">Documentation</div>'
        f'<h2 class="ara-h" style="margin-bottom:4px">{e(topic.title)}</h2>'
        f'<p class="ara-sub">{e(topic.blurb)}</p>'
        f'<a class="ara-btn ara-btn-ghost" href="/docs?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center;margin-top:8px">'
        '← All cards</a>'
        '<div class="doc-topic-layout">'
        '<div class="doc-topic-card">'
        + _doc_card(topic, token, hero=True, wrap="none") +
        '</div>'
        '<div class="doc-topic-body">'
        f'{sections_html}'
        '</div>'
        '</div>'
        '<div class="doc-strip">'
        '<div class="ara-eyebrow">Other cards</div>'
        f'<div class="doc-strip-row">{strip}</div>'
        '</div>'
        '</section>'
    )
    return _page(f"Docs · {topic.title}", "docs", body, token)


# ---------- gauntlet (contained-worker observation tab) ----------

def _collect_contained_sessions(state_root: Path) -> list[dict]:
    """Scan ``<state_root>/sessions/*`` for a ``contain.manifest.json``.
    Returns newest-first. Bounded to 50 sessions to keep the page cheap."""
    root = state_root / "sessions"
    if not root.exists():
        return []
    out: list[dict] = []
    for session_dir in sorted(root.iterdir(), reverse=True):
        manifest_path = session_dir / "contain.manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:  # noqa: BLE001
            continue
        entry = {
            "id": session_dir.name,
            "manifest": manifest,
            "dir": session_dir,
            "mtime": manifest_path.stat().st_mtime,
        }
        # Size of the contained tree — useful to spot runaway disk usage.
        contain_root = session_dir / "contain"
        if contain_root.exists():
            total = 0
            files = 0
            for p in contain_root.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                        files += 1
                    except OSError:
                        continue
            entry["bytes"] = total
            entry["files"] = files
        out.append(entry)
        if len(out) >= 50:
            break
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def _fmt_bytes(n: int) -> str:
    v: float = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def _tail_bytes(path: Path, limit: int = 16384) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - limit)
            f.seek(start)
            data = f.read()
    except OSError:
        return ""
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _render_session_containment_block(
    state_root: Path, session_id: str,
) -> str:
    """Manifest + tree + stdout/stderr log tails for a contained session.
    Returns '' when the session isn't contained. Embedded into the main
    session-detail page so Courtyard stops being a separate route."""
    session_dir = state_root / "sessions" / session_id
    manifest_path = session_dir / "contain.manifest.json"
    if not manifest_path.exists():
        return ''
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:  # noqa: BLE001
        return ''
    stdout_log = session_dir / "worker.stdout.log"
    stderr_log = session_dir / "worker.stderr.log"
    stdout_tail = _tail_bytes(stdout_log)
    stderr_tail = _tail_bytes(stderr_log)
    contain_root = session_dir / "contain"
    tree_rows: list[str] = []
    if contain_root.exists():
        entries: list[tuple[Path, int]] = []
        for p in sorted(contain_root.rglob("*")):
            if p.is_file():
                try:
                    entries.append((p, p.stat().st_size))
                except OSError:
                    continue
            if len(entries) >= 120:
                break
        for p, sz in entries:
            rel = p.relative_to(contain_root)
            tree_rows.append(
                '<tr>'
                f'<td class="mono" data-label="path">{e(str(rel))}</td>'
                f'<td class="num" data-label="size">{e(_fmt_bytes(sz))}</td>'
                '</tr>'
            )
    tree_html = (
        '<p class="ara-sub">(empty)</p>' if not tree_rows
        else '<table class="ara-table">'
             '<thead><tr><th>path (relative to contain/)</th>'
             '<th class="num">size</th></tr></thead>'
             f'<tbody>{"".join(tree_rows)}</tbody></table>'
    )
    return (
        '<h3 class="ara-eyebrow" style="margin-top:22px">'
        'Containment <span class="ara-sub">· manifest, tree, logs</span></h3>'
        '<details>'
        '<summary class="ara-sub" style="cursor:pointer">'
        f'manifest ({len(json.dumps(manifest))} bytes)</summary>'
        f'<pre class="ara-code">{e(json.dumps(manifest, indent=2, sort_keys=True))}</pre>'
        '</details>'
        '<details>'
        f'<summary class="ara-sub" style="cursor:pointer">contained tree ({len(tree_rows)} files)</summary>'
        f'<div style="margin-top:8px">{tree_html}</div>'
        '</details>'
        '<details><summary class="ara-sub" style="cursor:pointer">stderr tail</summary>'
        f'<pre class="ara-code" style="max-height:320px;overflow:auto">'
        f'{e(stderr_tail) or "(no stderr captured)"}</pre>'
        '</details>'
        '<details><summary class="ara-sub" style="cursor:pointer">stdout tail</summary>'
        f'<pre class="ara-code" style="max-height:320px;overflow:auto">'
        f'{e(stdout_tail) or "(no stdout captured)"}</pre>'
        '</details>'
    )


# ---------- shadows (A/B pairs from shadow_rewrite Rhema rules) ----------

def _collect_shadow_pairs(state_root: Path, limit: int = 50) -> list[dict]:
    """Walk every session's transcript.jsonl and group shadow-related
    records by ``pair_id``. Returns newest-first, capped at ``limit``."""
    sessions_root = state_root / "sessions"
    if not sessions_root.exists():
        return []
    pairs: dict[str, dict] = {}
    # Newest sessions first so we stop scanning early once we have enough.
    for session_dir in sorted(sessions_root.iterdir(), reverse=True):
        tx = session_dir / "transcript.jsonl"
        if not tx.exists():
            continue
        try:
            with tx.open() as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                rec = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            role = rec.get("role") or ""
            payload = rec.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            pair_id = payload.get("pair_id")
            if not pair_id or role not in (
                "shadow_link",
                "shadow_variant_request",
                "shadow_variant_response",
                "shadow_error",
            ):
                continue
            p = pairs.setdefault(pair_id, {
                "pair_id": pair_id,
                "session_id": session_dir.name,
                "ts": rec.get("ts") or 0,
                "link": None,
                "variant_request": None,
                "variant_response": None,
                "error": None,
                "channel": None,
            })
            p["ts"] = max(p["ts"], rec.get("ts") or 0)
            # Channel tag: any of the 4 records on a pair carry it
            # (shadow_link / shadow_variant_request / shadow_variant_response
            # / shadow_error). First non-empty wins.
            if not p["channel"] and rec.get("channel"):
                p["channel"] = rec["channel"]
            if role == "shadow_link":
                p["link"] = payload
            elif role == "shadow_variant_request":
                p["variant_request"] = payload
            elif role == "shadow_variant_response":
                p["variant_response"] = payload
            elif role == "shadow_error":
                p["error"] = payload
        if len(pairs) >= limit:
            break
    out = list(pairs.values())
    out.sort(key=lambda p: p["ts"], reverse=True)
    return out[:limit]


def _score_shadow_pair(pair: dict) -> float | None:
    """SequenceMatcher ratio of original response body vs variant body.
    Returns None when either side is missing (so we can't score the
    pair). Bounded 0.0–1.0; higher = more similar."""
    link = pair.get("link") or {}
    v_resp = pair.get("variant_response") or {}
    orig = link.get("original_response_body_text")
    variant = v_resp.get("body_text")
    if not isinstance(orig, str) or not isinstance(variant, str):
        return None
    if not orig or not variant:
        return None
    # SequenceMatcher is O(n*m) worst case; full Codex response bodies
    # (~200 KB SSE streams) wedge the /shadows render for minutes.
    # Cap at 4 KB per side and re-enable autojunk — readiness scoring
    # is a hint, not a precision metric, so a leading-prefix similarity
    # is enough to drive the badge.
    a = orig[:4_000]
    b = variant[:4_000]
    import difflib as _difflib
    return _difflib.SequenceMatcher(a=a, b=b).ratio()


def _aggregate_promote_readiness(
    pairs: list[dict], *,
    min_samples: int = 5,
    min_avg_similarity: float = 0.85,
) -> list[dict]:
    """Group shadow pairs by rule_id, compute avg similarity, and return
    a newest-first list of aggregates. Each entry shape:

        {"rule_id", "samples", "avg", "min", "max", "promote_ready"}

    ``promote_ready`` is a hint — the operator still clicks to promote.
    """
    buckets: dict[str, list[float]] = {}
    for p in pairs:
        rid = (p.get("link") or {}).get("rule_id") or ""
        if not rid:
            continue
        s = _score_shadow_pair(p)
        if s is None:
            continue
        buckets.setdefault(rid, []).append(s)
    out: list[dict] = []
    for rid, scores in buckets.items():
        n = len(scores)
        avg = sum(scores) / n if n else 0.0
        out.append({
            "rule_id": rid,
            "samples": n,
            "avg": avg,
            "min": min(scores) if scores else 0.0,
            "max": max(scores) if scores else 0.0,
            "promote_ready": n >= min_samples and avg >= min_avg_similarity,
        })
    out.sort(key=lambda a: (not a["promote_ready"], -a["avg"]))
    return out


def _render_shadow_spec_summary(spec: dict) -> str:
    """Render a shadow rewrite spec as scan-friendly fields.

    Large replacement prompts are summarized with counts and a wrapped
    preview instead of dumping JSON into a horizontal-scrolling code block.
    """
    if not isinstance(spec, dict) or not spec:
        return '<div class="ara-empty">(no rewrite spec captured)</div>'
    op = str(spec.get("op") or "unknown")
    field = spec.get("field")
    pattern = spec.get("pattern")
    replacement = spec.get("replacement")
    text = spec.get("text")
    chars = spec.get("chars")

    cards: list[str] = [
        '<div class="shadow-spec-item">'
        '<span>Operation</span>'
        f'<strong>{e(op)}</strong>'
        '</div>',
    ]
    if field:
        cards.append(
            '<div class="shadow-spec-item">'
            '<span>Target field</span>'
            f'<strong>{e(str(field))}</strong>'
            '</div>'
        )
    if chars is not None:
        cards.append(
            '<div class="shadow-spec-item">'
            '<span>Limit</span>'
            f'<strong>{e(str(chars))} chars</strong>'
            '</div>'
        )
    if pattern:
        cards.append(
            '<div class="shadow-spec-item">'
            '<span>Pattern</span>'
            f'<strong>{e(str(pattern))}</strong>'
            '</div>'
        )
    if replacement is not None:
        cards.append(
            '<div class="shadow-spec-item">'
            '<span>Replacement</span>'
            f'<strong>{e(str(replacement))}</strong>'
            '</div>'
        )

    preview = ""
    if isinstance(text, str) and text:
        line_count = len(text.splitlines())
        preview_text = text[:1_600]
        omitted = len(text) - len(preview_text)
        preview = (
            '<div class="shadow-spec-preview">'
            '<div class="shadow-spec-preview-head">'
            '<span class="ara-eyebrow">Replacement text</span>'
            f'<span class="rh-badge">{len(text):,} chars</span>'
            f'<span class="rh-badge">{line_count:,} lines</span>'
            '</div>'
            f'<div class="shadow-readable-block">{e(preview_text)}</div>'
            + (
                f'<p class="ara-sub">Preview truncated; {omitted:,} more chars in full spec.</p>'
                if omitted > 0 else ''
            )
            + '</div>'
        )

    unknown = [
        (k, v) for k, v in spec.items()
        if k not in {"op", "field", "pattern", "replacement", "text", "chars"}
    ]
    if unknown:
        cards.append(
            '<div class="shadow-spec-item shadow-spec-item-wide">'
            '<span>Other keys</span>'
            f'<strong>{e(", ".join(k for k, _ in unknown))}</strong>'
            '</div>'
        )

    return '<div class="shadow-spec-grid">' + "".join(cards) + '</div>' + preview


def _unified_diff_html(old: str, new: str, *, max_lines: int = 400) -> str:
    """Render a unified diff between two strings as HTML. Truncates each
    side to ``max_lines`` to keep large prompts from ballooning the page.
    Returns a ``<pre>`` block with inline spans for +/- lines."""
    import difflib as _difflib
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    if len(a) > max_lines:
        a = a[:max_lines] + [f"(… {len(a) - max_lines} more lines omitted)"]
    if len(b) > max_lines:
        b = b[:max_lines] + [f"(… {len(b) - max_lines} more lines omitted)"]
    diff_lines: list[str] = []
    for line in _difflib.unified_diff(a, b, fromfile="original", tofile="variant", n=2):
        cls = "shadow-diff-line"
        if line.startswith("+++") or line.startswith("---"):
            cls += " shadow-diff-file"
        elif line.startswith("@@"):
            cls += " shadow-diff-hunk"
        elif line.startswith("+"):
            cls += " shadow-diff-add"
        elif line.startswith("-"):
            cls += " shadow-diff-del"
        diff_lines.append(
            f'<div class="{cls}">{e(line.rstrip())}</div>'
        )
    if not diff_lines:
        return (
            '<div class="ara-empty" style="margin-top:6px">'
            '(no textual difference)</div>'
        )
    return '<div class="shadow-diff">' + "".join(diff_lines) + '</div>'


def _render_shadow_request_diff(link: dict) -> str:
    """Diff of the Rhema-targeted field on the request body. Renders
    only when ``shadow_link`` captured pre/post values (new pairs); old
    pairs fall back to an empty state without erroring."""
    field = link.get("field")
    if not field:
        return ""
    pre = link.get("original_field_value")
    post = link.get("mutated_field_value")
    if pre is None and post is None:
        return ""
    pre_s = pre if isinstance(pre, str) else json.dumps(pre, indent=2, sort_keys=True)
    post_s = post if isinstance(post, str) else json.dumps(post, indent=2, sort_keys=True)
    return (
        f'<div class="ara-eyebrow" style="margin-top:10px">'
        f'Request field diff · <code>{e(str(field))}</code></div>'
        + _unified_diff_html(pre_s, post_s)
    )


def _parse_shadow_response_events(text: str) -> list[dict]:
    events: list[dict] = []
    current_event = ""
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal current_event, data_lines
        if not current_event and not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
        event_name = current_event
        if not event_name and isinstance(parsed, dict):
            event_name = str(parsed.get("type") or "data")
        events.append({
            "event": event_name or "data",
            "data": parsed,
            "raw": raw,
        })
        current_event = ""
        data_lines = []

    for line in (text or "").splitlines():
        if line.startswith("event:"):
            flush()
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip():
            flush()
        else:
            data_lines.append(line)
    flush()
    return events


def _shadow_response_from_events(events: list[dict]) -> dict:
    for event in reversed(events):
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        response = data.get("response")
        if isinstance(response, dict):
            return response
        if data.get("object") == "response" or "output" in data or "output_text" in data:
            return data
    return {}


def _shadow_response_text(response: dict) -> str:
    text = response.get("output_text")
    if isinstance(text, str) and text:
        return text
    chunks: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        part_text = part.get("text")
                        if isinstance(part_text, str):
                            chunks.append(part_text)
                    elif isinstance(part, str):
                        chunks.append(part)
            elif isinstance(content, str):
                chunks.append(content)
    return "\n".join(chunks)


def _shadow_response_text_from_events(events: list[dict]) -> str:
    done_texts: list[str] = []
    deltas: list[str] = []
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        event_type = str(data.get("type") or event.get("event") or "")
        if event_type == "response.output_text.done":
            text = data.get("text")
            if isinstance(text, str):
                done_texts.append(text)
        elif event_type == "response.output_text.delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.content_part.done":
            part = data.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                done_texts.append(part["text"])
    if done_texts:
        unique_done_texts: list[str] = []
        for text in done_texts:
            if text not in unique_done_texts:
                unique_done_texts.append(text)
        return "\n".join(unique_done_texts)
    return "".join(deltas)


def _shadow_response_summary(text: str) -> dict:
    events = _parse_shadow_response_events(text)
    response = _shadow_response_from_events(events)
    event_names: list[str] = []
    for event in events:
        name = str(event.get("event") or "data")
        if name not in event_names:
            event_names.append(name)
    body_text = _shadow_response_text(response) or _shadow_response_text_from_events(events)
    has_structured_event = any(isinstance(event.get("data"), dict) for event in events)
    if not body_text and text and not has_structured_event:
        body_text = text
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    return {
        "bytes": len((text or "").encode("utf-8")),
        "event_count": len(events),
        "event_names": event_names,
        "response": response,
        "output_text": body_text,
        "status": response.get("status") if isinstance(response, dict) else None,
        "model": response.get("model") if isinstance(response, dict) else None,
        "id": response.get("id") if isinstance(response, dict) else None,
        "usage": usage,
        "error": response.get("error") if isinstance(response, dict) else None,
    }


def _render_shadow_response_card(label: str, summary: dict) -> str:
    status = summary.get("status") or "captured"
    model = summary.get("model") or "unknown"
    resp_id = summary.get("id") or "not recorded"
    usage = summary.get("usage") or {}
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = usage.get("total")
    output_text = summary.get("output_text") or ""
    preview = output_text[:1_600]
    omitted = len(output_text) - len(preview)
    event_names = summary.get("event_names") or []
    event_chips = "".join(
        f'<span>{e(str(name))}</span>' for name in event_names[:8]
    )
    if len(event_names) > 8:
        event_chips += f'<span>+{len(event_names) - 8} more</span>'
    error = summary.get("error")
    error_html = ""
    if error:
        error_text = error if isinstance(error, str) else json.dumps(error, sort_keys=True)
        error_html = (
            '<div class="shadow-response-error">'
            '<span class="ara-eyebrow">Error</span>'
            f'<div>{e(error_text)}</div>'
            '</div>'
        )
    return (
        '<article class="shadow-response-card">'
        '<div class="shadow-response-card-head">'
        f'<span class="ara-eyebrow">{e(label)}</span>'
        f'<span class="rh-badge">{e(str(status))}</span>'
        f'<span class="rh-badge">{int(summary.get("bytes") or 0):,} bytes</span>'
        f'<span class="rh-badge">{int(summary.get("event_count") or 0):,} events</span>'
        '</div>'
        '<div class="shadow-response-fields">'
        '<div><span>Model</span>'
        f'<strong>{e(str(model))}</strong></div>'
        '<div><span>Response ID</span>'
        f'<strong>{e(str(resp_id))}</strong></div>'
        '<div><span>Usage</span>'
        f'<strong>{e(str(total_tokens)) + " tokens" if total_tokens is not None else "not recorded"}</strong></div>'
        '</div>'
        + (
            '<div class="shadow-response-preview">'
            '<div class="shadow-spec-preview-head">'
            '<span class="ara-eyebrow">Output preview</span>'
            f'<span class="rh-badge">{len(output_text):,} chars</span>'
            '</div>'
            f'<div class="shadow-readable-block">{e(preview)}</div>'
            + (
                f'<p class="ara-sub">Preview truncated; {omitted:,} more chars in full response.</p>'
                if omitted > 0 else ''
            )
            + '</div>'
            if output_text else
            '<div class="ara-empty">(no assistant output text found in captured response)</div>'
        )
        + (
            f'<div class="shadow-event-strip">{event_chips}</div>'
            if event_chips else ''
        )
        + error_html
        + '</article>'
    )


def _render_shadow_response_diff(link: dict, v_resp: dict) -> str:
    """Side-by-side diff of original response body vs variant response
    body. Falls back to a raw variant body (prior behavior) when the
    link record doesn't carry the original text."""
    orig_text = link.get("original_response_body_text") or ""
    variant_text = (v_resp or {}).get("body_text") or ""
    if not variant_text:
        return (
            '<div class="ara-empty">'
            '(no variant response captured — in-flight or errored)</div>'
        )
    variant_summary = _shadow_response_summary(variant_text)
    if not orig_text:
        return (
            '<div class="shadow-response-compare shadow-response-compare-single">'
            + _render_shadow_response_card("Variant response", variant_summary)
            + '</div>'
            '<div class="ara-sub" style="margin-top:4px">'
            '(original response body not captured on this pair — '
            'only the variant is shown)</div>'
        )
    original_summary = _shadow_response_summary(orig_text)
    html = (
        '<div class="shadow-response-compare">'
        + _render_shadow_response_card("Original response", original_summary)
        + _render_shadow_response_card("Variant response", variant_summary)
        + '</div>'
    )
    if orig_text == variant_text:
        return html
    return (
        html +
        '<details class="shadow-raw-diff-drawer">'
        '<summary>Line diff</summary>'
        + _unified_diff_html(orig_text, variant_text)
        + '</details>'
    )


def _shadow_pair_response_delta(pair: dict) -> int:
    link = pair.get("link") or {}
    v_resp = pair.get("variant_response") or {}
    original_bytes = int(link.get("original_resp_bytes") or 0)
    variant_bytes = len(v_resp.get("body_text", "") or "")
    return abs(variant_bytes - original_bytes)


def _shadow_pair_request_delta(pair: dict) -> int:
    link = pair.get("link") or {}
    v_req = pair.get("variant_request") or {}
    if not v_req:
        return 0
    return int(link.get("original_req_bytes") or 0) - len(json.dumps(v_req.get("body", {})))


def _render_shadow_ops_bar(
    *, token: str, channel_filter: str | None, status_filter: str,
    rule_filter: str, sort_key: str, rule_ids: list[str],
) -> str:
    def selected(value: str, current: str) -> str:
        return ' selected' if value == current else ''

    rule_options = ['<option value="">All rules</option>'] + [
        f'<option value="{e(rule_id)}"{selected(rule_id, rule_filter)}>{e(rule_id)}</option>'
        for rule_id in rule_ids
    ]
    hidden_channel = (
        f'<input type="hidden" name="channel" value="{e(channel_filter)}">'
        if channel_filter else ''
    )
    return (
        '<form class="shadow-ops-bar" method="get" action="/rhema">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        '<input type="hidden" name="tab" value="shadows">'
        + hidden_channel +
        '<label><span>Status</span><select name="status">'
        f'<option value="all"{selected("all", status_filter)}>All statuses</option>'
        f'<option value="ok"{selected("ok", status_filter)}>OK only</option>'
        f'<option value="errors"{selected("errors", status_filter)}>Errors only</option>'
        f'<option value="promote"{selected("promote", status_filter)}>Promote-ready</option>'
        f'<option value="enforcing"{selected("enforcing", status_filter)}>Enforcing</option>'
        '</select></label>'
        '<label><span>Rule</span><select name="rule">'
        + "".join(rule_options) +
        '</select></label>'
        '<label><span>Sort</span><select name="sort">'
        f'<option value="newest"{selected("newest", sort_key)}>Newest first</option>'
        f'<option value="delta"{selected("delta", sort_key)}>Largest response delta</option>'
        f'<option value="request"{selected("request", sort_key)}>Largest request trim</option>'
        f'<option value="rule"{selected("rule", sort_key)}>Rule name</option>'
        '</select></label>'
        '<button class="ara-btn ara-btn-ghost" type="submit">Apply</button>'
        f'<a class="ara-btn ara-btn-ghost" href="/rhema?t={e(token)}&tab=shadows">Reset</a>'
        '</form>'
    )


def _render_channel_filter_bar(
    *, current: str, base_url: str, token: str, available: list[str],
) -> str:
    """Filter pill bar — one pill per channel observed in the data,
    plus an "all" reset and an "untagged" pill. Active pill is rendered
    with a stronger color background."""
    def _pill(channel: str | None, label: str, color: str) -> str:
        is_active = (current or "") == (channel or "")
        href = f"{base_url}?t={e(token)}"
        if channel:
            href += f"&channel={e(channel)}"
        if is_active:
            style = (
                f'background:color-mix(in srgb,{color} 70%,transparent);'
                f'color:#fff;border:1px solid {color};'
            )
        else:
            style = (
                f'background:color-mix(in srgb,{color} 15%,transparent);'
                f'color:{color};'
                f'border:1px solid color-mix(in srgb,{color} 35%,transparent);'
            )
        return (
            f'<a href="{href}" '
            f'style="text-decoration:none;padding:3px 10px;font-size:11px;'
            f'border-radius:12px;font-family:var(--font-mono);'
            f'letter-spacing:0.04em;text-transform:uppercase;{style}">'
            f'{e(label)}</a>'
        )
    parts = [_pill(None, "all", "#94a3b8")]
    for cid in available:
        if cid == "_untagged":
            parts.append(_pill("_untagged", "untagged", "#888888"))
        else:
            parts.append(_pill(cid, armors_mod.channel_label(cid),
                                armors_mod.channel_color(cid)))
    return (
        '<div style="display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 12px 0">'
        + "".join(parts) + '</div>'
    )


def _render_shadows_body(
    state_root: Path, token: str,
    flash: str | None = None, flash_level: str = "info",
    channel_filter: str | None = None,
    status_filter: str | None = None,
    rule_filter: str | None = None,
    sort_key: str | None = None,
) -> str:
    all_pairs = _collect_shadow_pairs(state_root)
    # Determine which channels are present in the dataset for the
    # filter bar, then narrow ``pairs`` if a filter is set.
    seen_channels: list[str] = []
    has_untagged = False
    seen_set: set = set()
    for p in all_pairs:
        ch = p.get("channel")
        if ch and ch not in seen_set:
            seen_set.add(ch)
            seen_channels.append(ch)
        if not ch:
            has_untagged = True
    if has_untagged:
        seen_channels.append("_untagged")
    if channel_filter == "_untagged":
        channel_pairs = [p for p in all_pairs if not p.get("channel")]
    elif channel_filter:
        channel_pairs = [p for p in all_pairs if p.get("channel") == channel_filter]
    else:
        channel_pairs = all_pairs
    flash_html = ""
    if flash:
        cls = "--tertiary" if flash_level == "ok" else "--error" if flash_level == "bad" else "--primary"
        flash_html = (
            f'<div class="ara-callout" '
            f'style="border-left:3px solid var({cls});margin-bottom:16px">{e(flash)}</div>'
        )
    if not all_pairs:
        body = (
            '<section>'
            + flash_html
            + '<div class="ara-eyebrow">A/B</div>'
            '<h2 class="ara-h">Shadows</h2>'
            '<p class="ara-sub">No shadow pairs captured yet. Add a Rhema '
            'rule with <code>action: shadow_rewrite</code> and a '
            '<code>rewrite_with</code> spec, then run a turn. The rewrite '
            'goes out in parallel with the original; both responses land '
            'here for side-by-side comparison.</p>'
            '<div class="ara-empty" style="margin-top:16px">'
            'Example spec — trim the agent\'s 35 KB system prompt to 5 KB:'
            '<pre class="ara-code" style="margin-top:8px">{\n'
            '  "scope": "request",\n'
            '  "pattern": "chatgpt.com/backend-api/codex/responses",\n'
            '  "pattern_kind": "substring",\n'
            '  "action": "shadow_rewrite",\n'
            '  "rewrite_with": {\n'
            '    "op": "trim_to_chars",\n'
            '    "field": "instructions",\n'
            '    "chars": 5000\n'
            '  },\n'
            '  "reason": "A/B: does 5K system prompt hold up?"\n'
            '}</pre></div>'
            '</section>'
        )
        return body

    # Resolve current enforcement status per rule_id so we can render
    # "Promote" only when the rule is still a shadow. For abs-live-<name>
    # we read the armor's absolute_enforce flag; for operator rules we
    # load rhema.json once.
    from .. import rhema as _rhema_mod
    from .. import armors as _armors_mod
    op_rules_by_id = {r.id: r for r in _rhema_mod.load(state_root)}

    def _is_enforcing(rule_id: str) -> bool:
        if not rule_id:
            return False
        if rule_id.startswith("abs-live-"):
            name = rule_id[len("abs-live-"):]
            return _armors_mod.is_absolute_enforcing(state_root, name)
        r = op_rules_by_id.get(rule_id)
        return bool(r and r.action == "rewrite")

    rule_filter = rule_filter or ""
    status_filter = status_filter if status_filter in {
        "all", "ok", "errors", "promote", "enforcing",
    } else "all"
    sort_key = sort_key if sort_key in {"newest", "delta", "request", "rule"} else "newest"

    readiness_for_filters = _aggregate_promote_readiness(channel_pairs)
    ready_rule_ids = {
        str(r.get("rule_id") or "")
        for r in readiness_for_filters
        if r.get("promote_ready")
    }
    available_rule_ids = sorted({
        str((p.get("link") or {}).get("rule_id") or "")
        for p in channel_pairs
        if (p.get("link") or {}).get("rule_id")
    })

    pairs = channel_pairs
    if rule_filter:
        pairs = [
            p for p in pairs
            if str((p.get("link") or {}).get("rule_id") or "") == rule_filter
        ]
    if status_filter == "errors":
        pairs = [p for p in pairs if p.get("error")]
    elif status_filter == "ok":
        pairs = [p for p in pairs if not p.get("error")]
    elif status_filter == "promote":
        pairs = [
            p for p in pairs
            if str((p.get("link") or {}).get("rule_id") or "") in ready_rule_ids
            and not _is_enforcing(str((p.get("link") or {}).get("rule_id") or ""))
        ]
    elif status_filter == "enforcing":
        pairs = [
            p for p in pairs
            if _is_enforcing(str((p.get("link") or {}).get("rule_id") or ""))
        ]

    if sort_key == "delta":
        pairs.sort(key=lambda p: (_shadow_pair_response_delta(p), p.get("ts") or 0), reverse=True)
    elif sort_key == "request":
        pairs.sort(key=lambda p: (_shadow_pair_request_delta(p), p.get("ts") or 0), reverse=True)
    elif sort_key == "rule":
        pairs.sort(key=lambda p: (str((p.get("link") or {}).get("rule_id") or ""), -(p.get("ts") or 0)))
    else:
        pairs.sort(key=lambda p: p.get("ts") or 0, reverse=True)

    rows: list[str] = []
    for p in pairs:
        link = p.get("link") or {}
        v_req = p.get("variant_request") or {}
        v_resp = p.get("variant_response") or {}
        err = p.get("error")
        original_bytes = link.get("original_resp_bytes", 0)
        original_status = link.get("original_status", "?")
        variant_bytes = len(v_resp.get("body_text", "") or "")
        variant_status = v_resp.get("status", "?") if v_resp else "?"
        saved = (
            link.get("original_req_bytes", 0)
            - len(json.dumps(v_req.get("body", {})))
            if v_req else 0
        )
        spec = link.get("spec") or v_req.get("spec") or {}
        status_chip = (
            '<span class="rh-badge" style="background:color-mix(in srgb,var(--error) 20%,transparent)">ERR</span>'
            if err else '<span class="rh-badge">ok</span>'
        )
        rule_id_str = str(link.get("rule_id") or "")
        enforcing_now = _is_enforcing(rule_id_str)
        if enforcing_now:
            promote_html = (
                '<span class="rh-badge" '
                'style="background:color-mix(in srgb,var(--tertiary) 25%,transparent)">'
                'ENFORCING</span>'
            )
        elif rule_id_str:
            confirm_msg = (
                f"Promote {rule_id_str} to an enforcing rewrite? "
                "The mutated request will replace the original on the wire; "
                "the agent will see the response to the rewritten request."
            )
            promote_html = (
                f'<form method="POST" action="/shadows/promote?t={e(token)}" '
                f'style="display:inline;margin:0" '
                f'onsubmit="return confirm({json.dumps(confirm_msg)})">'
                f'<input type="hidden" name="rule_id" value="{e(rule_id_str)}">'
                f'<button type="submit" class="ara-btn ara-btn-primary" '
                f'style="padding:2px 8px;font-size:0.85em">Promote →</button>'
                '</form>'
            )
        else:
            promote_html = ""
        ch_pill = (
            _render_channel_pills([p["channel"]], compact=True)
            if p.get("channel") else ""
        )
        saved_label = _fmt_bytes(saved) if saved else "0 B"
        rows.append(
            '<details class="shadow-pair-card">'
            '<summary class="shadow-pair-summary">'
            '<div class="shadow-pair-main">'
            f'<span class="shadow-pair-id mono">{e(p["pair_id"])}</span>'
            f'<span class="shadow-pair-age mono">{e(_fmt_ago(p["ts"]))} ago</span>'
            + (ch_pill or '')
            + f'{status_chip}'
            f'<span class="rh-badge">rule: {e(rule_id_str)}</span>'
            '</div>'
            '<div class="shadow-pair-metrics">'
            f'<span><strong>{e(str(original_status))}</strong><small>orig status</small></span>'
            f'<span><strong>{e(_fmt_bytes(original_bytes))}</strong><small>orig</small></span>'
            f'<span><strong>{e(str(variant_status))}</strong><small>variant status</small></span>'
            f'<span><strong>{e(_fmt_bytes(variant_bytes))}</strong><small>variant</small></span>'
            f'<span><strong>{e(saved_label)}</strong><small>req delta</small></span>'
            '</div>'
            + (f'<div class="shadow-pair-action">{promote_html}</div>' if promote_html else '')
            + '</summary>'
            '<div class="shadow-pair-body">'
            f'<div class="ara-eyebrow">Spec</div>'
            + _render_shadow_spec_summary(spec)
            + (f'<div class="ara-eyebrow" style="margin-top:10px">Error</div>'
               f'<div class="shadow-readable-block">{e(json.dumps(err, indent=2, sort_keys=True))}</div>' if err else '')
            + _render_shadow_request_diff(link)
            + '<div class="ara-eyebrow" style="margin-top:10px">Response diff</div>'
            + _render_shadow_response_diff(link, v_resp)
            + f'<div class="ara-sub" style="margin-top:8px">session · '
            f'<a href="/sessions/{e(p["session_id"])}?t={e(token)}" class="mono">'
            f'{e(p["session_id"][:12])}</a></div>'
            '</div>'
            '</details>'
        )

    readiness = _aggregate_promote_readiness(pairs)
    readiness_rows: list[str] = []
    for r in readiness:
        if _is_enforcing(r["rule_id"]):
            badge = ('<span class="rh-badge" style="background:'
                     'color-mix(in srgb,var(--tertiary) 25%,transparent)">'
                     'ENFORCING</span>')
            promote_cell = badge
        elif r["promote_ready"]:
            confirm = (
                f"Promote {r['rule_id']} to an enforcing rewrite? "
                f"Avg similarity {r['avg']:.1%} across {r['samples']} pairs."
            )
            promote_cell = (
                f'<form method="POST" action="/shadows/promote?t={e(token)}" '
                f'style="display:inline;margin:0" '
                f'onsubmit="return confirm({json.dumps(confirm)})">'
                f'<input type="hidden" name="rule_id" value="{e(r["rule_id"])}">'
                f'<button type="submit" class="ara-btn ara-btn-primary" '
                f'style="padding:2px 10px;font-size:0.85em">Promote →</button>'
                '</form>'
            )
        else:
            promote_cell = (
                '<span class="ara-sub" style="font-size:11px">'
                'more samples needed</span>'
            )
        readiness_rows.append(
            "<tr>"
            f'<td class="mono">{e(r["rule_id"])}</td>'
            f'<td>{r["samples"]}</td>'
            f'<td>{r["avg"]:.1%}</td>'
            f'<td class="ara-sub">{r["min"]:.1%} – {r["max"]:.1%}</td>'
            f'<td>{promote_cell}</td>'
            "</tr>"
        )
    # Promote-readiness table folds into a disclosure so it doesn't
    # shove the pair list below the fold. Opens to full view when
    # there's a promote-ready candidate (pulls attention up).
    any_ready = any(r.get("promote_ready") for r in readiness)
    readiness_html = (
        f'<details class="shadow-readiness-drawer"{" open" if any_ready else ""}>'
        '<summary class="shadow-readiness-summary">'
        '<span class="ara-eyebrow">Promote readiness</span>'
        f'<strong>{len(readiness)} rule(s)</strong>'
        '<span class="rh-badge">auto-scored</span>'
        + ('<span class="rh-badge shadow-ready">promote-ready</span>' if any_ready else '')
        + '</summary>'
        '<div class="shadow-readiness-body">'
        '<p class="ara-sub">Per-rule average response-body similarity '
        'between original and variant. A rule becomes "promote-ready" '
        'once it has ≥5 pairs with mean similarity ≥85%. The hint is '
        'advisory — operator still clicks to promote.</p>'
        '<table class="ara-table">'
        '<thead><tr><th>Rule ID</th><th>Samples</th>'
        '<th>Avg similarity</th><th>Min – Max</th><th></th></tr></thead>'
        f'<tbody>{"".join(readiness_rows)}</tbody></table>'
        '</div>'
        '</details>'
    ) if readiness_rows else ""

    filter_bar = (
        _render_channel_filter_bar(
            current=channel_filter or "",
            base_url="/shadows", token=token,
            available=seen_channels,
        )
        if seen_channels else ""
    )
    ops_bar = _render_shadow_ops_bar(
        token=token,
        channel_filter=channel_filter,
        status_filter=status_filter,
        rule_filter=rule_filter,
        sort_key=sort_key,
        rule_ids=available_rule_ids,
    )
    rule_ids = {
        str((p.get("link") or {}).get("rule_id") or "")
        for p in pairs if (p.get("link") or {}).get("rule_id")
    }
    err_count = sum(1 for p in pairs if p.get("error"))
    enforcing_count = sum(
        1 for rid in rule_ids if _is_enforcing(rid)
    )
    ready_count = sum(1 for r in readiness if r.get("promote_ready"))
    body = (
        '<section>'
        + flash_html
        + '<div class="shadow-dashboard">'
        + '<div class="ara-eyebrow">A/B</div>'
        '<h2 class="ara-h">Shadows</h2>'
        '<p class="ara-sub">Paired requests from <code>shadow_rewrite</code> '
        'Rhema rules — original went upstream (agent saw this), variant '
        'ran in parallel for comparison. Agent behavior unchanged.</p>'
        + filter_bar
        + ops_bar
        + '<div class="shadow-overview">'
        f'<div><strong>{len(pairs)}</strong><span>Pairs captured</span></div>'
        f'<div><strong>{len(rule_ids)}</strong><span>Rules observed</span></div>'
        f'<div><strong>{ready_count}</strong><span>Promote-ready</span></div>'
        f'<div><strong>{enforcing_count}</strong><span>Enforcing</span></div>'
        f'<div><strong>{err_count}</strong><span>Errors</span></div>'
        '</div>'
        + readiness_html
        + '<div class="shadow-pair-list">'
        + (
            "".join(rows)
            if rows else
            '<div class="ara-empty">No shadow pairs match the current filters.</div>'
        )
        + '</div>'
        + '</div>'
        + '</section>'
    )
    return body


def render_shadows(
    state_root: Path, token: str,
    flash: str | None = None, flash_level: str = "info",
    channel_filter: str | None = None,
    status_filter: str | None = None,
    rule_filter: str | None = None,
    sort_key: str | None = None,
) -> str:
    """Legacy page wrapper — Shadows is now the third tab on /rhema.
    Route at /shadows still calls this wrapper so bookmarks keep rendering."""
    body = _render_shadows_body(
        state_root, token, flash=flash, flash_level=flash_level,
        channel_filter=channel_filter,
        status_filter=status_filter,
        rule_filter=rule_filter,
        sort_key=sort_key,
    )
    return _page("Rhema · Shadows", "rhema", body, token)
