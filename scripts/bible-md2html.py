"""Convert an ARA-bible markdown doc to a self-contained, workbench-themed
HTML page combining two design languages:

  - **ARA armor-card hero** (trading card): holographic foil border,
    rarity stars, sigil glow, ability grid, and passives line.
  - **Arctic Base section pattern**: eyebrow + h2 + lede, dashed
    section dividers, polished tables, and color-mixed accents.

References --wb-* CSS variables so the bench's ARA-tuneup theme picks
it up automatically; falls back to ARA armory tokens otherwise.

Per-doc card metadata (rarity tier, abilities, passives) is keyed off
the file basename — see ``CARD_META`` below. Add an entry per new
subsystem doc.

Usage:  python3 scripts/bible-md2html.py <input.md> <output.html>
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Per-doc trading-card metadata.
# ---------------------------------------------------------------------------
# Rarities: legendary | epic | rare | uncommon | common
# Each card needs: tier_roman, abilities (list of (label, sub)), passives.
# Sigils: an SVG path data string drawn inside the bc-sigil circle.
# ---------------------------------------------------------------------------

_SIGIL_DOC = (
    '<path d="M4 4 V20 A2 2 0 0 0 6 22 H20 V6 A2 2 0 0 0 18 4 H6 '
    'A2 2 0 0 0 4 6 Z"/><path d="M8 8 H16 M8 12 H16 M8 16 H13"/>'
)
_SIGIL_SHIELD = (
    '<path d="M12 2 L20 6 V13 C20 18 16 21 12 22 C8 21 4 18 4 13 V6 Z"/>'
    '<path d="M12 8 V16 M8 12 H16"/>'
)
_SIGIL_GAVEL = (
    '<path d="M14 4 L20 10 M11 7 L17 13 M9 9 L15 15 M4 14 L10 20 '
    'M3 21 L7 17 M14 4 L17 7"/>'
)
_SIGIL_LAYERS = (
    '<path d="M12 3 L21 8 L12 13 L3 8 Z"/>'
    '<path d="M3 12 L12 17 L21 12"/>'
    '<path d="M3 16 L12 21 L21 16"/>'
)
_SIGIL_NETWORK = (
    '<circle cx="12" cy="12" r="3"/>'
    '<circle cx="4" cy="4" r="2"/><circle cx="20" cy="4" r="2"/>'
    '<circle cx="4" cy="20" r="2"/><circle cx="20" cy="20" r="2"/>'
    '<path d="M6 5 L10 11 M14 11 L18 5 M14 13 L18 19 M10 13 L6 19"/>'
)


CARD_META: dict[str, dict] = {
    "00-index": {
        "rarity": "legendary",
        "stars": 5,
        "tier_roman": "TIER V",
        "sigil": _SIGIL_DOC,
        "abilities": [
            ("Master index", "Every source file, every doc"),
            ("Checkbox-tracked", "Marked done only when read"),
            ("Three layers", "MD source · HTML render · workbench"),
            ("Living", "Updates as work progresses"),
        ],
        "passives": "Canonical task list for the end-to-end review.",
    },
    "01-overview": {
        "rarity": "legendary",
        "stars": 5,
        "tier_roman": "TIER V",
        "sigil": _SIGIL_SHIELD,
        "abilities": [
            ("Userspace MAC", "Filesystem · command · network"),
            ("Three layers", "WALLS · Rhema · ABSOLUTE"),
            ("0.1.0 release", "2026-04-20"),
            ("63K LOC", "Python · Node · libc shim"),
        ],
        "passives": "Agent Ready Armor — supervisory layer for AI runtimes.",
    },
    "02-policy-stack": {
        "rarity": "epic",
        "stars": 4,
        "tier_roman": "TIER IV",
        "sigil": _SIGIL_GAVEL,
        "abilities": [
            ("Bulwark DSL", "Single live policy file"),
            ("Five actions", "allow · deny · approval · rewrite · shadow"),
            ("Ten layers", "net · fs · nav · perm · exec · url · env · ask · reply · oc"),
            ("Scope-bucketed", "global vs armor precedence"),
        ],
        "passives": "Unified rule layer; WALLS / Rhema / overrides absorbed.",
    },
    "03-cli-and-launchers": {
        "rarity": "rare",
        "stars": 3,
        "tier_roman": "TIER III",
        "sigil": _SIGIL_DOC,
        "abilities": [
            ("ara armory", "Web dashboard"),
            ("ara openclaw", "OpenClaw under supervision"),
            ("ara claude", "Claude Code under supervision"),
            ("ara forge", "Weapon-forge orchestrator"),
        ],
        "passives": "Operator entry points into ARA's runtime.",
    },
    "04-supervisor-and-launch": {
        "rarity": "epic",
        "stars": 4,
        "tier_roman": "TIER IV",
        "sigil": _SIGIL_NETWORK,
        "abilities": [
            ("asyncio IPC hub", "Owns the policy loop"),
            ("Containment", "HOME / TMPDIR / XDG isolation"),
            ("LD_PRELOAD", "libc shim injection"),
            ("Heartbeat watchdog", "Compromise detection"),
        ],
        "passives": "The process that wraps every supervised worker.",
    },
    "05-transcript-and-audit": {
        "rarity": "rare",
        "stars": 3,
        "tier_roman": "TIER III",
        "sigil": _SIGIL_LAYERS,
        "abilities": [
            ("Hash-chained", "Tamper-evident JSONL"),
            ("Per-session", "Transcript + audit + events"),
            ("Attestable export", "tar.gz + HMAC-SHA256 signature"),
            ("Live SSE", "/rhema/events stream"),
        ],
        "passives": "The receipt layer — every turn provably recorded.",
    },
    "blacksmith-hammer-v0": {
        "rarity": "legendary",
        "stars": 5,
        "tier_roman": "PROPOSAL",
        "sigil": _SIGIL_GAVEL,
        "abilities": [
            ("Forge-only posture", "deny everything · narrow allows"),
            ("Six extract channels", "proc · conc · fact_shape · store · line · fact"),
            ("Hard contain required", "exec-approvals proxy · WS tap · HOME isolation"),
            ("ABSOLUTE enforced", "Live rewrite, not shadow"),
        ],
        "passives": "Default weapon. Stops the forge agent from doing anything but forging.",
    },
    "10-runtime-leads": {
        "rarity": "epic",
        "stars": 4,
        "tier_roman": "TIER IV",
        "sigil": _SIGIL_LAYERS,
        "abilities": [
            ("SM-18 decay", "exp(-elapsed/stability_s)"),
            ("Five-signal priority", "Pure function · no LLM"),
            ("Closure gate", "Crit/high · R<0.5 blocks close"),
            ("Death detectors", "Aged · contradicted · superseded"),
        ],
        "passives": "Hypothesis ledger with cognitive-decay scheduling.",
    },
    "09-runtime-facts": {
        "rarity": "rare",
        "stars": 3,
        "tier_roman": "TIER III",
        "sigil": _SIGIL_LAYERS,
        "abilities": [
            ("Append-only JSONL", "Per-session facts.jsonl"),
            ("Content-hash dedup", "Idempotent on (predicate,value,evidence)"),
            ("Five neighbors", "Causal · temporal · evidential · semantic"),
            ("Trace-or-die", "Must link evidence_refs OR depends_on"),
        ],
        "passives": "The agent's belief journal — every assertion traces to evidence.",
    },
    "08-runtime-channels": {
        "rarity": "epic",
        "stars": 4,
        "tier_roman": "TIER IV",
        "sigil": _SIGIL_LAYERS,
        "abilities": [
            ("Activation IDs", "Atomic mint · per-swap rotation"),
            ("Sync hash chain", "fsync per line · path-keyed registry"),
            ("Channel base class", "validate · execute · audit"),
            ("Two-tier registry", "Class catalog · live instances"),
        ],
        "passives": "The substrate every typed channel rides on.",
    },
    "07-armory-web-app": {
        "rarity": "epic",
        "stars": 4,
        "tier_roman": "TIER IV",
        "sigil": _SIGIL_DOC,
        "abilities": [
            ("stdlib http.server", "Threaded · localhost-bound"),
            ("Two-token auth", "Admin · viewer · cookie or ?t="),
            ("Two SSE streams", "/events · /rhema/events"),
            ("Attestable export", ".tgz + HMAC-SHA256 .sig"),
        ],
        "passives": "The operator's window into ARA — and the bootstrap that wires every sidecar.",
    },
    "06-classifiers-canary-mitm": {
        "rarity": "legendary",
        "stars": 5,
        "tier_roman": "TIER V",
        "sigil": _SIGIL_NETWORK,
        "abilities": [
            ("Threaded MITM", "Per-host leaf certs · 5 providers"),
            ("Bulwark + Rhema", "url · ask · reply · acp"),
            ("Honeytokens", "openclaw_pair · aws · stripe · github"),
            ("Shadow rewrites", "ABSOLUTE-live + operator A/B"),
        ],
        "passives": "The wire-level interception layer.",
    },
}

_DEFAULT_CARD = {
    "rarity": "uncommon",
    "stars": 3,
    "tier_roman": "TIER III",
    "sigil": _SIGIL_DOC,
    "abilities": [],
    "passives": "End-to-end source review.",
}


# ---------------------------------------------------------------------------
# Markdown subset
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_HR_RE = re.compile(r"^-{3,}\s*$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.+)$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return out or "section"


def _eyebrow_for_h2(text: str, doc_section: str) -> str:
    short = re.split(r"[(:—\-—]", text, 1)[0].strip()
    short = re.sub(r"\s+", " ", short).upper()
    return f"// {doc_section.upper()} · {short}"


def _inline(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        m = _INLINE_CODE_RE.match(s, i)
        if m:
            out.append(f"<code>{_esc(m.group(1))}</code>")
            i = m.end()
            continue
        out.append(_esc(s[i]))
        i += 1
    text = "".join(out)
    text = _LINK_RE.sub(
        lambda m: f'<a href="{_esc(m.group(2))}">{m.group(1)}</a>', text,
    )
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def _close_blocks(state: dict, out: list[str]) -> None:
    if state.get("list"):
        out.append(f"</{state['list']}>")
        state["list"] = None
    if state.get("blockquote"):
        out.append("</blockquote>")
        state["blockquote"] = False
    if state.get("table"):
        out.append("</tbody></table></div>")
        state["table"] = False


def _render_table_row(cells: list[str], *, header: bool) -> str:
    tag = "th" if header else "td"
    return (
        "<tr>"
        + "".join(f"<{tag}>{_inline(c.strip())}</{tag}>" for c in cells)
        + "</tr>"
    )


def md_to_sections(md: str, doc_section: str) -> tuple[str, list[str]]:
    lines = md.splitlines()
    sections: list[str] = []
    pre_h1_skipped = False
    pre_h2_buf: list[str] = []
    cur_section_title: Optional[str] = None
    cur_section_lines: list[str] = []

    def flush_section():
        nonlocal cur_section_title, cur_section_lines
        if cur_section_title is None:
            return
        eyebrow = _eyebrow_for_h2(cur_section_title, doc_section)
        body = _render_block_lines(cur_section_lines)
        sid = _slug(cur_section_title)
        sections.append(
            f'<section id="{sid}">\n'
            f'  <div class="eyebrow">{_esc(eyebrow)}</div>\n'
            f'  <h2>{_inline(cur_section_title)}</h2>\n'
            f'{body}\n'
            f'</section>'
        )
        cur_section_title = None
        cur_section_lines = []

    for line in lines:
        m = _HEADING_RE.match(line)
        if m and not pre_h1_skipped and len(m.group(1)) == 1:
            pre_h1_skipped = True
            continue
        if m and pre_h1_skipped and len(m.group(1)) == 2:
            flush_section()
            cur_section_title = m.group(2).strip()
            continue
        if cur_section_title is None:
            pre_h2_buf.append(line)
        else:
            cur_section_lines.append(line)
    flush_section()

    lede = _render_block_lines(pre_h2_buf)
    return lede, sections


def _render_block_lines(lines: list[str]) -> str:
    out: list[str] = []
    state = {"list": None, "blockquote": False, "table": False, "fence": False}
    fence_lang = ""
    fence_buf: list[str] = []
    para_buf: list[str] = []

    def flush_para(buf: list[str]) -> None:
        body = " ".join(_inline(line.strip()) for line in buf if line.strip())
        if body:
            out.append(f"<p>{body}</p>")

    i = 0
    while i < len(lines):
        line = lines[i]
        if state["fence"]:
            if _FENCE_RE.match(line):
                code = "\n".join(fence_buf)
                cls = f' class="lang-{_esc(fence_lang)}"' if fence_lang else ""
                out.append(f"<pre><code{cls}>{_esc(code)}</code></pre>")
                fence_buf = []
                state["fence"] = False
            else:
                fence_buf.append(line)
            i += 1
            continue

        m = _FENCE_RE.match(line)
        if m:
            flush_para(para_buf); para_buf = []
            _close_blocks(state, out)
            state["fence"] = True
            fence_lang = m.group(1)
            fence_buf = []
            i += 1
            continue

        if not line.strip():
            flush_para(para_buf); para_buf = []
            _close_blocks(state, out)
            i += 1
            continue

        if _HR_RE.match(line):
            flush_para(para_buf); para_buf = []
            _close_blocks(state, out)
            out.append("<hr>")
            i += 1
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush_para(para_buf); para_buf = []
            _close_blocks(state, out)
            level = min(6, len(m.group(1)))
            text = _inline(m.group(2))
            sid = _slug(m.group(2))
            out.append(f'<h{level} id="{sid}">{text}</h{level}>')
            i += 1
            continue

        m = _BLOCKQUOTE_RE.match(line)
        if m:
            flush_para(para_buf); para_buf = []
            if state["list"]:
                out.append(f"</{state['list']}>"); state["list"] = None
            if state["table"]:
                out.append("</tbody></table></div>"); state["table"] = False
            if not state["blockquote"]:
                out.append("<blockquote>"); state["blockquote"] = True
            quote_line = m.group(1)
            if quote_line.strip():
                out.append(f"<p>{_inline(quote_line)}</p>")
            i += 1
            continue
        elif state["blockquote"]:
            out.append("</blockquote>"); state["blockquote"] = False

        m = _LIST_RE.match(line)
        if m:
            flush_para(para_buf); para_buf = []
            if state["table"]:
                out.append("</tbody></table></div>"); state["table"] = False
            marker = m.group(2)
            tag = "ol" if marker[0].isdigit() else "ul"
            text = m.group(3)
            if state["list"] != tag:
                if state["list"]:
                    out.append(f"</{state['list']}>")
                out.append(f"<{tag}>")
                state["list"] = tag
            cb = re.match(r"^\[([ xX])\]\s+(.+)$", text)
            if cb:
                checked = "checked" if cb.group(1).lower() == "x" else ""
                klass = "task done" if checked else "task"
                out.append(
                    f'<li class="{klass}"><input type="checkbox" disabled {checked}> '
                    f'{_inline(cb.group(2))}</li>'
                )
            else:
                out.append(f"<li>{_inline(text)}</li>")
            i += 1
            continue
        elif state["list"]:
            out.append(f"</{state['list']}>"); state["list"] = None

        if "|" in line:
            cells = [c for c in line.strip().strip("|").split("|")]
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if (not state["table"]
                    and _TABLE_SEPARATOR_RE.match(next_line)):
                flush_para(para_buf); para_buf = []
                out.append('<div class="table-wrap"><table><thead>')
                out.append(_render_table_row(cells, header=True))
                out.append("</thead><tbody>")
                state["table"] = True
                i += 2
                continue
            if state["table"]:
                out.append(_render_table_row(cells, header=False))
                i += 1
                continue
        if state["table"]:
            out.append("</tbody></table></div>"); state["table"] = False

        para_buf.append(line)
        i += 1

    flush_para(para_buf)
    _close_blocks(state, out)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Trading card hero rendering
# ---------------------------------------------------------------------------

def render_card(*, title: str, section_label: str, meta: dict) -> str:
    rarity = meta.get("rarity", "uncommon")
    stars = "★" * int(meta.get("stars", 3))
    tier_roman = meta.get("tier_roman", "TIER III")
    sigil_paths = meta.get("sigil", _SIGIL_DOC)
    abilities = meta.get("abilities", [])
    passives = meta.get("passives", "End-to-end source review.")

    ability_tiles = "\n".join(
        f'      <div class="bc-ability-item">'
        f'<div class="bc-ability-hex">◆</div>'
        f'<div class="bc-ability-info">'
        f'<div class="bc-a-name">{_esc(label)}</div>'
        f'<div class="bc-a-level">{_esc(sub)}</div>'
        f'</div></div>'
        for label, sub in abilities
    )

    return (
        f'<div class="bc-card-stage">\n'
        f'  <div class="bc-card bc-rarity-{rarity}">\n'
        f'    <div class="bc-card-inner">\n'
        f'      <div class="bc-holo-shimmer"></div>\n'
        f'      <div class="bc-rarity-banner">\n'
        f'        <div class="bc-rarity-stars">{stars}</div>\n'
        f'        <div class="bc-rarity-label">{_esc(rarity)}</div>\n'
        f'      </div>\n'
        f'      <div class="bc-card-art">\n'
        f'        <div class="bc-sigil">\n'
        f'          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">\n'
        f'            {sigil_paths}\n'
        f'          </svg>\n'
        f'        </div>\n'
        f'        <div class="bc-name-plate">\n'
        f'          <div class="bc-doc-id">// ARA · BIBLE · {_esc(section_label.upper())}</div>\n'
        f'          <h2>{_esc(title)}</h2>\n'
        f'          <div class="bc-level">{_esc(tier_roman)}</div>\n'
        f'        </div>\n'
        f'      </div>\n'
        f'      <div class="bc-abilities">\n'
        f'        <div class="bc-abilities-title">Abilities</div>\n'
        f'        <div class="bc-ability-grid">\n'
        f'{ability_tiles}\n'
        f'        </div>\n'
        f'      </div>\n'
        f'      <div class="bc-passives">\n'
        f'        <div class="bc-passives-title">{_esc(passives)}</div>\n'
        f'      </div>\n'
        f'    </div>\n'
        f'  </div>\n'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Page template (CSS combines armor-card.css + arctic-base sections)
# ---------------------------------------------------------------------------

_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;600;700&family=Press+Start+2P&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:        var(--wb-bg, #10131a);
  --paper:     var(--wb-surface, #1d2026);
  --paper-2:   var(--wb-custom-surface-low, #191c22);
  --paper-3:   var(--wb-custom-surface-high, #272a31);
  --ink:       var(--wb-ink, #e1e2eb);
  --muted:     var(--wb-muted, #63697a);
  --rule:      var(--wb-rule, #272a31);
  --accent:    var(--wb-accent, #b6c4ff);
  --accent-2:  var(--wb-accent-2, #4edea3);
  --accent-3:  var(--wb-custom-holo-purple, #9b59b6);
  --warn:      var(--wb-custom-holo-gold, #ffd166);
  --error:     var(--wb-error, #ffb4ab);
  --success:   var(--wb-success, #4edea3);
  --primary:   var(--wb-accent, #b6c4ff);
  --rounded:   var(--wb-rounded-md, 8px);
  --rounded-lg:var(--wb-rounded-lg, 12px);
  --font-body: var(--wb-font-body, 'Inter', ui-sans-serif, system-ui, sans-serif);
  --font-mono: var(--wb-font-mono, 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace);
  --font-display: var(--wb-custom-font-display, 'Space Grotesk', ui-sans-serif, system-ui, sans-serif);
  --text-mute: var(--muted);
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0;
  background: var(--bg); color: var(--ink);
  font-family: var(--font-body);
  font-size: 14px; line-height: 1.6;
}}
.wrap {{ max-width: 1100px; margin: 0 auto; padding: 36px 24px 96px; }}

/* ======================================================================
   ARMOR CARD HERO — verbatim from src/ara/armory/static/armor-card.css
   ====================================================================== */
@property --holo-angle {{ syntax: '<angle>'; initial-value: 0deg; inherits: false; }}

.bc-card-stage {{
  perspective: 1200px;
  display: flex; justify-content: center;
  padding: 16px 0 28px;
}}
.bc-card {{
  width: 480px; max-width: 100%;
  border-radius: 16px;
  padding: 3px;
  position: relative;
  transform-style: preserve-3d;
  transition: transform 0.15s ease-out;
}}
.bc-card::before {{
  content: '';
  position: absolute; inset: 0;
  border-radius: 16px; padding: 3px;
  background: linear-gradient(var(--holo-angle, 135deg), #ff00ff, #00ffff, #ffff00, #ff00ff, #00ffff);
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor; mask-composite: exclude;
  animation: bcHoloRotate 4s linear infinite;
  opacity: 0.7;
}}
.bc-rarity-common::before   {{ background: linear-gradient(var(--holo-angle, 135deg), #666, #999, #666) !important; opacity: 0.4; }}
.bc-rarity-uncommon::before {{ background: linear-gradient(var(--holo-angle, 135deg), #1eff00, #00ffaa, #1eff00) !important; }}
.bc-rarity-rare::before     {{ background: linear-gradient(var(--holo-angle, 135deg), #0070dd, #00aaff, #0070dd) !important; }}
.bc-rarity-epic::before     {{ background: linear-gradient(var(--holo-angle, 135deg), #a335ee, #cc66ff, #a335ee) !important; }}
@keyframes bcHoloRotate {{
  0%   {{ --holo-angle: 0deg;   filter: hue-rotate(0deg); }}
  100% {{ --holo-angle: 360deg; filter: hue-rotate(360deg); }}
}}

.bc-card-inner {{
  background: #0f1420;
  border-radius: 14px;
  overflow: hidden;
  position: relative;
}}
.bc-holo-shimmer {{
  position: absolute; inset: 0;
  background: linear-gradient(125deg,
    transparent 0%,
    rgba(255,255,255,0.03) 25%,
    rgba(74,166,204,0.05) 35%,
    rgba(161,230,255,0.05) 45%,
    rgba(255,255,255,0.05) 55%,
    rgba(74,166,204,0.03) 65%,
    transparent 100%);
  background-size: 250% 250%;
  animation: bcShimmerMove 6s ease-in-out infinite;
  pointer-events: none;
  z-index: 10;
  border-radius: 14px;
}}
@keyframes bcShimmerMove {{
  0%   {{ background-position: 0% 0%; }}
  50%  {{ background-position: 100% 100%; }}
  100% {{ background-position: 0% 0%; }}
}}

.bc-rarity-banner {{
  text-align: center; padding: 10px 16px 6px;
  background: linear-gradient(90deg, transparent, rgba(74,166,204,0.15), transparent);
}}
.bc-rarity-stars {{
  font-size: 13px; color: #ffd166; letter-spacing: 4px;
  text-shadow: 0 0 8px rgba(255,209,102,0.5);
}}
.bc-rarity-label {{
  font-family: 'Orbitron', var(--font-display);
  font-size: 10px; letter-spacing: 6px;
  text-transform: uppercase;
  color: var(--text-mute);
}}

.bc-card-art {{
  height: 260px;
  background: linear-gradient(180deg, rgba(15,20,32,0.5), #0f1420);
  display: flex; align-items: center; justify-content: center;
  padding-bottom: 92px;
  position: relative; overflow: hidden;
}}
.bc-card-art::before {{
  content: '';
  position: absolute; width: 200px; height: 200px; border-radius: 50%;
  background: radial-gradient(circle, rgba(74,166,204,0.2) 0%, transparent 60%);
  animation: bcArtPulse 4s ease-in-out infinite;
}}
@keyframes bcArtPulse {{
  0%, 100% {{ transform: scale(0.8); opacity: 0.5; }}
  50%      {{ transform: scale(1.2); opacity: 0.85; }}
}}
.bc-sigil {{
  width: 108px; height: 108px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  position: relative; z-index: 1;
  border: 3px solid rgba(74,166,204,0.55);
  background: rgba(10,13,16,0.55);
  box-shadow: 0 0 30px rgba(74,166,204,0.35);
  color: var(--primary);
}}
.bc-sigil svg {{ width: 52px; height: 52px; }}

.bc-name-plate {{
  position: absolute; bottom: 0; left: 0; right: 0;
  text-align: center; padding: 14px 12px 16px;
  background: linear-gradient(transparent 0%,
                              rgba(15,20,32,0.85) 45%,
                              #0f1420 80%);
}}
.bc-name-plate .bc-doc-id {{
  font-family: 'Press Start 2P', var(--font-mono);
  font-size: 8px;
  color: var(--accent);
  letter-spacing: 2px;
  margin-bottom: 8px;
}}
.bc-name-plate h2 {{
  font-family: 'Orbitron', var(--font-display);
  font-size: 22px; font-weight: 900;
  color: white;
  letter-spacing: 4px;
  text-shadow: 0 0 20px rgba(74,166,204,0.45);
  margin: 0;
  text-transform: uppercase;
}}
.bc-name-plate .bc-level {{
  font-family: 'Press Start 2P', var(--font-mono);
  font-size: 10px;
  color: #ffd166;
  letter-spacing: 2px;
  margin-top: 6px;
}}

.bc-abilities {{ padding: 16px 22px; }}
.bc-abilities-title {{
  font-family: 'Orbitron', var(--font-display);
  font-size: 10px; color: var(--text-mute); letter-spacing: 4px;
  margin-bottom: 12px; text-transform: uppercase;
}}
.bc-ability-grid {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
}}
.bc-ability-item {{
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px;
  background: rgba(255,255,255,0.02);
  border-radius: 6px;
  border-left: 3px solid;
}}
.bc-ability-item:nth-child(1) {{ border-color: var(--primary); }}
.bc-ability-item:nth-child(2) {{ border-color: #9b59b6; }}
.bc-ability-item:nth-child(3) {{ border-color: #3498db; }}
.bc-ability-item:nth-child(4) {{ border-color: #ffd166; }}
.bc-ability-hex {{
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px;
  background: rgba(255,255,255,0.05);
  clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%);
  flex-shrink: 0;
}}
.bc-ability-info .bc-a-name {{
  font-size: 12px; color: #dbe3ea; font-weight: 600;
  font-family: 'Rajdhani', var(--font-body);
}}
.bc-ability-info .bc-a-level {{
  font-size: 10px;
  color: var(--text-mute);
  font-family: var(--font-mono);
  letter-spacing: 0.5px;
}}

.bc-passives {{
  padding: 14px 22px 18px;
  border-top: 1px solid rgba(255,255,255,0.05);
}}
.bc-passives-title {{
  font-family: 'Rajdhani', var(--font-body);
  font-size: 13px; font-weight: 500;
  color: #dbe3ea; text-align: center;
  letter-spacing: 0.5px;
}}

@media (prefers-reduced-motion: reduce) {{
  .bc-card::before, .bc-holo-shimmer, .bc-card-art::before {{
    animation: none !important;
  }}
}}

/* ======================================================================
   ARCTIC-BASE SECTION PATTERN — eyebrow + h2 + lede + body
   ====================================================================== */

section {{
  padding: 48px 0;
  border-bottom: 1px dashed var(--rule);
}}
section:last-of-type {{ border-bottom: 0; }}
.eyebrow {{
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  margin-bottom: 10px;
}}
section > h2 {{
  margin: 0 0 14px;
  font-family: var(--font-display);
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.018em;
  color: var(--ink);
  line-height: 1.15;
}}
section h3 {{
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 600;
  color: var(--ink);
  margin: 28px 0 8px;
  letter-spacing: -0.01em;
}}
section h3::before {{
  content: '·· ';
  color: var(--accent);
  font-family: var(--font-mono);
  letter-spacing: 0;
}}
section h4 {{
  font-family: var(--font-mono);
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 20px 0 6px;
}}
section h5, section h6 {{
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--accent-2);
  margin: 16px 0 4px;
}}
p {{ margin: 0 0 0.9em; max-width: 78ch; }}
section p {{ color: var(--ink); }}

ul, ol {{ margin: 0 0 1em; padding-left: 1.4em; }}
li {{ margin: 4px 0; }}
li::marker {{ color: var(--accent); }}
li.task {{ list-style: none; margin-left: -1.4em; }}
li.task input {{ margin-right: 8px; transform: translateY(1px); }}
li.task.done {{ color: var(--muted); text-decoration: line-through; }}

code {{
  font-family: var(--font-mono);
  font-size: 0.92em;
  background: var(--paper-2);
  color: var(--accent-2);
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid color-mix(in srgb, var(--rule) 60%, transparent);
}}
pre {{
  background: var(--paper-2);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--accent);
  padding: 14px 18px;
  margin: 14px 0;
  overflow-x: auto;
  border-radius: 0 var(--rounded) var(--rounded) 0;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 6%, transparent);
}}
pre code {{
  background: transparent; color: var(--ink);
  border: 0; padding: 0;
  font-size: 13px; line-height: 1.55;
  font-weight: 500;
}}

blockquote {{
  border-left: 3px solid var(--accent-2);
  background: color-mix(in srgb, var(--accent-2) 6%, transparent);
  margin: 16px 0;
  padding: 12px 18px;
  border-radius: 0 var(--rounded) var(--rounded) 0;
  color: var(--ink);
}}
blockquote p {{ margin: 0.4em 0; }}
blockquote code {{
  background: color-mix(in srgb, var(--accent-2) 12%, transparent);
  color: var(--ink);
  border-color: color-mix(in srgb, var(--accent-2) 30%, transparent);
}}

.table-wrap {{
  background: var(--paper);
  border: 1px solid var(--rule);
  border-radius: var(--rounded);
  overflow: hidden;
  margin: 16px 0;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}}
th, td {{
  padding: 10px 14px;
  text-align: left;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
}}
th {{
  background: var(--paper-3);
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--muted);
}}
tbody tr:nth-child(even) {{
  background: color-mix(in srgb, var(--paper-2) 50%, transparent);
}}
tbody tr:hover {{
  background: color-mix(in srgb, var(--accent) 6%, var(--paper));
}}
tbody tr:last-child td {{ border-bottom: 0; }}
td code, th code {{
  background: rgba(0,0,0,0.25);
  border-color: color-mix(in srgb, var(--rule) 80%, transparent);
}}

a {{
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px dashed transparent;
  transition: border-color 0.12s ease;
}}
a:hover {{ border-bottom-color: var(--accent); }}

hr {{
  border: 0;
  height: 1px;
  background: linear-gradient(90deg,
    transparent,
    color-mix(in srgb, var(--accent) 40%, transparent),
    transparent);
  margin: 36px 0;
}}

footer.bible-foot {{
  margin-top: 56px;
  padding-top: 24px;
  border-top: 1px dashed var(--rule);
  color: var(--muted);
  font-size: 11px;
  font-family: var(--font-mono);
  letter-spacing: 0.05em;
  text-align: center;
}}
footer.bible-foot a {{ color: var(--accent); }}
footer.bible-foot .row {{ margin: 4px 0; }}

/* Below-card breadcrumb / nav */
.bc-after-card-meta {{
  text-align: center;
  margin-top: -12px; margin-bottom: 24px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--muted);
  letter-spacing: 0.18em;
  text-transform: uppercase;
}}

@media (max-width: 720px) {{
  .wrap {{ padding: 16px 14px 64px; }}
  .bc-card {{ width: 100%; }}
  .bc-ability-grid {{ grid-template-columns: 1fr; }}
  .bc-name-plate h2 {{ font-size: 18px; letter-spacing: 2px; }}
  section {{ padding: 32px 0; }}
  section > h2 {{ font-size: 22px; }}
  th, td {{ padding: 8px 10px; }}
}}
</style>
</head>
<body>
<div class="wrap">
  {card}
  <div class="bc-after-card-meta">// LEDE BELOW · {section_label} OF THE ARA BIBLE</div>
  {lede}
  {sections}
  <footer class="bible-foot">
    <div class="row">// ARA · BIBLE · {section_label} — line-by-line review of every source file in this subsystem.</div>
    <div class="row">No inference. Every claim above is backed by a file read; verbatim quotes are blockquoted.</div>
  </footer>
</div>
</body>
</html>
"""


def render(md_path: Path, out_path: Path) -> None:
    md_text = md_path.read_text()
    title = md_path.stem.replace("-", " ").upper()
    section_label = md_path.stem
    m = re.search(r"^#\s+(.+?)\s*$", md_text, re.M)
    if m:
        full = m.group(1)
        if "·" in full:
            section_label, _, title = full.partition("·")
            section_label = section_label.strip()
            title = title.strip()
        else:
            title = full
            section_label = md_path.stem

    lede_html, sections = md_to_sections(md_text, section_label)
    if lede_html.strip():
        lede_block = f'<div class="bible-lede">{lede_html}</div>'
    else:
        lede_block = ""

    meta = CARD_META.get(md_path.stem, _DEFAULT_CARD)
    card_html = render_card(
        title=title, section_label=section_label, meta=meta,
    )

    page = _PAGE_TEMPLATE.format(
        title=html.escape(title),
        section_label=html.escape(section_label),
        card=card_html,
        lede=lede_block,
        sections="\n  ".join(sections),
    )
    out_path.write_text(page)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: bible-md2html.py <input.md> <output.html>",
              file=sys.stderr)
        return 2
    render(Path(argv[1]), Path(argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
