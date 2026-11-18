"""Live debug viewer — one page that tails all of a session's log streams.

Aggregation only: every stream here is read from the same per-session /
global ``*.jsonl`` files that other armory pages (/events, /sessions, /courtyard,
/rhema) read. This page adds no new data — it just unifies the live tail of the
high-signal streams (narration, findings, leads, phase timeline, enforcement,
events, framework debug) with noise filtered out and every line timestamped.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import views as base_views


def _hhmmss(ts) -> str:
    """epoch-float / epoch-string / ISO → HH:MM:SS (best effort)."""
    if ts is None:
        return "--:--:--"
    try:
        f = float(ts)
        if f > 1_000_000_000:
            import time as _t
            return _t.strftime("%H:%M:%S", _t.localtime(f))
    except (TypeError, ValueError):
        pass
    s = str(ts)
    m = re.search(r"(\d{2}:\d{2}:\d{2})", s)
    return m.group(1) if m else "--:--:--"


def _proc_running(pattern: str) -> bool:
    import subprocess
    try:
        return bool(subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, timeout=3).stdout.strip())
    except Exception:  # noqa: BLE001
        return False


_MODEL_CACHE = {"ts": 0.0, "up": None}


def _model_up() -> bool | None:
    """Quick health of the model endpoint (cached ~15s to keep polls cheap)."""
    import time
    import urllib.request
    now = time.time()
    if _MODEL_CACHE["up"] is not None and now - _MODEL_CACHE["ts"] < 15:
        return _MODEL_CACHE["up"]
    url = os.environ.get("ARA_MODEL_HEALTH_URL", "http://127.0.0.1:1234/v1/models")
    up = False
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            up = r.status == 200
    except Exception:  # noqa: BLE001
        up = False
    _MODEL_CACHE.update(ts=now, up=up)
    return up


def _seccomp_status(state_root: Path) -> "tuple[bool | None, int, int]":
    """Egress-enforcement status from the seccomp-egress debug stream:
    ``(active, n_allow, n_deny)`` over the recent tail. ``active`` is None when
    unknown. Lets the health bar flag a silent fallback-to-shim while a task
    runs (i.e. the unbypassable layer isn't actually on)."""
    p = Path(state_root) / "logs" / "ara-debug.jsonl"
    if not p.is_file():
        return (None, 0, 0)
    active: "bool | None" = None
    na = nd = 0
    try:
        chunk = p.read_bytes()[-300_000:]
        for line in chunk.splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("component") != "seccomp-egress":
                continue
            ev = r.get("event")
            f = r.get("fields") or {}
            if ev == "attached":
                active = True
            elif ev in ("loop_exit", "attach_failed", "no_fd_received"):
                active = False
            elif ev == "decision":
                active = True
                if f.get("verdict") == "allow":
                    na += 1
                elif f.get("verdict") == "deny":
                    nd += 1
    except Exception:  # noqa: BLE001
        pass
    return (active, na, nd)


def health(state_root: Path) -> list[dict]:
    """Up/down of every ARA + Hermes component, for the debug health bar.
    ``required``: whether this component is expected to always be up (vs. only
    while a task runs)."""
    task_running = _proc_running("contain-profile armory-task")
    _sec_active, _sec_a, _sec_d = _seccomp_status(state_root)
    _sec_name = "Egress enforce (seccomp)"
    if _sec_active:
        _sec_name += f"  {_sec_a}✓/{_sec_d}✕"
    checks = [
        ("ARA gateway (supervisor)", _proc_running("ara.cli launch.*hermes gateway"), True),
        ("Contained Hermes", _proc_running("hermes gateway run"), True),
        ("Armory (web UI)", True, True),  # serving this page
        # forge-mcp is spawned by a task session (needs ARA_SOCKET), so it is
        # only *expected* up while a task runs — down-when-a-task-runs is the
        # real failure; down-while-idle is normal.
        ("forge-mcp", _proc_running("forge-mcp"), task_running),
        ("temper-mcp", _proc_running("temper-mcp"), True),
        ("Model endpoint", bool(_model_up()), True),
        ("Task session", task_running, False),
        ("MITM proxy", _proc_running("mitm") or task_running, False),
        # Kernel egress backstop — only *expected* up while a task runs; down
        # then means it silently fell back to the shim.
        (_sec_name, bool(_sec_active), task_running),
    ]
    return [{"name": n, "up": bool(u), "required": req} for n, u, req in checks]


def _sessions(state_root: Path) -> list[dict]:
    root = Path(state_root) / "sessions"
    out: list[dict] = []
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if not d.is_dir():
            continue
        mtime = 0.0
        for name in ("transcript.jsonl", "events.jsonl"):
            p = d / name
            if p.is_file():
                mtime = max(mtime, p.stat().st_mtime)
        running = False
        sj = d / "session.json"
        if sj.is_file():
            try:
                pid = json.loads(sj.read_text()).get("pid")
                running = bool(pid) and Path(f"/proc/{pid}").exists()
            except Exception:  # noqa: BLE001
                pass
        tr = d / "transcript.jsonl"
        has_content = tr.is_file() and tr.stat().st_size > 0
        out.append({"id": d.name, "mtime": mtime, "running": running,
                    "content": has_content})
    # Sort real task sessions (with transcript) to the top — running first,
    # then by recency — so bare gateway/infra sessions don't head the picker.
    out.sort(key=lambda r: (r["content"], r["running"], r["mtime"]), reverse=True)
    return out


def _default_session(sessions: list[dict]) -> str:
    """Prefer the newest task session with actual transcript content (running
    first) over bare gateway/infra sessions that have no narration."""
    for s in sessions:
        if s["running"] and s["content"]:
            return s["id"]
    for s in sessions:
        if s["content"]:
            return s["id"]
    return sessions[0]["id"] if sessions else ""


def _tail(path: Path, start: int, fmt, *, keep=None) -> tuple[list[str], int]:
    """Read jsonl records after byte offset ``start``; format each; drop those
    ``fmt`` maps to None (or that ``keep`` rejects)."""
    lines: list[str] = []
    if not path.is_file():
        return lines, start
    with path.open() as f:
        f.seek(start)
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if keep is not None and not keep(r):
                continue
            try:
                s = fmt(r)
            except Exception:  # noqa: BLE001
                s = None
            if s:
                lines.append(s)
        return lines, f.tell()


# ---- narration (transcript assistant text) --------------------------------

def _tail_narration(path: Path, start: int) -> tuple[list[str], int]:
    lines: list[str] = []
    if not path.is_file():
        return lines, start
    with path.open() as f:
        f.seek(start)
        for raw in f:
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if r.get("kind") != "transcript":
                continue
            bt = (r.get("payload") or {}).get("body_text", "")
            if not isinstance(bt, str):
                continue
            frags = re.findall(r'"content":"((?:[^"\\]|\\.)*)"', bt)
            txt = "".join(frags)
            if txt.strip():
                try:
                    txt = bytes(txt, "utf-8").decode("unicode_escape", "ignore")
                except Exception:  # noqa: BLE001
                    pass
                lines.append(f"{_hhmmss(r.get('ts'))}  {txt.strip()}")
        return lines, f.tell()


def _tail_ask(path: Path, start: int) -> tuple[list[str], int]:
    """What the model is TOLD each turn (what rhema produces) — from request
    transcript records: the system prompt size (injected absolute+temper) and
    the latest user message (the turn/chunk prompt)."""
    lines: list[str] = []
    if not path.is_file():
        return lines, start
    with path.open() as f:
        f.seek(start)
        for raw in f:
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if r.get("kind") != "transcript":
                continue
            body = (r.get("payload") or {}).get("body")
            if not isinstance(body, dict):
                continue
            msgs = body.get("messages")
            if not isinstance(msgs, list) or not msgs:
                continue
            user = None
            sys_chars = 0
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if m.get("role") == "user":
                    user = m.get("content")
                elif m.get("role") == "system":
                    sys_chars = len(str(m.get("content") or ""))
            if user:
                u = str(user).replace("\n", " ").strip()
                lines.append(f"{_hhmmss(r.get('ts'))}  [sys {sys_chars}c] {u[:400]}")
        return lines, f.tell()


# ---- per-stream formatters -------------------------------------------------

def _fmt_fact(r: dict) -> str:
    pred = r.get("predicate") or "?"
    val = r.get("value")
    ev = r.get("evidence_refs")
    n = len(ev) if isinstance(ev, list) else 0
    conf = r.get("confidence")
    tail = f"  ({n} ev" + (f", conf {conf})" if conf not in (None, "") else ")")
    return f"{_hhmmss(r.get('asserted_at'))}  {pred}: {str(val)[:120]}{tail}"


def _fmt_lead(r: dict) -> str:
    pr = r.get("priority_computed") or r.get("priority_agent_proposed") or "?"
    st = r.get("status") or ""
    return f"{_hhmmss(r.get('created_at'))}  [{str(pr)[:4]}] {str(r.get('title'))[:110]}  · {st}"


def _fmt_chunk(r: dict) -> str:
    kind = r.get("kind") or ""
    arrow = "▶ init" if kind == "initialize" else "→"
    return f"{_hhmmss(r.get('ts'))}  {arrow} {r.get('to_chunk','?')}  ({kind})"


def _fmt_activity(r: dict) -> str:
    """Runtime containment audit: every net/dns/exec/fs op + its verdict.
    A non-allow verdict (a temper/bulwark enforcement) is marked so it stands
    out among the routine allows."""
    op = str(r.get("op") or r.get("kind") or "")
    verdict = str(r.get("verdict") or r.get("action") or r.get("decision") or "allow").lower()
    path = r.get("path") or r.get("raw_path") or ""
    if op == "exec.spawn":
        cmd = r.get("command") or ""
        args = r.get("args") or []
        path = (f"{cmd} " + " ".join(str(a) for a in args[:3])).strip() or path
    mark = "" if verdict == "allow" else "⛔ "
    return f"{_hhmmss(r.get('ts'))}  {mark}{verdict:8} {op:12} {str(path)[:70]}"


def _fmt_channel(r: dict) -> str:
    b = r.get("body") or {}
    ch = b.get("channel") or str(r.get("kind", "")).replace("channel.", "")
    extra = b.get("summary") or b.get("title") or b.get("tag") or b.get("query") or ""
    st = b.get("status", "")
    return f"{_hhmmss(r.get('ts'))}  {ch:18} {st:8} {str(extra)[:90]}"


def _keep_channel(r: dict) -> bool:
    # only real channel events — drop heartbeat/hello/spawn noise
    return str(r.get("kind") or "").startswith("channel.")


_DEBUG_SKIP_FIELDS = {"args", "bulwark_event", "detail", "spec", "body",
                      "keep", "keep_default_sections"}


def _fmt_debug(r: dict) -> str:
    lvl = str(r.get("level") or "").lower()
    comp = r.get("component") or "?"
    ev = r.get("event") or ""
    # keep warnings/errors + the operationally interesting components; drop
    # routine framework chatter.
    keep = (lvl in ("warn", "warning", "error", "critical")
            or comp in ("task-session", "task-gate", "containment", "bulwark")
            or "gate" in str(ev) or "deny" in str(ev) or "rewrite" in str(ev)
            or "error" in str(ev) or "reject" in str(ev))
    if not keep:
        return None
    fields = r.get("fields") or {}
    # compact key=val of scalar fields only — no nested/verbose dumps.
    kv = []
    for k, v in fields.items():
        if k in _DEBUG_SKIP_FIELDS or isinstance(v, (dict, list)):
            continue
        kv.append(f"{k}={str(v)[:36]}")
        if len(kv) >= 4:
            break
    tag = "ERR " if lvl in ("error", "critical") else (
        "WARN " if lvl in ("warn", "warning") else "")
    # ``comp\tevent`` — the tab lets the client color the component token.
    return (f"{_hhmmss(r.get('ts') or r.get('iso'))}  {tag}"
            f"{comp}\t{ev}  {' '.join(kv)}")


def _fmt_seccomp(r: dict) -> "str | None":
    """Per-decision view of the kernel egress firewall (seccomp user-notif).

    Reads the global ``seccomp-egress`` debug stream. Every BLOCK is shown (with
    host/port/reason) plus any non-loopback ALLOW (real egress that was let
    through). Routine loopback / AF_UNIX allows — the model + MITM plumbing, not
    real egress — are dropped as noise; their aggregate lives in the health
    badge. Lines starting with ⛔ are rendered red by the client.
    """
    if r.get("component") != "seccomp-egress":
        return None
    ev = r.get("event") or ""
    ts = _hhmmss(r.get("ts") or r.get("iso"))
    f = r.get("fields") or {}
    if ev == "attached":
        return f"{ts}  🛡 seccomp attached — kernel egress enforcement active"
    if ev in ("failopen", "detached", "error", "attach_failed"):
        return f"{ts}  ⛔ seccomp {ev} — {str(f.get('reason') or f.get('error') or '')[:60]}"
    if ev != "decision":
        return None
    verdict = str(f.get("verdict") or "")
    host = f.get("host")
    port = f.get("port")
    op = str(f.get("op") or "")
    if verdict == "allow":
        h = str(host or "")
        # loopback / unknown-dest / AF_UNIX plumbing — not egress, drop the spam.
        if host is None or h.startswith("127.") or h.startswith("::1") or h == "localhost":
            return None
        return f"{ts}  ✓ allow   {op:8} {h}:{port}"
    return f"{ts}  ⛔ {verdict:7} {op:8} {host}:{port}  {str(f.get('reason') or '')[:32]}"


def _fmt_evidence(r: dict) -> "str | None":
    """Evidence capture: tool call+output bound as ``tool:*`` evidence from the
    MITM stream (the actual response, not the command)."""
    if r.get("component") != "evidence-binder":
        return None
    ev = r.get("event") or ""
    ts = _hhmmss(r.get("ts") or r.get("iso"))
    f = r.get("fields") or {}
    if ev == "tool_capture":
        return (f"{ts}  🧾 bound {f.get('bound')} tool-output record(s) → evidence"
                f"  act={str(f.get('activation_id') or '')[:12]}")
    return f"{ts}  🧾 {ev}  {str({k: v for k, v in f.items() if not isinstance(v, (dict, list))})[:60]}"


def _fmt_evgate(r: dict) -> "str | None":
    """Evidence gate + provenance: primary-evidence accept/reject decisions and
    the auto-correlated provenance chain bound at filing."""
    comp = r.get("component")
    if comp not in ("finding-gate", "provenance"):
        return None
    ev = r.get("event") or ""
    ts = _hhmmss(r.get("ts") or r.get("iso"))
    f = r.get("fields") or {}
    if comp == "provenance" and ev == "enriched":
        prim = f.get("primary") or []
        return (f"{ts}  🔗 provenance bound  primary={len(prim) if isinstance(prim, list) else prim}"
                f" conns={f.get('connected_via')} auth={len(f.get('authorized_by') or [])}")
    if comp == "finding-gate":
        verdict = str(f.get("verdict") or ev)
        icon = "✓" if verdict == "accept" else "⛔"
        return (f"{ts}  {icon} {f.get('kind') or 'entry'} gate {verdict}"
                f"  {str(f.get('reason') or '')[:50]}")
    return None


def _keep_guard(r: dict) -> bool:
    return r.get("component") == "hermes-guard"


def _fmt_guard(r: dict) -> "str | None":
    if r.get("component") != "hermes-guard":
        return None
    return f"🚨 {r.get('event', 'guard')}: {r.get('detail', '')}"


_STREAMS = [
    # key, label, icon, (session-relative file | None for global), formatter, keep-filter
    ("ask", "Ask — what the agent is told (rhema)", "📥", "transcript.jsonl", None, None),
    ("narration", "Narration — what the agent replies", "🗣", "transcript.jsonl", None, None),
    ("findings", "Findings (facts)", "🔎", "facts.jsonl", _fmt_fact, None),
    ("leads", "Leads", "💡", "leads.jsonl", _fmt_lead, None),
    ("phase", "Phase timeline", "🧭", "chunk_history.jsonl", _fmt_chunk, None),
    ("activity", "Runtime activity (net / dns / exec / fs)", "🌐", "policy_hits.jsonl", _fmt_activity, None),
    ("egress", "Egress firewall — kernel (seccomp)", "🛡", None, _fmt_seccomp, None),
    ("evidence", "Evidence capture (tool output)", "🧾", None, _fmt_evidence, None),
    ("evgate", "Evidence gate & provenance", "🔗", None, _fmt_evgate, None),
    ("events", "Channels", "📡", "events.jsonl", _fmt_channel, _keep_channel),
    ("hermes-guard", "Hermes guard (rogue / hold-off)", "🚨", None, _fmt_guard, _keep_guard),
    ("debug", "Framework debug (warn / error / gate)", "🔧", None, _fmt_debug, None),
]


def debug_tail(state_root: Path, session: str, offsets: dict) -> dict:
    state_root = Path(state_root)
    sd = state_root / "sessions" / session if session else None
    res: dict = {"session": session, "streams": {}, "offsets": {},
                 "health": health(state_root)}
    for key, _label, _icon, fname, fmt, keep in _STREAMS:
        off = int(offsets.get(key, 0) or 0)
        if key in ("narration", "ask"):
            path = (sd / "transcript.jsonl") if sd else None
            if path is None:
                res["streams"][key] = []
                res["offsets"][key] = 0
                continue
            lines, new_off = (_tail_ask if key == "ask"
                              else _tail_narration)(path, off)
        else:
            path = (state_root / "logs" / "ara-debug.jsonl") if fname is None \
                else ((sd / fname) if sd else None)
            if path is None:
                res["streams"][key] = []
                res["offsets"][key] = 0
                continue
            lines, new_off = _tail(path, off, fmt, keep=keep)
            if key in ("debug", "egress"):
                lines = lines[-60:]
        res["streams"][key] = lines
        res["offsets"][key] = new_off
    return res


def render_debug_page(state_root: Path, token: str, session: str | None = None) -> str:
    sessions = _sessions(state_root)
    sel = session or _default_session(sessions)
    opts = "".join(
        f'<option value="{base_views.e(s["id"])}"{" selected" if s["id"]==sel else ""}>'
        f'{"● " if s["running"] else "○ "}{base_views.e(s["id"][:18])}'
        f'{"" if s["content"] else " (no transcript)"}</option>'
        for s in sessions
    )
    panels = "".join(
        f'<div class="dbgpanel" data-k="{k}"><h3>{ic} {base_views.e(lbl)} '
        f'<span class="cnt" data-cnt="{k}">0</span>'
        f'<button class="dbgcopy" type="button" data-copy="{k}" '
        f'title="Copy this panel\'s contents">copy</button>'
        f'</h3><pre></pre></div>'
        for (k, lbl, ic, *_rest) in _STREAMS
    )
    body = f"""
<section>
<div class="ara-eyebrow">Live debug</div>
<h2 class="ara-h" style="font-size:22px;margin:2px 0 4px">Session log stream</h2>
<p class="ara-sub" style="margin:0 0 10px">A unified live tail of one session's logs — same data the other pages read, filtered to signal and timestamped. Auto-refreshes every 3s.</p>
<div id="health" class="healthbar"></div>
<div class="dbgbar">
  <select id="sess">{opts}</select>
  <label><input type="checkbox" id="auto" checked> auto 3s</label>
  <label><input type="checkbox" id="wrap" checked> wrap</label>
  <button id="clear" class="ara-btn ara-btn-ghost" type="button">Clear</button>
  <span id="stat" class="ara-sub"></span>
</div>
<div id="grid">{panels}</div>
</section>
<style>
.healthbar{{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}}
.hpill{{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;border:1px solid var(--wb-border,#30363d)}}
.hpill .dot{{width:8px;height:8px;border-radius:50%}}
.hpill.up{{background:rgba(63,185,80,.12);color:#3fb950}} .hpill.up .dot{{background:#3fb950}}
.hpill.down{{background:rgba(248,81,73,.14);color:#f85149}} .hpill.down .dot{{background:#f85149}}
.hpill.idle{{background:rgba(139,148,158,.1);color:#8b949e}} .hpill.idle .dot{{background:#8b949e}}
.dbgbar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px;font-size:13px}}
.dbgbar select{{background:var(--wb-panel,#161b22);color:inherit;border:1px solid var(--wb-border,#30363d);border-radius:8px;padding:7px 10px;font-family:monospace;max-width:60vw}}
#grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.dbgpanel{{background:var(--wb-panel,#0d1117);border:1px solid var(--wb-border,#30363d);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}}
.dbgpanel h3{{margin:0;padding:8px 12px;font-size:12.5px;font-weight:600;border-bottom:1px solid var(--wb-border,#30363d);background:rgba(127,127,127,.05);display:flex;align-items:center;gap:6px}}
.dbgpanel h3 .cnt{{margin-left:auto;font-size:11px;color:var(--wb-muted,#8b949e);background:rgba(127,127,127,.12);border-radius:20px;padding:1px 8px;font-weight:500}}
.dbgpanel h3 .dbgcopy{{margin-left:6px;font:600 10.5px/1.4 inherit;padding:1px 9px;cursor:pointer;border-radius:20px;border:1px solid var(--wb-border,#30363d);background:rgba(127,127,127,.12);color:var(--wb-muted,#8b949e)}}
.dbgpanel h3 .dbgcopy:hover{{color:var(--wb-fg,#e6edf3);border-color:var(--wb-muted,#8b949e)}}
.dbgpanel h3 .dbgcopy.ok{{color:#3fb950;border-color:#3fb950;background:rgba(63,185,80,.14)}}
.dbgpanel pre{{margin:0;padding:10px 12px;font:12px/1.55 'JetBrains Mono',ui-monospace,monospace;overflow:auto;max-height:44vh;flex:1}}
.dbgpanel pre.nowrap{{white-space:pre}}
.dbgpanel pre.wrap{{white-space:pre-wrap;word-break:break-word}}
.dbgpanel pre .t{{color:var(--wb-muted,#6e7681)}}
.dbgpanel[data-k=findings] pre,.dbgpanel[data-k=leads] pre{{color:#7ee787}}
.dbgpanel[data-k=phase] pre{{color:#79c0ff}}
.dbgpanel[data-k=activity] pre{{color:#8b949e}}
.dbgpanel[data-k=narration] pre{{color:#adbac7}}
.dbgpanel[data-k=debug] pre{{color:#8b949e}}
.dbgpanel[data-k=egress] pre{{color:#8b949e}}
.dbgpanel pre .deny{{color:#ff7b72;font-weight:600}}
.dbgpanel pre .warn{{color:#e3b341}}
.dbgpanel pre span[class^=c-]{{font-weight:600}}
.c-supervisor{{color:#d2a8ff}} .c-mitm{{color:#79c0ff}} .c-bulwark{{color:#ff9492}}
.c-task-gate{{color:#f0883e}} .c-task-session{{color:#7ee787}} .c-containment{{color:#ffa657}}
.c-launch{{color:#8b949e}} .c-armory{{color:#a5d6ff}} .c-forge-mcp{{color:#56d4dd}}
.dbgpanel pre .pill{{padding:0 6px;border-radius:20px;font-size:10.5px;font-weight:600;white-space:nowrap}}
.dbgpanel pre .pill.pg{{background:rgba(63,185,80,.16);color:#3fb950}}
.dbgpanel pre .pill.pr{{background:rgba(248,81,73,.16);color:#f85149}}
.dbgpanel pre .pill.pa{{background:rgba(210,153,34,.16);color:#e3b341}}
.dbgpanel pre .pill.pn{{background:rgba(139,148,158,.14);color:#adbac7}}
@media(max-width:820px){{#grid{{grid-template-columns:1fr}}.dbgbar select{{max-width:100%}}}}
</style>
<script>
(function(){{
  var TOKEN={json.dumps(token)};
  var off={{}}, counts={{}};
  var panels={{}};
  document.querySelectorAll('.dbgpanel').forEach(function(p){{panels[p.dataset.k]=p.querySelector('pre');counts[p.dataset.k]=0;}});
  function cur(){{return document.getElementById('sess').value;}}
  function wrapCls(){{return document.getElementById('wrap').checked?'wrap':'nowrap';}}
  function applyWrap(){{for(var k in panels)panels[k].className=wrapCls();}}
  function reset(){{off={{}}; for(var k in panels){{panels[k].textContent='';counts[k]=0;document.querySelector('[data-cnt='+k+']').textContent='0';}}}}
  function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;');}}
  function pills(s){{ // status-like values -> colored pills
    return s.replace(/\\b(verdict|decision|status|action|phase|reason|result)=([A-Za-z_]+)/g,function(_,k,v){{
      var lv=v.toLowerCase(), c='pn';
      if(/(allow|^ok$|work|pass|accept|clear|done)/.test(lv)) c='pg';
      else if(/(deny|block|reject|fail|error|miss|abort|kill)/.test(lv)) c='pr';
      else if(/(approv|pending|warn|requir|gat|review|skip)/.test(lv)) c='pa';
      return k+'=<span class="pill '+c+'">'+v+'</span>';
    }});
  }}
  function fmt(line){{
    // split leading HH:MM:SS as muted
    var m=line.match(/^(\\d{{2}}:\\d{{2}}:\\d{{2}})\\s+([\\s\\S]*)$/);
    var rest=m?m[2]:line, html;
    var tab=rest.indexOf('\\t');
    if(tab>=0){{ // framework debug: "[ERR ]component\\tevent  kv"
      var comp=rest.slice(0,tab), after=rest.slice(tab+1);
      var isErr=/^ERR /.test(comp), isWarn=/^WARN /.test(comp);
      var name=comp.replace(/^(ERR|WARN)\\s+/,'');
      var compHtml='<span class="c-'+name+'">'+esc(comp)+'</span>';
      html=compHtml+'  '+pills(esc(after));
      if(isErr) html='<span class="deny">'+html+'</span>';
      else if(isWarn) html='<span class="warn">'+html+'</span>';
    }} else {{
      html=pills(esc(rest));
      if(rest.indexOf('\\u26d4')===0) html='<span class="deny">'+html+'</span>';
    }}
    return m?('<span class="t">'+m[1]+'</span>  '+html):html;
  }}
  function poll(){{
    var s=cur(); if(!s)return;
    var q='/debug/tail?t='+encodeURIComponent(TOKEN)+'&session='+encodeURIComponent(s);
    for(var k in off)q+='&'+k+'='+off[k];
    fetch(q).then(function(r){{return r.json();}}).then(function(d){{
      off=d.offsets||off; var added=0;
      if(d.health){{
        document.getElementById('health').innerHTML=d.health.map(function(h){{
          var cls=h.up?'up':(h.required?'down':'idle');
          var label=h.up?'up':(h.required?'DOWN':'idle');
          return '<span class="hpill '+cls+'"><span class="dot"></span>'+esc(h.name)+' · '+label+'</span>';
        }}).join('');
      }}
      for(var k in panels){{
        var lines=(d.streams&&d.streams[k])||[];
        if(lines.length){{
          var pre=panels[k];
          pre.innerHTML+=lines.map(fmt).join('\\n')+'\\n';
          pre.scrollTop=pre.scrollHeight;
          counts[k]+=lines.length; added+=lines.length;
          var c=document.querySelector('[data-cnt='+k+']'); if(c)c.textContent=counts[k];
        }}
      }}
      document.getElementById('stat').textContent='updated '+new Date().toLocaleTimeString()+(added?(' +'+added):'');
    }}).catch(function(e){{document.getElementById('stat').textContent='error: '+e;}});
  }}
  function copyFallback(txt,done){{var ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{{document.execCommand('copy');done();}}catch(e){{}}document.body.removeChild(ta);}}
  function copyPanel(k,btn){{
    var pre=panels[k]; if(!pre)return;
    var txt=pre.textContent||'';
    var done=function(){{btn.textContent='copied';btn.classList.add('ok');setTimeout(function(){{btn.textContent='copy';btn.classList.remove('ok');}},1200);}};
    if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(txt).then(done,function(){{copyFallback(txt,done);}});}}
    else copyFallback(txt,done);
  }}
  document.querySelectorAll('.dbgcopy').forEach(function(b){{b.addEventListener('click',function(){{copyPanel(b.getAttribute('data-copy'),b);}});}});
  document.getElementById('sess').addEventListener('change',function(){{reset();poll();}});
  document.getElementById('clear').addEventListener('click',reset);
  document.getElementById('wrap').addEventListener('change',applyWrap);
  applyWrap(); poll();
  setInterval(function(){{if(document.getElementById('auto').checked)poll();}},3000);
}})();
</script>
"""
    return base_views._page("Debug", "debug", body, token)
