"""Armory views for forge-v1 surfaces.

Three pages:

- ``/engagement`` — live engagement status (equipped weapon, active
  chunk, recent activations + chunk swaps, channel-call counts,
  finding/lead/anomaly/fact/evidence totals). Mirror of ``ara
  engagement status`` rendered as HTML.

- ``/weapons`` — sealed weapons list with equip/unequip controls.
  Click a weapon for detail (alphabets, chunks, channel catalog,
  policy stack) — currently a single drill-down per weapon_id.

- ``/forge`` — forge sessions index and browser-driven forge workflow.
  Operators can create sessions, upload mats, progress phases, synthesize,
  seal, abort, and inspect per-phase detail.

All renderers take ``state_root`` + ``token`` and return the full
HTML page string. Wire-up lives in ``armory/app.py`` next to the
existing tab handlers.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from .. import armors as armors_mod
from .. import process_state as process_state_mod
from .. import task_channel_gate as task_channel_gate_mod
from .. import task_preflight as task_preflight_mod
from .. import temper as temper_mod
from .. import trims as trims_mod
from .. import weapons as weapons_mod

import html as _html


def render_pack_review(state_root, forge_id: str, token: str = "") -> str:
    """Render the pack-forge draft as one batch curate form (shown on the
    forge session page when kind == 'pack'). Checkboxes name
    ``keep_<group>_<index>``; per-group ``add_<group>`` adds a custom item;
    Save curation / Seal pack submit to ``/forge/<id>/pack/curate``."""
    from ara.forge.phases import synthesize
    fq = _html.escape(forge_id)
    tq = _html.escape(token)
    draft = synthesize.load_pack_draft(Path(state_root), forge_id)
    if draft is None:
        return ('<section class="card forge-controls-card pack-review">'
                '<div class="forge-card-heading"><div>'
                '<span class="ara-eyebrow">Curate</span><h2>Pack draft</h2>'
                '<p class="muted">No pack draft yet — run <b>Synthesize</b> '
                'to assemble the agent\'s proposals into a draft you can curate.'
                '</p></div></div></section>')
    # group meta: label + a one-line hint + whether its data blob is wide
    groups = (
        ("alphabets", "Vocabulary shapes the agent declared", True),
        ("entries", "Concrete values populating the alphabets", True),
        ("bindings", "Validator rules tying record fields to alphabets", False),
        ("chunks", "Investigation chunk-graph templates", True),
        ("policy", "Bulwark / WALLS / rhema policy defaults", True),
        ("detection", "Manifest kind-detection tuning", False),
    )
    out = [
        '<section class="card forge-controls-card pack-review">'
        '<div class="forge-card-heading"><div>'
        '<span class="ara-eyebrow">Curate</span>'
        f'<h2>Pack draft: {_html.escape(draft.name)}</h2>'
        '<p class="muted">Untick an item to drop it from the pack; add custom '
        'items as JSON. <b>Save curation</b> keeps your selections; '
        '<b>Seal pack</b> validates and writes the runtime pack.</p>'
        '</div></div>'
        f'<form method="post" action="/forge/{fq}/pack/curate?t={tq}">'
        '<div class="pack-group-grid">'
    ]
    for g, hint, wide in groups:
        items = draft.items_for(g)
        wide_cls = " pack-group-wide" if wide else ""
        out.append(
            f'<div class="forge-action-card pack-group{wide_cls}">'
            f'<span>{g} · {len(items)}</span>'
            f'<p class="pack-group-hint">{_html.escape(hint)}</p>'
            '<div class="pack-items">')
        if not items:
            out.append('<p class="pack-none">none</p>')
        for i, it in enumerate(items):
            checked = "checked" if it.get("keep", True) else ""
            src = _html.escape(str(it.get("source", "")))
            blob = json.dumps(it.get("data"), ensure_ascii=False)
            data = _html.escape(blob if len(blob) <= 220 else blob[:220] + "…")
            out.append(
                f'<label class="pack-item"><input type="checkbox" '
                f'name="keep_{g}_{i}" {checked}>'
                f'<span class="pack-src pack-src-{src}">{src}</span>'
                f'<code class="pack-data">{data}</code></label>')
        out.append(
            '</div>'
            f'<input class="pack-add" type="text" name="add_{g}" '
            f'placeholder="add {g} as JSON… (optional)"></div>')
    out.append(
        '</div>'
        '<div class="forge-create-actions pack-actions">'
        '<span class="muted">Dropping every item in a required group will '
        'block the seal.</span><div class="pack-action-btns">'
        '<button class="ara-btn" type="submit" name="save" value="1">Save curation</button>'
        '<button class="ara-btn ara-btn-primary" type="submit" name="seal" value="1">Seal pack</button>'
        '</div></div></form></section>')
    return "".join(out)
from .views import (
    _fmt_ago,
    _fmt_ts,
    _page,
    _render_armor_info_card,
    _render_creation_preview,
    _render_loadout_card,
    _static_css_href,
    e,
)


# ---------- shared utilities ----------

def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-limit:] if (limit and limit > 0) else out


def _session_is_running(session_dir: Path) -> bool:
    """True if this session's launch pid is a live ``armory-task`` process.
    Shared by the session resolver and the Spoils resolver so a live run is
    identified the same way in both (robust against stale/reused pids)."""
    meta = session_dir / "session.json"
    if not meta.is_file():
        return False
    try:
        pid = json.loads(meta.read_text()).get("pid")
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"armory-task" in fh.read()
    except OSError:
        return False


def _resolve_session_dir(state_root: Path, session_id: str | None = None) -> Path | None:
    sessions = state_root / "sessions"
    if not sessions.is_dir():
        return None
    if session_id:
        cand = sessions / session_id
        return cand if cand.is_dir() else None
    candidates = [c for c in sessions.iterdir() if c.is_dir()]
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda c: (-_session_activity_mtime_ns(c), c.name),
    )
    # 1) A currently-RUNNING task session wins — the operator is watching the
    #    active run, which must show immediately (even before it has produced
    #    any findings). Identified by a live session.json pid whose process is
    #    an `armory-task` launch (robust against stale/reused pids).
    for c in ordered:
        if _session_is_running(c):
            return c
    # 2) else the most-recently-active session that actually holds forge output
    #    (facts / leads / findings), so a work-less session doesn't shadow the
    #    real engagement. 3) then any session with task artifacts, 4) most-recent.
    def _has_forge_output(c: Path) -> bool:
        for name in ("findings.jsonl", "leads.jsonl", "facts.jsonl"):
            p = c / name
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False

    for c in ordered:
        if _has_forge_output(c):
            return c
    for c in ordered:
        if _session_has_task_artifacts(c):
            return c
    return ordered[0]


def _session_has_task_artifacts(session_dir: Path) -> bool:
    for name in (
        "chunk_history.jsonl",
        "activations.jsonl",
        "facts.jsonl",
        "leads.jsonl",
        "findings.jsonl",
        "anomalies.jsonl",
    ):
        path = session_dir / name
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    for row in _read_jsonl(session_dir / "events.jsonl", limit=200):
        if str(row.get("kind") or "").startswith("channel."):
            return True
    return False


def _session_dir_for_launch(state_root: Path, marker: dict | None) -> Path | None:
    if not marker:
        return None
    session_id = marker.get("session_id")
    if session_id:
        session_dir = _resolve_session_dir(state_root, str(session_id))
        if session_dir is not None:
            return session_dir
    pid = marker.get("pid")
    sessions = state_root / "sessions"
    if not isinstance(pid, int) or not sessions.is_dir():
        return None
    matches: list[Path] = []
    for candidate in sessions.iterdir():
        if not candidate.is_dir():
            continue
        try:
            meta = json.loads((candidate / "session.json").read_text())
        except Exception:
            continue
        if meta.get("pid") == pid:
            matches.append(candidate)
    if not matches:
        return None
    return sorted(matches, key=lambda c: (-_session_activity_mtime_ns(c), c.name))[0]


def _resolve_task_session_dir(state_root: Path, marker: dict | None = None) -> Path | None:
    by_marker = _session_dir_for_launch(state_root, marker)
    if by_marker is not None:
        return by_marker
    if marker and marker.get("channel_required"):
        fallback = _resolve_session_dir(state_root)
        if fallback is not None:
            return fallback
    sessions = state_root / "sessions"
    if not sessions.is_dir():
        return None
    candidates = [
        c for c in sessions.iterdir()
        if c.is_dir() and _session_has_task_artifacts(c)
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda c: (-_session_activity_mtime_ns(c), c.name),
    )[0]


def _session_activity_mtime_ns(session_dir: Path) -> int:
    try:
        newest = session_dir.stat().st_mtime_ns
    except OSError:
        return 0
    for child in session_dir.iterdir():
        try:
            newest = max(newest, child.stat().st_mtime_ns)
        except OSError:
            continue
    return newest


def _latest_per_id(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        rid = r.get("id")
        if rid:
            out[rid] = r
    return out


def _empty_state(title: str, message: str) -> str:
    return (
        f'<section class="card"><h2>{e(title)}</h2>'
        f'<p class="muted">{e(message)}</p></section>'
    )


def _session_pid_live(session_dir: Path | None) -> bool:
    if session_dir is None:
        return False
    meta_path = session_dir / "session.json"
    if not meta_path.is_file():
        return False
    try:
        pid = json.loads(meta_path.read_text()).get("pid")
    except Exception:
        return False
    return process_state_mod.is_pid_live(pid)


def _active_trim_label(state_root: Path, armor_name: str | None) -> str | None:
    if not armor_name:
        return None
    try:
        return trims_mod.active_trim_id(state_root, armor_name)
    except OSError:
        return None


def _active_temper_label(state_root: Path, armor_name: str | None) -> str | None:
    if not armor_name:
        return None
    try:
        return temper_mod.active_temper_id(state_root, armor_name)
    except OSError:
        return None


def _latest_task_launch(state_root: Path) -> tuple[Path, dict] | None:
    launch_dir = state_root / "task-launches"
    if not launch_dir.is_dir():
        return None
    candidates = [p for p in launch_dir.glob("task-*.json") if p.is_file()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(latest.read_text())
    except Exception:
        return None
    return latest, data


def _task_launch_live(marker: dict | None) -> bool:
    if not marker:
        return False
    status = str(marker.get("status") or "requested")
    if status in {"exited", "stopped", "failed"}:
        return False
    return process_state_mod.is_pid_live(marker.get("pid"))


def _task_launch_status(marker: dict | None) -> str:
    if not marker:
        return "none"
    status = str(marker.get("status") or "requested")
    if status in {"exited", "stopped", "failed"}:
        return status
    if _task_launch_live(marker):
        return "running"
    if status == "requested":
        return "stale"
    return status


def _render_latest_launch_card(state_root: Path) -> str:
    latest = _latest_task_launch(state_root)
    if latest is None:
        return ""
    marker_path, data = latest
    worker = data.get("worker_command") or []
    command = data.get("command") or []
    if not isinstance(worker, list):
        worker = [worker]
    if not isinstance(command, list):
        command = [command]
    pid = data.get("pid")
    status = _task_launch_status(data)
    log_path = Path(str(data.get("log") or ""))
    log_label = log_path.name if str(log_path) else "none"
    ts = data.get("ts")
    rows = [
        ("status", f'<span class="forge-phase-pill">{e(status)}</span>'),
        ("pid", f'<code>{e(pid or "unknown")}</code>'),
        ("task run", f'<code>{e(data.get("task_run_id") or "none")}</code>'),
        ("armor", f'<code>{e(data.get("armor_id") or "none")}</code>'),
        ("weapon", f'<code>{e(data.get("weapon_id") or "none")}</code>'),
        ("trim", f'<code>{e(data.get("trim_id") or "none")}</code>'),
        ("temper", f'<code>{e(data.get("temper_id") or "none")}</code>'),
        ("depth", f'<code>{e(data.get("task_depth") or "unspecified")}</code>'),
        ("worker", f'<code>{e(" ".join(str(x) for x in worker) or "unknown")}</code>'),
        ("marker", f'<code>{e(marker_path.name)}</code>'),
        ("log", f'<code>{e(log_label)}</code>'),
    ]
    if ts:
        rows.insert(1, ("requested", f'<span>{e(_fmt_ago(ts))} ago</span>'))
    body = "".join(
        '<div class="status-vital-row">'
        f'<span>{e(label)}</span><strong>{value}</strong>'
        '</div>'
        for label, value in rows
    )
    return (
        '<section class="card task-launch-card">'
        '<div class="forge-section-head">'
        '<div><div class="ara-eyebrow">Task launch</div>'
        '<h2>Latest launch</h2>'
        '<p class="muted">Last Start Task request recorded by Armory.</p>'
        '</div></div>'
        f'<div class="status-vital-rows">{body}</div>'
        '</section>'
    )


def _latest_launch_requires_channels(state_root: Path) -> bool:
    latest = _latest_task_launch(state_root)
    if latest is None:
        return False
    _marker_path, data = latest
    return bool(data.get("channel_required"))


def _session_channel_counts(session_dir: Path) -> Counter[str]:
    rows = _read_jsonl(session_dir / "events.jsonl")
    by_channel: Counter[str] = Counter()
    for r in rows:
        kind = r.get("kind") or ""
        if not kind.startswith("channel."):
            continue
        body = r.get("body") or {}
        ch = body.get("channel") or kind[len("channel."):]
        by_channel[str(ch)] += 1
    return by_channel


def _render_channel_requirement_card(state_root: Path, session_dir: Path | None) -> str:
    latest = _latest_task_launch(state_root)
    if session_dir is None or latest is None:
        return ""
    _marker_path, marker = latest
    if not marker.get("channel_required"):
        return ""
    gate = task_channel_gate_mod.evaluate_task_channel_gate(
        state_root,
        session_dir,
        marker,
        session_live=_session_pid_live(session_dir),
    )
    tracked_names = list(dict.fromkeys([
        *gate.required_channels,
        *task_channel_gate_mod.KNOWLEDGE_CHANNELS,
        "chunks.emit_tag",
    ]))
    rows = "".join(
        '<div class="status-vital-row">'
        f'<span>{e(name)}</span>'
        '<strong>'
        f'<code>{gate.counts.accepted.get(name, 0)}</code>'
        + (
            f' <span class="muted">/{gate.counts.total.get(name, 0)} total</span>'
            if gate.counts.total.get(name, 0) != gate.counts.accepted.get(name, 0)
            else ""
        )
        + '</strong>'
        '</div>'
        for name in tracked_names
    )
    details = [
        ("current chunk", gate.current_chunk or "unknown"),
        ("required", ", ".join(gate.required_channels) or "none"),
        ("missing", ", ".join(gate.missing_channels) or "none"),
        (
            "chunk transition",
            "present" if gate.transition_present else (
                "required" if gate.transition_required else "not required"
            ),
        ),
    ]
    detail_rows = "".join(
        '<div class="status-vital-row">'
        f'<span>{e(label)}</span><strong><code>{e(value)}</code></strong>'
        '</div>'
        for label, value in details
    )
    allowed = ", ".join(gate.allowed_channels) or "unknown"
    alias_rows = ""
    if gate.bad_aliases:
        alias_rows = (
            '<div class="task-channel-alert">'
            '<strong>Tool-name mismatch detected</strong>'
            '<p class="muted">Hermes referenced non-existent old MCP names. '
            'The exposed ARA tools are the exact channel names.</p>'
            '<div class="status-vital-rows">'
            + "".join(
                '<div class="status-vital-row">'
                f'<span><code>{e(alias)}</code></span>'
                f'<strong><code>{e(canonical)}</code></strong>'
                '</div>'
                for alias, canonical in gate.bad_aliases
            )
            + '</div></div>'
        )
    reason_line = ", ".join(gate.reasons) or "none"
    message = (
        "Structured channel output and phase movement are present for this task."
        if gate.status == "ok" else
        "The task is still running, but required structured ARA artifacts are not complete yet."
        if gate.status == "pending" else
        "No fact, lead, finding, or evidence channel calls have been recorded. "
        "The run is not complete even if the agent wrote prose."
    )
    return (
        '<section class="card task-channel-required-card">'
        '<div class="forge-section-head">'
        '<div><div class="ara-eyebrow">ARA channels</div>'
        '<h2>Structured output required</h2>'
        f'<p class="muted">{e(message)}</p></div>'
        f'<span class="forge-phase-pill">{e(gate.status)}</span>'
        '</div>'
        f'<p class="muted">Allowed by active chunk: <code>{e(allowed)}</code></p>'
        f'<p class="muted">Gate reasons: <code>{e(reason_line)}</code></p>'
        f'<div class="status-vital-rows">{detail_rows}</div>'
        f'<div class="status-vital-rows">{rows}</div>'
        f'{alias_rows}'
        '</section>'
    )


def _render_task_control_panel(
    state_root: Path,
    token: str,
    session_dir: Path | None,
) -> str:
    armor_name = armors_mod.active_armor_name(state_root)
    weapon_id = weapons_mod.equipped_weapon_id(state_root)
    trim_id = _active_trim_label(state_root, armor_name)
    temper_id = _active_temper_label(state_root, armor_name)
    preflight = task_preflight_mod.check_active_trim_for_task(state_root)
    latest = _latest_task_launch(state_root)
    marker = latest[1] if latest is not None else None
    live = _task_launch_live(marker) if marker is not None else _session_pid_live(session_dir)
    sid = session_dir.name if session_dir is not None else ""
    session_label = f"<code>{e(sid)}</code>" if sid else "<span>none</span>"
    session_label_name = "Active session" if live else "Latest session"
    blocked = not preflight.ok
    start_controls = (
        '<div class="task-blocked-box">'
        '<strong>Task blocked</strong>'
        f'<p class="muted">{e(preflight.message)}</p>'
        + (
            f'<p class="muted">Window end: <code>{e(preflight.window_end)}</code></p>'
            if preflight.window_end else ""
        )
        + '</div>'
        if blocked else
        '<form class="ara-form task-start-form" method="post" action="/engagement/start?t=' + e(token) + '">'
        '<label class="forge-field">Worker command'
        '<input type="text" name="worker_cmd" value="hermes" placeholder="hermes"></label>'
        '<label class="forge-field">Initial message'
        '<input type="text" name="prompt" placeholder="optional; sent as hermes -z"></label>'
        '<label class="forge-field">Task depth'
        '<select name="task_depth">'
        '<option value="full" selected>Full task</option>'
        '<option value="enum_only">Enum only</option>'
        '<option value="passive_probe">Passive probe</option>'
        '<option value="safe_mutation">Safe mutation</option>'
        '<option value="authenticated">Authenticated</option>'
        '<option value="exploit_validation">Exploit validation</option>'
        '<option value="writeup_only">Writeup only</option>'
        '</select></label>'
        '<label class="ara-check"><input type="checkbox" name="contain" checked> contain worker</label>'
        '<button class="ara-btn ara-btn-primary" type="submit">Start task</button>'
        '</form>'
    )
    stop_form = (
        f'<form method="post" action="/engagement/stop?t={e(token)}">'
        f'<input type="hidden" name="session_id" value="{e(sid)}">'
        '<button class="ara-btn ara-btn-bad" type="submit">Stop task</button>'
        '</form>'
        if live and sid else
        '<button class="ara-btn ara-btn-ghost" type="button" disabled>Stop task</button>'
    )
    return (
        '<section class="card task-control-card">'
        '<div class="task-control-head">'
        '<div><div class="ara-eyebrow">Task control</div>'
        '<h2>Start / stop</h2>'
        '<p class="ara-sub">Start a supervised worker with the currently '
        'equipped armor, weapon, and trim. Stop terminates the active '
        'supervisor session.</p></div>'
        f'<span class="forge-phase-pill">{"blocked" if blocked else ("running" if live else "standby")}</span>'
        '</div>'
        '<div class="task-loadout-strip">'
        f'<div><span>Armor</span><strong>{e(armor_name or "not equipped")}</strong></div>'
        f'<div><span>Weapon</span><strong>{e(weapon_id or "not equipped")}</strong></div>'
        f'<div><span>Trim</span><strong>{e(trim_id or "not applied")}</strong></div>'
        f'<div><span>Temper</span><strong>{e(temper_id or "not active")}</strong></div>'
        '</div>'
        '<div class="task-control-grid">'
        f'{start_controls}'
        '<div class="task-stop-box">'
        f'{stop_form}'
        f'<p class="muted">{session_label_name}: {session_label}</p>'
        '</div>'
        '</div>'
        '</section>'
    )


# ---------- /engagement ----------

def render_engagement(state_root: Path, token: str) -> str:
    """Top-level task view: equipped weapon + active session
    summary + chunk graph state + counts."""
    eq = weapons_mod.equipped_weapon_id(state_root)
    latest_launch = _latest_task_launch(state_root)
    session_dir = _resolve_task_session_dir(
        state_root,
        latest_launch[1] if latest_launch is not None else None,
    )

    parts = [
        '<section class="card ara-page-card">'
        '<div class="ara-eyebrow">Live</div>'
        '<h2 class="ara-h">Task</h2>'
    ]
    if eq is None:
        parts.append(
            '<p class="muted">No weapon equipped. '
            'Forge one and run <code>ara weapon equip &lt;id&gt;</code>, '
            'or pass <code>--weapon &lt;id&gt;</code> to a launcher.</p>'
        )
    else:
        parts.append(
            f'<p>Equipped weapon: <code>{e(eq)}</code> '
            f'<a href="/weapons/{e(eq)}?t={e(token)}">view weapon</a></p>'
        )
    if session_dir is None:
        parts.append('<p class="muted">No active session yet.</p>')
        parts.append('</section>')
        parts.append(_render_task_control_panel(state_root, token, session_dir))
        parts.append(_render_latest_launch_card(state_root))
        return _page("Task", "engagement", "".join(parts), token)
    parts.append(
        f'<p>Session: <code>{e(session_dir.name)}</code></p>'
    )
    parts.append('</section>')

    parts.append(_render_task_control_panel(state_root, token, session_dir))
    parts.append(_render_latest_launch_card(state_root))
    parts.append(_render_channel_requirement_card(state_root, session_dir))
    parts.append(_render_chunk_card(session_dir))
    parts.append(_render_activations_card(session_dir, token=token))
    parts.append(_render_counts_card(session_dir, token=token))
    parts.append(_render_recent_channels_card(session_dir, token=token))
    return _page("Task", "engagement", "".join(parts), token)


def _render_chunk_card(session_dir: Path) -> str:
    rows = _read_jsonl(session_dir / "chunk_history.jsonl")
    if not rows:
        return _empty_state(
            "Chunks",
            "No chunk runtime mounted yet (weapon not equipped, or "
            "task hasn't started)."
        )
    current = None
    for r in reversed(rows):
        if r.get("kind") in ("initialize", "swap", "jump", "rewind"):
            current = r.get("to_chunk")
            break
    head = f'<section class="card"><h2>Chunks</h2>'
    if current:
        head += f'<p>Active: <code>{e(current)}</code></p>'
    head += '<table class="ara-table"><thead><tr>'
    head += '<th>ts</th><th>kind</th><th>from</th><th>to</th>'
    head += '<th>tag</th><th>caller</th><th>activation</th>'
    head += '</tr></thead><tbody>'
    body_rows: list[str] = []
    for r in rows[-15:]:
        body_rows.append(
            "<tr>"
            f"<td><span class=muted>{e(_fmt_ago(r.get('ts')))}</span></td>"
            f"<td><code>{e(str(r.get('kind') or ''))}</code></td>"
            f"<td><code>{e(str(r.get('from_chunk') or ''))}</code></td>"
            f"<td><code>{e(str(r.get('to_chunk') or ''))}</code></td>"
            f"<td><code>{e(str(r.get('tag') or ''))}</code></td>"
            f"<td>{e(str(r.get('caller') or ''))}</td>"
            f"<td><code>{e(str(r.get('activation_id') or '')[:10])}</code></td>"
            "</tr>"
        )
    return head + "".join(body_rows) + '</tbody></table></section>'


def _render_activations_card(session_dir: Path, *,
                                  token: str = "") -> str:
    current_path = session_dir / "activation.txt"
    current = current_path.read_text().strip() if current_path.is_file() else None
    rows = _read_jsonl(session_dir / "activations.jsonl")
    if not rows and current is None:
        return ""
    head = '<section class="card"><h2>Activations</h2>'
    if current:
        head += f'<p>Current: <code>{e(current)}</code> · count: {len(rows)}'
        if token:
            head += (f' · <a href="/engagement/activations?t={e(token)}">'
                      'view all →</a>')
        head += '</p>'
    head += '<table class="ara-table"><thead><tr>'
    head += '<th>ts</th><th>id</th><th>reason</th><th>chunk</th>'
    head += '</tr></thead><tbody>'
    body = "".join(
        "<tr>"
        f"<td><span class=muted>{e(_fmt_ago(r.get('ts')))}</span></td>"
        f"<td><code>{e(str(r.get('activation_id') or '')[:16])}</code></td>"
        f"<td>{e(str(r.get('reason') or ''))}</td>"
        f"<td><code>{e(str(r.get('chunk_id') or ''))}</code></td>"
        "</tr>"
        for r in rows[-10:]
    )
    return head + body + '</tbody></table></section>'


def _render_counts_card(session_dir: Path, *, token: str = "") -> str:
    findings = _latest_per_id(_read_jsonl(session_dir / "findings.jsonl"))
    leads = _latest_per_id(_read_jsonl(session_dir / "leads.jsonl"))
    anomalies = _latest_per_id(_read_jsonl(session_dir / "anomalies.jsonl"))
    facts = _read_jsonl(session_dir / "facts.jsonl")

    sev = Counter((f.get("severity_name") or "?") for f in findings.values())
    lead_status = Counter((ld.get("status") or "?") for ld in leads.values())
    anom_pending = sum(1 for a in anomalies.values() if a.get("status") == "pending")

    evidence_total = 0
    db = session_dir / "evidence" / "index.db"
    if db.is_file():
        try:
            conn = sqlite3.connect(db)
            try:
                evidence_total = int(
                    conn.execute("SELECT count(*) FROM evidence").fetchone()[0]
                )
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass

    evidence_lbl = (
        f'<div class="lbl"><a href="/engagement/evidence?t={e(token)}">'
        'evidence</a></div>'
        if token else '<div class="lbl">evidence</div>'
    )
    return (
        '<section class="card"><h2>Knowledge production</h2>'
        '<div class="ara-stat-row">'
        f'<div class="stat"><div class="n">{len(findings)}</div>'
        f'<div class="lbl">findings</div>'
        f'<div class="muted">{e(_fmt_dist(sev))}</div></div>'
        f'<div class="stat"><div class="n">{len(leads)}</div>'
        f'<div class="lbl">leads</div>'
        f'<div class="muted">{e(_fmt_dist(lead_status))}</div></div>'
        f'<div class="stat"><div class="n">{anom_pending}</div>'
        f'<div class="lbl">anomalies pending</div>'
        f'<div class="muted">of {len(anomalies)} total</div></div>'
        f'<div class="stat"><div class="n">{len(facts)}</div>'
        f'<div class="lbl">facts</div></div>'
        f'<div class="stat"><div class="n">{evidence_total}</div>'
        f'{evidence_lbl}</div>'
        '</div></section>'
    )


def _fmt_dist(counter: Counter[str]) -> str:
    if not counter:
        return ""
    return ", ".join(f"{k}: {v}" for k, v in sorted(counter.items()))


def _render_recent_channels_card(session_dir: Path, *,
                                      token: str = "") -> str:
    rows = _read_jsonl(session_dir / "events.jsonl")
    by_channel: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    for r in rows:
        kind = r.get("kind") or ""
        if not kind.startswith("channel."):
            continue
        body = r.get("body") or {}
        ch = body.get("channel") or kind[len("channel."):]
        by_channel[ch] += 1
        if body.get("status") == "rejected":
            rejected[ch] += 1
    if not by_channel:
        return ""
    items = "".join(
        f"<tr><td><code>{e(name)}</code></td><td>{count}</td>"
        f"<td>{rejected.get(name, 0)}</td></tr>"
        for name, count in by_channel.most_common(20)
    )
    link = (f'<p><a href="/engagement/events?t={e(token)}">view all →</a></p>'
            if token else "")
    return (
        '<section class="card"><h2>Channel calls</h2>'
        + link +
        '<table class="ara-table"><thead>'
        '<tr><th>channel</th><th>total</th><th>rejected</th></tr>'
        f'</thead><tbody>{items}</tbody></table>'
        '</section>'
    )


# ---------- /engagement/activations ----------

def render_activations(state_root: Path, token: str, *,
                          limit: int = 500,
                          chunk_filter: str | None = None,
                          reason_filter: str | None = None) -> str:
    sd = _resolve_session_dir(state_root)
    parts: list[str] = ['<section class="card"><h2>Activations</h2>'
                          f'<p><a href="/engagement?t={e(token)}">'
                          '← task</a></p>']
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Activations", "engagement", "".join(parts), token)
    rows = _read_jsonl(sd / "activations.jsonl")
    chunks_log = _read_jsonl(sd / "chunk_history.jsonl")
    swaps_by_act: dict[str, list[dict]] = {}
    for s in chunks_log:
        aid = s.get("activation_id") or ""
        if aid:
            swaps_by_act.setdefault(aid, []).append(s)
    current = None
    cur_path = sd / "activation.txt"
    if cur_path.is_file():
        current = cur_path.read_text().strip() or None
    parts.append(
        f'<p>Session <code>{e(sd.name)}</code> · '
        f'{len(rows)} activation{"s" if len(rows) != 1 else ""} · '
        f'current: <code>{e(current or "—")}</code></p>'
    )
    if reason_filter or chunk_filter:
        parts.append(
            f'<p><a href="/engagement/activations?t={e(token)}">'
            'clear filters</a></p>'
        )
    parts.append('</section>')

    if not rows:
        parts.append(
            '<section class="card"><p class="muted">'
            'No activations recorded yet.</p></section>'
        )
        return _page("Activations", "engagement",
                      "".join(parts), token)

    # Filter chips: distinct reasons + chunks
    reasons = Counter(r.get("reason") or "?" for r in rows)
    chunks = Counter(r.get("chunk_id") or "?" for r in rows)
    parts.append('<section class="card">')
    if reasons:
        chips = "".join(
            f'<a href="/engagement/activations?t={e(token)}'
            f'&reason={e(name)}" class="ara-badge" '
            'style="text-decoration:none;margin:0 6px 4px 0;">'
            f'{e(name)} <span class="muted">({c})</span></a>'
            for name, c in reasons.most_common(8)
        )
        parts.append(
            '<p class="muted" style="margin:0 0 6px;">'
            'By reason: ' + chips + '</p>'
        )
    if chunks:
        chips = "".join(
            f'<a href="/engagement/activations?t={e(token)}'
            f'&chunk={e(name)}" class="ara-badge" '
            'style="text-decoration:none;margin:0 6px 4px 0;">'
            f'{e(name)} <span class="muted">({c})</span></a>'
            for name, c in chunks.most_common(8)
        )
        parts.append(
            '<p class="muted" style="margin:0 0 6px;">'
            'By chunk: ' + chips + '</p>'
        )
    parts.append('</section>')

    # Apply filters + compute durations (next.ts - this.ts)
    sorted_rows = sorted(rows, key=lambda r: r.get("ts") or 0.0)
    durations: dict[str, float] = {}
    for i, r in enumerate(sorted_rows):
        aid = r.get("activation_id") or ""
        nxt = sorted_rows[i + 1] if i + 1 < len(sorted_rows) else None
        if aid and nxt:
            try:
                durations[aid] = float(nxt.get("ts", 0.0)) - float(r.get("ts", 0.0))
            except (TypeError, ValueError):
                pass
    filtered = sorted_rows
    if reason_filter:
        filtered = [r for r in filtered if r.get("reason") == reason_filter]
    if chunk_filter:
        filtered = [r for r in filtered if r.get("chunk_id") == chunk_filter]
    # Show newest first; cap to ``limit``
    filtered = list(reversed(filtered))[:limit]
    if not filtered:
        parts.append(
            '<section class="card"><p class="muted">'
            'No activations match.</p></section>'
        )
        return _page("Activations", "engagement",
                      "".join(parts), token)

    body: list[str] = []
    for r in filtered:
        aid = r.get("activation_id") or ""
        is_current = aid and aid == current
        dur = durations.get(aid)
        dur_cell = f"{dur:.1f}s" if isinstance(dur, float) else "—"
        n_swaps = len(swaps_by_act.get(aid, []))
        body.append(
            "<tr>"
            f'<td>{e(_fmt_ago(r.get("ts")))}</td>'
            f'<td><code>{e(aid)}</code>'
            + (' <span class="ara-badge ok">current</span>'
               if is_current else "") +
            '</td>'
            f'<td>{e(str(r.get("reason") or ""))}</td>'
            f'<td><code>{e(str(r.get("chunk_id") or ""))}</code></td>'
            f'<td class="muted">{dur_cell}</td>'
            f'<td class="muted">{n_swaps}</td>'
            "</tr>"
        )
    parts.append(
        '<section class="card"><table class="ara-table"><thead><tr>'
        '<th>at</th><th>activation_id</th><th>reason</th>'
        '<th>chunk</th><th>duration</th><th>chunk swaps</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table></section>'
    )
    return _page("Activations", "engagement", "".join(parts), token)


# ---------- /engagement/events ----------

def render_events(state_root: Path, token: str, *,
                     limit: int = 500,
                     channel_filter: str | None = None,
                     status_filter: str | None = None,
                     activation_filter: str | None = None) -> str:
    sd = _resolve_session_dir(state_root)
    parts: list[str] = ['<section class="card"><h2>Channel events</h2>'
                          f'<p><a href="/engagement?t={e(token)}">'
                          '← task</a></p>']
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Events", "engagement", "".join(parts), token)
    rows = _read_jsonl(sd / "events.jsonl")
    if not rows:
        parts.append('<p class="muted">No events recorded yet.</p></section>')
        return _page("Events", "engagement", "".join(parts), token)

    # Channel-call rows only — non-channel audit kinds live on /forge/<id>/audit
    chan_rows: list[dict] = []
    for r in rows:
        kind = r.get("kind") or ""
        if not kind.startswith("channel."):
            continue
        body = r.get("body") or {}
        ch = body.get("channel") or kind[len("channel."):]
        chan_rows.append({
            "ts": r.get("ts"),
            "channel": ch,
            "status": body.get("status") or "",
            "activation_id": body.get("activation_id") or "",
            "caller": body.get("caller") or "",
            "reason": body.get("reason") or body.get("error") or "",
            "raw": r,
        })

    parts.append(
        f'<p>Session <code>{e(sd.name)}</code> · '
        f'{len(chan_rows)} channel event'
        f'{"s" if len(chan_rows) != 1 else ""}'
        f' (of {len(rows)} total log lines).</p>'
    )

    # Filter chips
    by_chan = Counter(r["channel"] for r in chan_rows)
    by_status = Counter(r["status"] for r in chan_rows if r["status"])
    chips: list[str] = []
    if by_chan:
        chips.append('<p class="muted" style="margin:4px 0;">By channel: '
                       + "".join(
            f'<a href="/engagement/events?t={e(token)}'
            f'&channel={e(name)}" class="ara-badge" '
            'style="text-decoration:none;margin:0 6px 4px 0;">'
            f'{e(name)} <span class="muted">({c})</span></a>'
            for name, c in by_chan.most_common(8)
        ) + '</p>')
    if by_status:
        chips.append('<p class="muted" style="margin:4px 0;">By status: '
                       + "".join(
            f'<a href="/engagement/events?t={e(token)}'
            f'&status={e(name)}" class="ara-badge" '
            'style="text-decoration:none;margin:0 6px 4px 0;">'
            f'{e(name)} <span class="muted">({c})</span></a>'
            for name, c in by_status.most_common()
        ) + '</p>')
    parts.extend(chips)
    if channel_filter or status_filter or activation_filter:
        parts.append(
            f'<p><a href="/engagement/events?t={e(token)}">'
            'clear filters</a></p>'
        )
    parts.append('</section>')

    filtered = chan_rows
    if channel_filter:
        filtered = [r for r in filtered if r["channel"] == channel_filter]
    if status_filter:
        filtered = [r for r in filtered if r["status"] == status_filter]
    if activation_filter:
        filtered = [r for r in filtered
                    if r["activation_id"] == activation_filter]
    # Newest first
    filtered = list(reversed(filtered))[:limit]
    if not filtered:
        parts.append(
            '<section class="card"><p class="muted">'
            'No events match.</p></section>'
        )
        return _page("Events", "engagement", "".join(parts), token)

    body_html: list[str] = []
    for r in filtered:
        status_cls = (
            "ara-badge ok" if r["status"] == "ok" else
            "ara-badge bad" if r["status"] == "rejected" else
            "ara-badge"
        )
        body_html.append(
            "<tr>"
            f'<td>{e(_fmt_ago(r["ts"]))}</td>'
            f'<td><code>{e(r["channel"])}</code></td>'
            f'<td>{e(r["caller"])}</td>'
            f'<td><span class="{status_cls}">{e(r["status"])}</span></td>'
            f'<td><code>{e(r["activation_id"])}</code></td>'
            f'<td>{e(str(r["reason"])[:60])}</td>'
            "</tr>"
        )
    parts.append(
        '<section class="card"><table class="ara-table"><thead><tr>'
        '<th>at</th><th>channel</th><th>caller</th><th>status</th>'
        '<th>activation</th><th>reason / error</th>'
        f'</tr></thead><tbody>{"".join(body_html)}</tbody></table></section>'
    )
    return _page("Events", "engagement", "".join(parts), token)


# ---------- /weapons ----------

def render_weapons(state_root: Path, token: str, *,
                    flash: str | None = None) -> str:
    eq = weapons_mod.equipped_weapon_id(state_root)
    weapons = weapons_mod.list_sealed_weapons(state_root)
    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
    ]
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section>'
        '<div class="ara-eyebrow">Barracks</div>'
        '<h2 class="ara-h" style="margin-bottom:4px">Weapons</h2>'
        '<p class="ara-sub">Sealed forge outputs that can be equipped on top '
        'of the standing armor policy.</p>'
    )
    if not weapons:
        parts.append(
            '<div class="ara-empty">None yet. '
            'Forge one with <code>ara forge new …</code> '
            'and seal it.</div>'
        )
        parts.append('</section>')
        return _page("Weapons", "weapons", "".join(parts), token)

    hero = eq if eq in weapons else weapons[0]
    rest = [wid for wid in weapons if wid != hero]

    def _seal(wid: str) -> dict:
        p = weapons_mod.weapon_dir(state_root, wid) / "seal.json"
        try:
            return json.loads(p.read_text()) if p.is_file() else {}
        except Exception:
            return {}

    def _weapon_counts(wid: str) -> dict[str, int]:
        wdir = weapons_mod.weapon_dir(state_root, wid)
        return {
            "chunks": len(list((wdir / "chunks").glob("*.json"))) if (wdir / "chunks").is_dir() else 0,
            "alphabets": len(list((wdir / "alphabets").glob("*.json"))) if (wdir / "alphabets").is_dir() else 0,
            "policy": len([p for p in (wdir / "policy").glob("*") if p.is_file()]) if (wdir / "policy").is_dir() else 0,
        }

    seal = _seal(hero)
    counts = _weapon_counts(hero)
    equipped = hero == eq
    focus_label = "Equipped weapon" if equipped else "Available weapon"
    focus_heading = "Active forge output" if equipped else "No weapon equipped"
    focus_detail = (
        "Sealed weapon policy is equipped."
        if equipped else
        "This sealed weapon is available, but no weapon is currently equipped."
    )
    action = (
        f'<form method="post" action="/weapons/unequip?t={e(token)}" style="margin:0">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        '<button type="submit" class="ara-btn ara-btn-primary">Unequip</button></form>'
        if equipped else
        f'<form method="post" action="/weapons/equip?t={e(token)}" style="margin:0">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        f'<input type="hidden" name="weapon_id" value="{e(hero)}">'
        '<button type="submit" class="ara-btn ara-btn-primary">Equip</button></form>'
    )
    parts.append(
        f'<section class="ara-armor-focus ara-weapon-focus" aria-label="{e(focus_label)}">'
        '<div class="ara-armor-focus-card">'
        '<div>'
        f'<div class="ara-eyebrow">{e(focus_label)}</div>'
        + _render_loadout_card(
            "weapon", "Weapon", hero,
            focus_detail,
            f"/weapons/{e(hero)}", token, glyph="W",
            meta_rows=[
                ("chunks", str(counts["chunks"])),
                ("alphabets", str(counts["alphabets"])),
                ("policy", str(counts["policy"])),
            ],
        )
        + '</div>'
        '<div class="ara-armor-focus-copy">'
        f'<span class="ara-eyebrow">{e(focus_heading)}</span>'
        f'<h3>{e(hero)}</h3>'
        '<p>Weapon overlays add forge-derived channels, alphabets, chunks, '
        'and policy material without replacing the equipped armor.</p>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">'
        + action
        + f'<a class="ara-btn ara-btn-ghost" href="/weapons/{e(hero)}?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">View</a>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="ara-armor-info-grid">'
        + _render_armor_info_card(
            "Seal",
            "equipped" if equipped else "sealed",
            "Operator-sealed forge output and hash metadata.",
            [
                ("hash", f'<code>{e(str(seal.get("weapon_hash") or "—")[:12])}</code>'),
                ("operator", e(seal.get("operator_id") or "—")),
                ("forge", f'<code>{e(seal.get("forge_id") or "—")}</code>'),
            ],
            glyph="S",
        )
        + _render_armor_info_card(
            "Channels",
            f'{counts["chunks"]} chunks',
            "Runtime route material available to the task loop.",
            [
                ("alphabets", str(counts["alphabets"])),
                ("policy", str(counts["policy"])),
                ("state", "equipped" if equipped else "standby"),
            ],
            glyph="C",
        )
        + _render_armor_info_card(
            "Overlay",
            "additive",
            "Weapon policy layers on top of the standing armor.",
            [
                ("mode", "hot-swap"),
                ("target", f'<code>{e(seal.get("weapon_target_id") or hero)}</code>'),
                ("warnings", str(len(seal.get("warnings") or []))),
            ],
            glyph="O",
        )
        + '</div>'
        '<details class="ara-armor-detail-inline">'
        '<summary><span class="ara-eyebrow">Detail</span>'
        '<strong>View weapon internals</strong></summary>'
        f'<pre class="ara-code">{e(json.dumps(seal, indent=2, sort_keys=True))}</pre>'
        '</details>'
        '</section>'
    )

    if rest:
        parts.append('<div class="ara-eyebrow" style="margin-top:32px">Other weapons</div>')
        parts.append(
            '<div class="ara-kit-grid" style="margin-top:8px">'
            + "".join(
                _render_loadout_card(
                    "weapon", "Weapon", wid,
                    "Sealed weapon ready.",
                    f"/weapons/{e(wid)}", token, glyph="W",
                    meta_rows=[
                        ("state", "standby"),
                        ("seal", "sealed"),
                    ],
                )
                for wid in rest
            )
            + '</div>'
        )
    parts.append('</section>')
    return _page("Weapons", "weapons", "".join(parts), token)


def render_weapon_detail(state_root: Path, weapon_id: str, token: str) -> str:
    wdir = weapons_mod.weapon_dir(state_root, weapon_id)
    if not weapons_mod.weapon_exists(state_root, weapon_id):
        body = (
            '<section class="card"><h2>Weapon not found</h2>'
            f'<p class="muted">No <code>seal.json</code> under '
            f'<code>{e(str(wdir))}</code>. Has it been sealed?</p>'
            f'<p><a href="/weapons?t={e(token)}">← back</a></p>'
            '</section>'
        )
        return _page("Weapon", "weapons", body, token)
    eq = weapons_mod.equipped_weapon_id(state_root) == weapon_id
    seal_path = wdir / "seal.json"
    try:
        seal = json.loads(seal_path.read_text()) if seal_path.is_file() else {}
    except Exception:
        seal = {}
    counts = {
        "chunks": len(list((wdir / "chunks").glob("*.json"))) if (wdir / "chunks").is_dir() else 0,
        "alphabets": len(list((wdir / "alphabets").glob("*.json"))) if (wdir / "alphabets").is_dir() else 0,
        "policy": len([p for p in (wdir / "policy").glob("*") if p.is_file()]) if (wdir / "policy").is_dir() else 0,
        "resources": 0,
    }
    try:
        weapon_data = json.loads((wdir / "weapon.json").read_text())
        counts["resources"] = len(weapon_data.get("resources") or [])
    except Exception:
        pass
    action = (
        f'<form method="post" action="/weapons/unequip?t={e(token)}" style="margin:0">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        '<button type="submit" class="ara-btn ara-btn-primary">Unequip</button></form>'
        if eq else
        f'<form method="post" action="/weapons/equip?t={e(token)}" style="margin:0">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        f'<input type="hidden" name="weapon_id" value="{e(weapon_id)}">'
        '<button type="submit" class="ara-btn ara-btn-primary">Equip</button></form>'
    )
    parts: list[str] = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section class="ara-armor-focus ara-weapon-detail-focus" aria-label="Weapon visual identity">'
        '<div class="ara-armor-focus-card">'
        '<div>'
        '<div class="ara-eyebrow">Weapon identity</div>'
        + _render_loadout_card(
            "weapon",
            "Weapon",
            weapon_id,
            "Equipped weapon policy." if eq else "Sealed weapon ready.",
            None,
            token,
            glyph="W",
            meta_rows=[
                ("chunks", str(counts["chunks"])),
                ("alphabets", str(counts["alphabets"])),
                ("policy", str(counts["policy"])),
                ("resources", str(counts["resources"])),
            ],
        )
        + '</div>'
        '<div class="ara-armor-focus-copy">'
        '<span class="ara-eyebrow">Manage forged weapon</span>'
        f'<h3>{e(weapon_id)}</h3>'
        '<p>Generated visual identity is deterministic from the weapon id, '
        'so the same sealed weapon keeps the same silhouette everywhere it is shown.</p>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">'
        + action
        + f'<a class="ara-btn ara-btn-ghost" href="/weapons?t={e(token)}" '
        'style="text-decoration:none;display:inline-flex;align-items:center">Back to weapons</a>'
        '</div>'
        '<div class="ara-gateway-row" style="justify-content:flex-start">'
        f'<span class="rh-badge">hash {e(str(seal.get("weapon_hash") or "—")[:12])}</span>'
        f'<span class="rh-badge">forge {e(seal.get("forge_id") or "—")}</span>'
        '</div>'
        '</div>'
        '</div>'
        '</section>',
    ]
    parts.append(
        f'<section class="card"><h2><code>{e(weapon_id)}</code> '
        + ('<span class="ara-badge ok">equipped</span>' if eq else "")
        + f'</h2><p><a href="/weapons?t={e(token)}">← weapons</a></p>'
        '</section>'
    )
    parts.append(_render_weapon_extractions(wdir))
    parts.append(_render_weapon_resources(wdir, weapon_id=weapon_id, token=token))
    parts.append(_render_weapon_alphabets(wdir))
    parts.append(_render_weapon_chunks(wdir))
    parts.append(_render_weapon_catalog(wdir))
    parts.append(_render_weapon_policy(wdir))
    parts.append(_render_weapon_calibration(wdir))
    parts.append(_render_weapon_seal(wdir))
    return _page(f"Weapon · {weapon_id}", "weapons",
                  "".join(parts), token)

def _render_weapon_resources(wdir: Path, *, weapon_id: str, token: str) -> str:
    weapon_path = wdir / "weapon.json"
    if not weapon_path.is_file():
        return ""
    try:
        weapon = json.loads(weapon_path.read_text())
    except Exception:
        return ""
    resources = weapon.get("resources") or []
    if not isinstance(resources, list) or not resources:
        return ""

    cards: list[str] = []
    for res in resources:
        if not isinstance(res, dict):
            continue
        rid = str(res.get("id") or "resource")
        cls = str(res.get("class") or res.get("resource_class") or "reference")
        name = str(res.get("name") or rid)
        locator = str(res.get("locator") or res.get("url") or res.get("host") or "")
        status = str(res.get("status") or ("resolved" if locator else "needs_resolution"))
        allowed = res.get("allowed_uses") or []
        approvals = res.get("requires_approval_for") or []
        hints = res.get("hints") or []
        resolution = res.get("resolution") or {}
        if not isinstance(allowed, list):
            allowed = [allowed]
        if not isinstance(approvals, list):
            approvals = [approvals]
        if not isinstance(hints, list):
            hints = [hints]
        if not isinstance(resolution, dict):
            resolution = {}
        rationale = str(res.get("rationale") or "")
        cite = (res.get("citation") or {}).get("manifest_element_id") or ""
        status_label = {
            "resolved": "resolved",
            "needs_resolution": "needs URL",
            "probe_failed": "probe failed",
            "ambiguous": "ambiguous",
        }.get(status, status or "unknown")
        status_class = {
            "resolved": "ok",
            "needs_resolution": "warn",
            "probe_failed": "bad",
            "ambiguous": "warn",
        }.get(status, "warn")
        locator_html = (
            f'<a href="{e(locator)}">{e(locator)}</a>'
            if locator.startswith(("http://", "https://"))
            else f'<code>{e(locator or "operator URL needed")}</code>'
        )
        resolution_bits = []
        if resolution.get("source"):
            resolution_bits.append(f'source {e(str(resolution.get("source")))}')
        if resolution.get("status_code"):
            resolution_bits.append(f'HTTP {e(str(resolution.get("status_code")))}')
        if resolution.get("error"):
            resolution_bits.append(e(str(resolution.get("error"))))
        resolution_html = (
            '<p class="muted">resolution: '
            + " · ".join(resolution_bits)
            + '</p>'
            if resolution_bits else ""
        )
        hints_html = (
            '<div class="ara-gateway-row resource-hints">'
            + "".join(
                f'<span class="rh-badge">hint {e(str(x))}</span>'
                for x in hints if str(x).strip()
            )
            + '</div>'
            if hints else ""
        )
        approval_action = ""
        if status in {"needs_resolution", "probe_failed", "ambiguous"}:
            approval_action = (
                f'<form method="post" action="/weapons/{e(weapon_id)}/resources/'
                f'{e(rid)}/request-url?t={e(token)}" class="resource-url-form">'
                f'<input type="hidden" name="suggested_url" value="{e(locator)}">'
                '<button class="ara-btn ara-btn-ghost" type="submit">'
                'Request URL approval</button>'
                '</form>'
            )
        cards.append(
            '<article class="forge-mini-weapon weapon-resource-card">'
            '<div class="forge-mini-head">'
            f'<span class="forge-phase-pill">{e(cls)}</span>'
            f'<span class="ara-badge {status_class}">{e(status_label)}</span>'
            f'<code>{e(rid)}</code>'
            '</div>'
            f'<h3>{e(name)}</h3>'
            f'<p>{locator_html}</p>'
            f'{resolution_html}'
            f'{hints_html}'
            '<div class="ara-gateway-row">'
            + "".join(
                f'<span class="rh-badge">allow {e(str(x))}</span>'
                for x in allowed if str(x).strip()
            )
            + "".join(
                f'<span class="rh-badge">approval {e(str(x))}</span>'
                for x in approvals if str(x).strip()
            )
            + '</div>'
            + (f'<p class="muted">{e(rationale)}</p>' if rationale else "")
            + (f'<p class="muted">cite: <code>{e(cite)}</code></p>' if cite else "")
            + approval_action
            + '</article>'
        )
    if not cards:
        return ""
    return (
        '<section class="card">'
        '<div class="forge-section-head">'
        '<div><div class="ara-eyebrow">Forge-curated runtime access</div>'
        '<h2>Weapon resources</h2>'
        '<p class="muted">Resources sealed by the forge for this weapon. '
        'Tasks may reference or fetch these with provenance; execution, '
        'scope expansion, or aggressive tooling still requires approval.</p>'
        '</div>'
        f'<span class="forge-phase-pill">{len(cards)} sealed</span>'
        '</div>'
        '<div class="forge-session-hand forge-resource-hand">'
        + "".join(cards)
        + '</div>'
        '</section>'
    )


def _render_weapon_extractions(wdir: Path) -> str:
    """Surface the agent-derived extraction collections (crumbs, shapes,
    stores, lines, fact_shapes) that live inside ``weapon.json``.

    Pack-defined alphabets are handled separately by
    :func:`_render_weapon_alphabets`. Without this section the weapon
    detail page silently understates a forge-built weapon by 90%+.

    Layout: a summary card with totals + per-collection drill-down. Each
    collection is a collapsed ``<details>`` block — a single peek row
    + a count, click to expand to full table. Keeps the page scannable
    even for 600-element forge runs.
    """
    weapon_path = wdir / "weapon.json"
    if not weapon_path.is_file():
        return ""
    try:
        weapon = json.loads(weapon_path.read_text())
    except Exception:
        return ""

    crumbs = weapon.get("crumbs") or []
    shapes = weapon.get("shapes") or []
    stores = weapon.get("stores") or []
    lines = weapon.get("lines") or []
    fact_shapes = weapon.get("fact_shapes") or []
    rhema_rules = weapon.get("rhema_rules") or []
    bulwark_rules = weapon.get("bulwark_rules") or []
    walls_paths = weapon.get("walls_paths") or []
    total = (
        len(crumbs) + len(shapes) + len(stores) + len(lines)
        + len(fact_shapes) + len(rhema_rules) + len(bulwark_rules)
        + len(walls_paths)
    )
    if total == 0:
        return ""

    forge_id = weapon.get("forge_id", "?")

    # Cross-reference manifest → mat name so we can show "wstg-v4.2.pdf"
    # instead of "elem_5c5421553…". The manifest lives in the forge
    # session dir, which sits under the same state-root as wdir's parent.
    # State-root = wdir.parent.parent (weapons/<id>/ → weapons/ → state/).
    state_root = wdir.parent.parent
    manifest_path = state_root / "forge" / str(forge_id) / "manifest.jsonl"
    elem_to_mat: dict[str, str] = {}
    mat_id_to_name: dict[str, str] = {}
    if manifest_path.is_file():
        try:
            for line in manifest_path.read_text().splitlines():
                if not line.strip():
                    continue
                m = json.loads(line)
                elem_to_mat[m.get("id", "")] = m.get("source_mat_id", "")
        except Exception:
            pass
    # Mat-id → original filename. ``meta.json`` files don't carry the
    # mat_id (it's a derived hash), so we recompute via the same scheme
    # the enumerator uses: mat_<sha256(filename + bytes)[:10]>.
    mats_dir = state_root / "forge" / str(forge_id) / "mats"
    if mats_dir.is_dir():
        import hashlib as _h
        try:
            for f in mats_dir.iterdir():
                if not f.is_file() or f.name.endswith(".meta.json"):
                    continue
                try:
                    digest = _h.sha256()
                    digest.update(f.name.encode("utf-8"))
                    digest.update(f.read_bytes())
                    mid = "mat_" + digest.hexdigest()[:10]
                    mat_id_to_name[mid] = f.name
                except OSError:
                    continue
        except OSError:
            pass

    def _source_label(entry: dict) -> tuple[str, str]:
        """Return (display_text, full_id) for the source-cite cell."""
        cite = entry.get("citation") or {}
        mid = cite.get("manifest_element_id") or ""
        if not mid:
            return ("—", "")
        mat_id = elem_to_mat.get(mid, "")
        mat_name = mat_id_to_name.get(mat_id, "")
        if mat_name:
            return (mat_name, mid)
        # Fallback: short element id
        return (mid[:14] + "…", mid)

    def _short(s, n: int = 80) -> str:
        s = (str(s) if s is not None else "").replace("\n", " ").strip()
        return s[: n - 1] + "…" if len(s) > n else s

    # Standard top-level fields every entry has (added by the channel
    # framework). Skip these when summarising body content so the
    # summary describes what the *agent* put in, not the bookkeeping.
    _META_KEYS = {
        "citation", "citations", "extraction_id", "ts", "manifest_element_id",
        "alphabet_name",
    }

    def _pick(entry: dict, *candidates) -> object:
        """First non-empty value from candidate keys. ``"a.b"`` syntax
        descends into a dict-valued sub-field (one level)."""
        if not isinstance(entry, dict):
            return None
        for key in candidates:
            if "." in key:
                outer, inner = key.split(".", 1)
                sub = entry.get(outer)
                if isinstance(sub, dict):
                    v = sub.get(inner)
                    if v not in (None, "", [], {}):
                        return v
            else:
                v = entry.get(key)
                if v not in (None, "", [], {}):
                    return v
        return None

    def _body_summary(entry: dict, max_len: int = 90) -> str:
        """Render a one-line summary of the body's user-visible fields
        when neither ``description`` nor ``name`` works. Picks 2-3
        non-meta fields and shows ``key=val``.
        """
        if not isinstance(entry, dict):
            return ""
        bits = []
        for k, v in entry.items():
            if k in _META_KEYS:
                continue
            if isinstance(v, str):
                if not v.strip():
                    continue
                bits.append(f"{k}={v}")
            elif isinstance(v, (int, float, bool)):
                bits.append(f"{k}={v}")
            elif isinstance(v, list):
                if not v:
                    continue
                bits.append(f"{k}[{len(v)}]")
            elif isinstance(v, dict):
                if not v:
                    continue
                bits.append(f"{k}{{{len(v)}}}")
            if len(bits) >= 4:
                break
        return _short(" · ".join(bits), max_len)

    # Stat tile factory — uniform card-like cells for the summary grid
    def _tile(num: int, label: str) -> str:
        return (
            '<div class="weapon-stat">'
            f'<div class="weapon-stat-num">{num}</div>'
            f'<div class="weapon-stat-label">{e(label)}</div>'
            '</div>'
        )

    summary = (
        '<section class="card">'
        '<h2 class="ara-h">Forge extractions</h2>'
        '<p class="muted">Agent-derived content — what the operator\'s '
        'mats actually contributed to this weapon. Pack-shipped '
        'alphabets are listed separately below.</p>'
        '<div class="weapon-stat-grid">'
        + _tile(total, "total entries")
        + _tile(len(crumbs), "crumbs · procs")
        + _tile(len(shapes), "shapes · concs")
        + _tile(len(stores), "stores")
        + _tile(len(lines), "lines")
        + _tile(len(fact_shapes), "fact_shapes")
        + _tile(len(rhema_rules), "rhema rules")
        + _tile(len(bulwark_rules), "bulwark rules")
        + _tile(len(walls_paths), "walls paths")
        + '</div>'
        f'<p class="muted" style="margin-top:14px">Source: forge '
        f'<code>{e(str(forge_id))}</code>. Each entry carries a '
        '<code>manifest_element_id</code> citation back to the source '
        'mat (hover the source cell for the full id).</p>'
        '</section>'
    )

    def _table(title: str, anchor: str, rows: list,
               cols: list[tuple[str, str]],
               name_extractor) -> str:
        """cols: list of (header, css-class). The lead column is "name"
        (always rendered first); each tuple maps header → field key
        per-row by position via the same-index extractor list. Source
        column auto-appended."""
        if not rows:
            return ""

        # Column widths: name 30%, each middle col splits remaining,
        # source 18% mono. We declare via colgroup for predictable
        # layout instead of letting browsers heuristic-fit.
        n_middle = len(cols)
        middle_pct = (52 // max(n_middle, 1)) if n_middle else 52
        colgroup = (
            '<colgroup>'
            '<col style="width:30%">'
            + (f'<col style="width:{middle_pct}%">' * n_middle)
            + '<col style="width:18%">'
            + '</colgroup>'
        )
        head_cols = (
            '<th>name</th>'
            + "".join(f'<th>{e(h)}</th>' for h, _ in cols)
            + '<th>source</th>'
        )

        def _row_html(entry: dict, get_field) -> str:
            name = name_extractor(entry) or "—"
            src_disp, src_full = _source_label(entry)
            tds = [f'<td><code title="{e(str(name))}">'
                   f'{e(_short(name, 70))}</code></td>']
            for h, _ in cols:
                v = get_field(entry, h)
                if isinstance(v, list):
                    text = f"{len(v)} item(s)"
                    title_attr = (
                        ", ".join(str(x)[:40] for x in v[:6])
                        if v else ""
                    )
                elif isinstance(v, dict):
                    keys = list(v.keys())[:4]
                    text = ", ".join(keys) + (
                        f" +{len(v) - 4}" if len(v) > 4 else ""
                    )
                    title_attr = json.dumps(v)[:200] if v else ""
                else:
                    text = _short(v, 80)
                    title_attr = str(v) if v else ""
                tds.append(
                    f'<td title="{e(title_attr)}">{e(text)}</td>'
                )
            tds.append(
                f'<td class="mono" title="{e(src_full)}">'
                f'<code>{e(src_disp)}</code></td>'
            )
            return "<tr>" + "".join(tds) + "</tr>"

        # Default field-extractor: look at body field by header name
        def _default_get(entry: dict, header: str):
            # Each cols entry's second item is the body key; fall through
            # to the header itself if no key
            for h, k in cols:
                if h == header:
                    return entry.get(k or h)
            return None

        # Preview: 1 row visible above the <details>. The collapsed
        # block shows the REMAINING rows (skip the peeked one) so the
        # operator doesn't see a duplicate at the top of the expansion.
        peek_html = _row_html(rows[0], _default_get) if rows else ""
        rest = rows[1:]
        rest_html = "".join(_row_html(r, _default_get) for r in rest)

        details_block = ""
        if rest:
            details_block = (
                '<details class="weapon-extract-details">'
                f'<summary>show {len(rest)} more</summary>'
                '<table class="ara-table weapon-extract-table">'
                f'{colgroup}'
                f'<tbody>{rest_html}</tbody>'
                '</table>'
                '</details>'
            )

        return (
            f'<section class="card weapon-extract-section" id="{e(anchor)}">'
            f'<h2 class="ara-h">{e(title)} '
            f'<span class="muted">· {len(rows)} entries</span></h2>'
            '<table class="ara-table weapon-extract-table">'
            f'{colgroup}'
            f'<thead><tr>{head_cols}</tr></thead>'
            f'<tbody>{peek_html}</tbody>'
            '</table>'
            f'{details_block}'
            '</section>'
        )

    # Section nav — anchors at top so operators can jump between
    # collections without scrolling the full set.
    nav = (
        '<div class="weapon-extract-nav">'
        f'<a href="#xtr-crumbs">crumbs ({len(crumbs)})</a>'
        f'<a href="#xtr-shapes">shapes ({len(shapes)})</a>'
        f'<a href="#xtr-stores">stores ({len(stores)})</a>'
        f'<a href="#xtr-lines">lines ({len(lines)})</a>'
        f'<a href="#xtr-fact-shapes">fact_shapes ({len(fact_shapes)})</a>'
        f'<a href="#xtr-rhema">rhema ({len(rhema_rules)})</a>'
        f'<a href="#xtr-bulwark">bulwark ({len(bulwark_rules)})</a>'
        f'<a href="#xtr-walls">walls ({len(walls_paths)})</a>'
        '</div>'
    )

    # Bodies vary widely (the agent invents its own schema per element).
    # Each per-section name/summary extractor falls back through the
    # most likely fields, then to a generic body-summary if none fit.
    def _name_crumb(x): return _pick(x, "name", "id", "title", "step_name")
    def _name_shape(x):
        return _pick(x, "name", "title", "id",
                     "entry.name", "entry.id", "entry.label",
                     "vulnerability_type", "category", "alphabet_name")
    def _name_store(x):
        return _pick(x, "name", "title", "id", "full_name", "device",
                     "tool_name", "resource", "store_name")
    def _name_line(x):
        return _pick(x, "name", "title", "id", "scope",
                     "section_heading", "boundary", "category")
    def _name_factshape(x):
        return _pick(x, "name", "title", "id", "predicate", "shape_name")

    def _summary_crumb(x):
        v = _pick(x, "description", "purpose", "summary")
        if v: return _short(v, 80)
        return _body_summary(x)
    def _summary_shape(x):
        # Many shapes have either {entry: {description}} OR top-level
        # fields like description/severity/finding_number.
        v = _pick(x, "description", "summary", "entry.description",
                  "entry.label", "purpose")
        if v: return _short(v, 80)
        return _body_summary(x)
    def _summary_store(x):
        v = _pick(x, "description", "summary", "purpose")
        if v: return _short(v, 80)
        return _body_summary(x)
    def _summary_line(x):
        v = _pick(x, "description", "scope_rationale", "boundary",
                  "category", "purpose")
        if v: return _short(v, 80)
        return _body_summary(x)
    def _summary_factshape(x):
        v = _pick(x, "description", "purpose", "summary")
        if v: return _short(v, 80)
        return _body_summary(x)

    def _info_crumb(x):
        steps = x.get("steps") or []
        if isinstance(steps, list) and steps:
            return f"{len(steps)} step{'s' if len(steps)!=1 else ''}"
        return _short(_pick(x, "category", "phase_mapping", "id"), 60)
    def _info_shape(x):
        # Show the alphabet/category bucket if available
        return _short(_pick(x, "alphabet_name", "category",
                            "vulnerability_type", "severity",
                            "entry.id"), 60)
    def _info_store(x):
        cats = x.get("categories") or []
        if isinstance(cats, list) and cats:
            return f"{len(cats)} categor{'ies' if len(cats)!=1 else 'y'}"
        # Fall back to other "what kind of thing is this" hints
        return _short(_pick(x, "device", "tool_metadata",
                            "resource_type", "category"), 60)
    def _info_line(x):
        ins = x.get("in_scope") or []
        outs = x.get("out_of_scope") or []
        if isinstance(ins, list) or isinstance(outs, list):
            ni = len(ins) if isinstance(ins, list) else 0
            no = len(outs) if isinstance(outs, list) else 0
            if ni or no:
                return f"in:{ni} · out:{no}"
        return _short(_pick(x, "scope", "direction", "boundary"), 60)
    def _info_factshape(x):
        fields = x.get("fields") or []
        if isinstance(fields, list) and fields:
            return f"{len(fields)} field{'s' if len(fields)!=1 else ''}"
        return _short(_pick(x, "shape_kind", "domain"), 60)

    def _section(title, anchor, rows, summary_fn, info_fn,
                 info_header, name_fn) -> str:
        """Local override of _table that uses callable extractors per
        column. Three-column layout: name / summary / source.
        ``info_fn`` output (when non-empty) gets folded inline into
        the summary as a small dim-suffix; this keeps the table dense
        without a half-empty column for sections where the agent
        didn't consistently emit count-shaped fields."""
        if not rows:
            return ""
        head_cols = (
            "<th>name</th>"
            "<th>summary</th>"
            "<th>source</th>"
        )
        colgroup = (
            '<colgroup>'
            '<col style="width:30%">'
            '<col style="width:50%">'
            '<col style="width:20%">'
            '</colgroup>'
        )

        def _row_html(entry: dict) -> str:
            name = name_fn(entry)
            if not name:
                # Fall back to listing the body's first non-meta key
                # so the operator at least sees what's in the cell.
                fallback = ""
                for k in entry.keys():
                    if k not in _META_KEYS:
                        fallback = f"&lt;{k}&gt;"
                        break
                name = fallback or "&lt;unnamed&gt;"
            summary = summary_fn(entry) or ""
            info = info_fn(entry) or ""
            src_disp, src_full = _source_label(entry)
            summary_html = e(summary)
            if info:
                summary_html += (
                    f' <span class="weapon-extract-info">'
                    f'· {e(str(info))}</span>'
                )
            # name is pre-escaped only via fallback; safe to e() user-derived
            name_str = str(name) if not name.startswith("&") else name
            name_disp = (
                _short(name_str, 60) if not name_str.startswith("&")
                else name_str
            )
            return (
                "<tr>"
                f'<td><code title="{e(str(name_str))}">'
                f'{name_disp if name_str.startswith("&") else e(name_disp)}'
                '</code></td>'
                f'<td title="{e(str(summary))}">{summary_html}</td>'
                f'<td class="mono" title="{e(src_full)}">'
                f'<code>{e(src_disp)}</code></td>'
                "</tr>"
            )

        peek_html = _row_html(rows[0])
        rest = rows[1:]
        rest_html = "".join(_row_html(r) for r in rest)
        details_block = (
            (
                '<details class="weapon-extract-details">'
                f'<summary>show {len(rest)} more</summary>'
                '<table class="ara-table weapon-extract-table">'
                f'{colgroup}'
                f'<tbody>{rest_html}</tbody>'
                '</table>'
                '</details>'
            ) if rest else ""
        )
        return (
            f'<section class="card weapon-extract-section" id="{e(anchor)}">'
            f'<h2 class="ara-h">{e(title)} '
            f'<span class="muted">· {len(rows)} entries</span></h2>'
            '<table class="ara-table weapon-extract-table">'
            f'{colgroup}'
            f'<thead><tr>{head_cols}</tr></thead>'
            f'<tbody>{peek_html}</tbody>'
            '</table>'
            f'{details_block}'
            '</section>'
        )

    sections = [summary, nav]
    sections.append(_section(
        "Crumbs (procedures)", "xtr-crumbs", crumbs,
        _summary_crumb, _info_crumb, "steps", _name_crumb,
    ))
    sections.append(_section(
        "Shapes (concepts / templates)", "xtr-shapes", shapes,
        _summary_shape, _info_shape, "alphabet/cat", _name_shape,
    ))
    sections.append(_section(
        "Stores (resources)", "xtr-stores", stores,
        _summary_store, _info_store, "categories", _name_store,
    ))
    sections.append(_section(
        "Lines (scope boundaries)", "xtr-lines", lines,
        _summary_line, _info_line, "in/out", _name_line,
    ))
    sections.append(_section(
        "Fact shapes (assertion schemas)", "xtr-fact-shapes", fact_shapes,
        _summary_factshape, _info_factshape, "fields", _name_factshape,
    ))

    # Policy extractions — separate set of name/summary/info extractors
    # tuned to the rule-shape body that extract.rhema_rule /
    # extract.bulwark_rule / extract.walls_path produce.
    def _name_policy(x):
        return _pick(x, "name", "id", "title")
    def _summary_rhema(x):
        v = _pick(x, "rationale", "evidence_quote", "description")
        if v: return _short(v, 80)
        return _body_summary(x)
    def _info_rhema(x):
        action = _pick(x, "action") or ""
        layer = _pick(x, "layer") or ""
        return f"{action} · {layer}".strip(" ·") or "—"
    def _info_bulwark(x):
        action = _pick(x, "action") or ""
        layer = _pick(x, "layer") or ""
        return f"{action} · {layer}".strip(" ·") or "—"
    def _info_walls(x):
        return _short(_pick(x, "kind", "value"), 40) or "—"

    sections.append(_section(
        "Rhema rules (prompt mediation)", "xtr-rhema", rhema_rules,
        _summary_rhema, _info_rhema, "action·layer", _name_policy,
    ))
    sections.append(_section(
        "Bulwark rules (layered policy)", "xtr-bulwark", bulwark_rules,
        _summary_rhema, _info_bulwark, "action·layer", _name_policy,
    ))
    sections.append(_section(
        "WALLS paths (resource denies)", "xtr-walls", walls_paths,
        _summary_rhema, _info_walls, "kind", _name_policy,
    ))
    return "".join(sections)


def _render_weapon_alphabets(wdir: Path) -> str:
    a_dir = wdir / "alphabets"
    if not a_dir.is_dir():
        return ""
    files = sorted(p for p in a_dir.glob("*.json")
                    if p.name not in ("_pack.json", "_conflicts.json"))
    if not files:
        return ""
    pack_meta = a_dir / "_pack.json"
    pack_label = ""
    if pack_meta.is_file():
        try:
            d = json.loads(pack_meta.read_text())
            pack_label = f' (pack <code>{e(str(d.get("name", "")))}</code>'
            v = d.get("version")
            if v:
                pack_label += f' v{e(str(v))}'
            pack_label += ")"
        except Exception:
            pass
    parts = [f'<section class="card"><h2>Alphabets{pack_label}</h2>']
    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        shape = data.get("shape", "?")
        if shape == "categorical":
            entries = data.get("entries") or []
            parts.append(
                f'<h3><code>{e(f.stem)}</code> <span class="muted">categorical · {len(entries)} entries</span></h3>'
            )
            if entries:
                def _origin(ent: dict) -> tuple[str, str]:
                    """(class, label) — categorise the entry's
                    provenance so blank cells become honest signals
                    instead of formatting noise.
                    Pack-declared = the alphabet pack lists this id
                    canonically (no label, no source field).
                    Starter = pack-shipped opt-in entry, ``source``
                    field reads "starter" (only present when
                    ``allow_starter_alphabets=True`` was set on seal).
                    Agent = produced by extract.conc with a real
                    ``description``; no source field set."""
                    src = ent.get("source") or ""
                    if src == "starter":
                        return ("origin-starter", "starter")
                    if src:
                        return ("origin-other", src)
                    if ent.get("description") or ent.get("label"):
                        return ("origin-agent", "agent")
                    return ("origin-pack", "pack")

                def _row(ent):
                    cls, otext = _origin(ent)
                    label = (
                        ent.get("label")
                        or ent.get("description")
                        or ""
                    )
                    label_html = (
                        f"<span>{e(label)}</span>" if label
                        else f'<span class="muted">—</span>'
                    )
                    return (
                        "<tr>"
                        f"<td><code>{e(ent.get('id',''))}</code></td>"
                        f"<td>{label_html}</td>"
                        f'<td><span class="alpha-origin {cls}">'
                        f'{e(otext)}</span></td>'
                        "</tr>"
                    )
                rows = "".join(_row(ent) for ent in entries[:30])
                more = (
                    f'<p class="muted">… +{len(entries) - 30} more</p>'
                    if len(entries) > 30 else ""
                )
                parts.append(
                    '<table class="ara-table"><thead>'
                    '<tr><th>id</th><th>label</th><th>origin</th></tr>'
                    f'</thead><tbody>{rows}</tbody></table>{more}'
                )
        elif shape == "matrix":
            cells = data.get("cells") or []
            axes = data.get("axes") or []
            parts.append(
                f'<h3><code>{e(f.stem)}</code> <span class="muted">matrix · '
                f'{len(cells)} cells · axes {e(", ".join(axes))}</span></h3>'
            )
            if cells:
                rows = "".join(
                    f"<tr><td><code>{e(json.dumps(c.get('coords') or {}, sort_keys=True))}</code></td>"
                    f"<td>{e(str(c.get('score','')))}</td>"
                    f"<td>{e(str(c.get('bucket','')))}</td></tr>"
                    for c in cells[:30]
                )
                parts.append(
                    '<table class="ara-table"><thead>'
                    '<tr><th>coords</th><th>score</th><th>bucket</th></tr>'
                    f'</thead><tbody>{rows}</tbody></table>'
                )
        elif shape == "template_set":
            tmpls = data.get("templates") or []
            parts.append(
                f'<h3><code>{e(f.stem)}</code> <span class="muted">template_set · {len(tmpls)} entries</span></h3>'
            )
            if tmpls:
                def _row_t(t):
                    src = t.get("source") or ""
                    if src == "starter":
                        cls, otext = "origin-starter", "starter"
                    elif src:
                        cls, otext = "origin-other", src
                    elif t.get("body") and not src:
                        cls, otext = "origin-agent", "agent"
                    else:
                        cls, otext = "origin-pack", "pack"
                    cats = t.get("applicable_categories") or []
                    title = t.get("title") or ""
                    title_html = (
                        f"<span>{e(title)}</span>" if title
                        else '<span class="muted">—</span>'
                    )
                    return (
                        "<tr>"
                        f"<td><code>{e(t.get('id',''))}</code></td>"
                        f"<td>{title_html}</td>"
                        f"<td><code>{e(', '.join(cats))}</code></td>"
                        f'<td><span class="alpha-origin {cls}">'
                        f'{e(otext)}</span></td>'
                        "</tr>"
                    )
                rows = "".join(_row_t(t) for t in tmpls[:30])
                parts.append(
                    '<table class="ara-table"><thead>'
                    '<tr><th>id</th><th>title</th>'
                    '<th>applicable</th><th>origin</th></tr>'
                    f'</thead><tbody>{rows}</tbody></table>'
                )
        elif shape == "vector":
            axes = data.get("axes") or []
            parts.append(
                f'<h3><code>{e(f.stem)}</code> <span class="muted">vector · {len(axes)} axes</span></h3>'
            )
            if axes:
                rows = "".join(
                    f"<tr><td><code>{e(ax.get('name',''))}</code></td>"
                    f"<td>{e(str(ax.get('min','')))}…{e(str(ax.get('max','')))}</td>"
                    f"<td>{e(ax.get('description','') or '')}</td></tr>"
                    for ax in axes
                )
                parts.append(
                    '<table class="ara-table"><thead>'
                    '<tr><th>axis</th><th>range</th><th>description</th></tr>'
                    f'</thead><tbody>{rows}</tbody></table>'
                )
    parts.append('</section>')
    return "".join(parts)


def _render_weapon_chunks(wdir: Path) -> str:
    cdir = wdir / "chunks"
    if not cdir.is_dir():
        return ""
    chunks: list[dict] = []
    for f in sorted(cdir.glob("*.json")):
        try:
            chunks.append(json.loads(f.read_text()))
        except Exception:
            continue
    if not chunks:
        return ""
    rows = "".join(
        "<tr>"
        f"<td><code>{e(c.get('id',''))}</code>"
        + (' <span class="ara-badge ok">entry</span>'
           if c.get("is_entry") else "")
        + (' <span class="ara-badge">terminal</span>'
           if c.get("is_terminal") else "")
        + (' <span class="ara-badge warn">finding</span>'
           if c.get("can_emit_finding") else "")
        + "</td>"
        f"<td>{e(c.get('title',''))}</td>"
        f"<td><code>{e(', '.join(c.get('tool_surface') or []))}</code></td>"
        f"<td><code>{e(', '.join((c.get('exits') or {}).keys()))}</code></td>"
        "</tr>"
        for c in chunks
    )
    return (
        f'<section class="card"><h2>Chunks <span class="muted">({len(chunks)})</span></h2>'
        + _render_chunk_graph(chunks)
        + '<table class="ara-table"><thead><tr>'
        '<th>id</th><th>title</th><th>tool surface</th><th>exits</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></section>'
    )


def _layered_chunk_positions(chunks: list[dict]) -> dict[str, dict]:
    """Sugiyama-lite: place chunks on layers by BFS depth from entries.

    Returns ``{chunk_id: {"x": ..., "y": ..., "depth": ...,
    "is_back": False}}``. Edges where target depth ≤ source depth are
    "back-edges" — caller styles them lighter so the forward flow stays
    readable.
    """
    ids = [c.get("id", "") for c in chunks if c.get("id")]
    by_id = {c.get("id"): c for c in chunks if c.get("id")}
    entries = [i for i in ids if by_id[i].get("is_entry")]
    if not entries and ids:
        entries = [ids[0]]
    depth: dict[str, int] = {i: 0 for i in entries}
    queue = list(entries)
    while queue:
        cur = queue.pop(0)
        cur_d = depth.get(cur, 0)
        exits = (by_id[cur].get("exits") or {}).items()
        for _tag, target in exits:
            if not isinstance(target, dict):
                continue
            tgt = target.get("next_chunk_id")
            if not tgt or tgt not in by_id:
                continue
            if tgt not in depth:
                depth[tgt] = cur_d + 1
                queue.append(tgt)
    # Anything still missing depth (e.g. isolated nodes) goes on a final
    # row so they don't overlap entry chunks.
    max_seen = max(depth.values(), default=0)
    for i in ids:
        if i not in depth:
            depth[i] = max_seen + 1
    # Bucket nodes by depth so each layer can spread horizontally.
    layers: dict[int, list[str]] = {}
    for i in ids:
        layers.setdefault(depth[i], []).append(i)
    # Stable order: terminal nodes drift right, finding-emitters drift
    # left within their layer. Otherwise keep declaration order.
    def _layer_sort_key(i: str) -> tuple:
        c = by_id[i]
        is_term = bool(c.get("is_terminal"))
        is_find = bool(c.get("can_emit_finding"))
        # finding=0, normal=1, terminal=2 — left-to-right
        bucket = 0 if is_find else (2 if is_term else 1)
        return (bucket, i)
    for d in layers:
        layers[d].sort(key=_layer_sort_key)

    out: dict[str, dict] = {}
    pad_x = 100
    pad_y = 90
    col_w = 150
    row_h = 110
    max_cols = max((len(v) for v in layers.values()), default=1)
    canvas_w = pad_x + col_w * max_cols
    for d, ids_in_layer in layers.items():
        # Center-align each row inside the canvas
        row_w = col_w * len(ids_in_layer)
        x0 = (canvas_w - row_w) / 2 + col_w / 2
        for col, i in enumerate(ids_in_layer):
            out[i] = {
                "x": x0 + col * col_w,
                "y": pad_y + d * row_h,
                "depth": d,
            }
    return out


def _render_chunk_graph(chunks: list[dict]) -> str:
    """Render the chunk graph as a cytoscape.js visualization.

    Layered (top-down) DAG layout: BFS depth from entries assigns
    rows; cycles get classified as back-edges and rendered lighter +
    dashed so the forward flow stays readable. Edge labels live in the
    Chunks table below — keeping them off the graph prevents the
    label-pile-up that small dense topologies cause.
    """
    positions = _layered_chunk_positions(chunks)
    node_depth = {i: p["depth"] for i, p in positions.items()}
    nodes: list[dict] = []
    edges: list[dict] = []
    for c in chunks:
        cid = c.get("id", "")
        cls = []
        if c.get("is_entry"): cls.append("entry")
        if c.get("is_terminal"): cls.append("terminal")
        if c.get("can_emit_finding"): cls.append("finding")
        node = {
            "data": {
                "id": cid,
                "label": cid,
                "title": c.get("title", "") or "",
            },
            "classes": " ".join(cls),
        }
        if cid in positions:
            node["position"] = {
                "x": positions[cid]["x"],
                "y": positions[cid]["y"],
            }
        nodes.append(node)
        for tag, target in (c.get("exits") or {}).items():
            if not isinstance(target, dict):
                continue
            tgt_id = target.get("next_chunk_id")
            if not tgt_id:
                continue
            caller = target.get("requires_caller") or "any"
            src_d = node_depth.get(cid, 0)
            tgt_d = node_depth.get(tgt_id, 0)
            edge_classes = [f"caller-{caller}"]
            if cid == tgt_id:
                edge_classes.append("self-loop")
            elif tgt_d <= src_d:
                edge_classes.append("back-edge")
            edges.append({
                "data": {
                    "id": f"{cid}--{tag}",
                    "source": cid,
                    "target": tgt_id,
                    "tag": tag,
                    "caller": caller,
                },
                "classes": " ".join(edge_classes),
            })
    payload = {"nodes": nodes, "edges": edges}
    return (
        '<div id="ara-chunk-graph" '
        'style="height:420px;border:1px solid var(--border, #2a2a2a);'
        'border-radius:6px;background:var(--card, #111);'
        'margin:8px 0 14px;"></div>'
        '<p class="muted" style="margin-top:0">'
        'Green = entry · Red = terminal · Orange = can emit findings · '
        'Solid line = agent · Dashed = operator · Dotted = system · '
        'Light = any · '
        'Faint dashed = back-edge (cycle) · '
        'Hover an edge for tag.</p>'
        '<script src="/static/vendor/cytoscape.min.js"></script>'
        '<script>'
        '(function(){'
        '  if (!window.cytoscape) return;'
        '  var els = ' + json.dumps(payload) + ';'
        '  var stylesheet = ['
        '    {selector:"node",style:{'
        '      "label":"data(label)","color":"#cfcfcf","font-size":"12px",'
        '      "text-valign":"center","text-halign":"center",'
        '      "background-color":"#3a4250","border-width":1,'
        '      "border-color":"#5a6270","width":92,"height":36,'
        '      "shape":"round-rectangle"}},'
        '    {selector:"node.entry",style:{'
        '      "background-color":"#1a7a35","border-color":"#2dbb52"}},'
        '    {selector:"node.terminal",style:{'
        '      "background-color":"#7a1a1a","border-color":"#bb2d2d"}},'
        '    {selector:"node.finding",style:{'
        '      "background-color":"#7a4a1a","border-color":"#bb7a2d"}},'
        '    {selector:"edge",style:{'
        '      "curve-style":"bezier","control-point-step-size":34,'
        '      "line-color":"#7a8290","target-arrow-color":"#7a8290",'
        '      "target-arrow-shape":"triangle","arrow-scale":0.9,'
        '      "width":1.5,"opacity":0.85}},'
        # Back-edges (cycles) — rendered lighter + dashed so the forward
        # flow remains the visual focus.
        '    {selector:"edge.back-edge",style:{'
        '      "line-style":"dashed","line-color":"#3a4250",'
        '      "target-arrow-color":"#3a4250","opacity":0.5,"width":1}},'
        '    {selector:"edge.self-loop",style:{'
        '      "loop-direction":"-90deg","loop-sweep":"45deg",'
        '      "control-point-step-size":42,'
        '      "line-color":"#5a6270","target-arrow-color":"#5a6270",'
        '      "opacity":0.6}},'
        '    {selector:"edge.caller-operator",style:{"line-style":"dashed"}},'
        '    {selector:"edge.caller-system",style:{"line-style":"dotted"}},'
        '    {selector:"edge.caller-any",style:{"line-color":"#3a4250",'
        '      "target-arrow-color":"#3a4250"}},'
        # Hover state — only the focused edge shows its tag, so labels
        # don't pile up on the small canvas.
        '    {selector:"edge.hover",style:{'
        '      "label":"data(tag)","color":"#fff","font-size":"11px",'
        '      "text-background-color":"#111","text-background-opacity":0.85,'
        '      "text-background-padding":3,'
        '      "line-color":"#cfcfcf","target-arrow-color":"#cfcfcf",'
        '      "opacity":1,"width":2.2,"z-index":99}},'
        '  ];'
        '  var cy = cytoscape({'
        '    container: document.getElementById("ara-chunk-graph"),'
        '    elements: els,'
        '    style: stylesheet,'
        '    layout: {name:"preset", fit:true, padding:30},'
        '    wheelSensitivity: 0.2,'
        '  });'
        # Hover handlers reveal the edge tag on demand.
        '  cy.on("mouseover", "edge", function(ev){'
        '    ev.target.addClass("hover");'
        '  });'
        '  cy.on("mouseout", "edge", function(ev){'
        '    ev.target.removeClass("hover");'
        '  });'
        '})();'
        '</script>'
    )


def _render_catalog_graph(channels: list[dict], edges: list[dict]) -> str:
    """Cytoscape graph of channels (nodes) + their static edges
    (cross-channel calls). Nodes carry classes per top-level subsystem
    (the part before ``.`` in the channel name) so callers can see
    fact / lead / finding / anomaly groupings at a glance."""
    nodes: list[dict] = []
    for c in channels:
        name = c.get("name", "")
        subsys = name.split(".", 1)[0] if "." in name else "other"
        callers = ", ".join(c.get("callers") or []) or "—"
        nodes.append({
            "data": {
                "id": name, "label": name,
                "title": f"{name} · sink {c.get('sink_subsystem','')} · "
                         f"callers {callers}",
            },
            "classes": f"subsys-{subsys}",
        })
    edge_dicts: list[dict] = []
    for i, ed in enumerate(edges):
        src = ed.get("from") or ed.get("source")
        tgt = ed.get("to") or ed.get("target")
        if not src or not tgt:
            continue
        edge_dicts.append({
            "data": {
                "id": f"e{i}", "source": src, "target": tgt,
                "label": ed.get("kind", ""),
            },
        })
    payload = {"nodes": nodes, "edges": edge_dicts}
    return (
        '<div id="ara-catalog-graph" '
        'style="height:340px;border:1px solid var(--border, #2a2a2a);'
        'border-radius:6px;background:var(--card, #111);'
        'margin:8px 0 14px;"></div>'
        '<p class="muted" style="margin-top:0">'
        'Color groups by subsystem (fact / lead / finding / anomaly / …). '
        'Edges are static cross-channel calls detected at seal time.</p>'
        '<script src="/static/vendor/cytoscape.min.js"></script>'
        '<script>'
        '(function(){'
        '  if (!window.cytoscape) return;'
        '  var els = ' + json.dumps(payload) + ';'
        '  var palette = {'
        '    "subsys-fact":"#1a4a7a","subsys-lead":"#1a7a35",'
        '    "subsys-finding":"#7a4a1a","subsys-anomaly":"#7a1a4a",'
        '    "subsys-evidence":"#4a1a7a","subsys-channel":"#3a4250",'
        '    "subsys-style":"#5a6a1a","subsys-extract":"#1a7a7a",'
        '    "subsys-other":"#3a4250"'
        '  };'
        '  var stylesheet = ['
        '    {selector:"node",style:{'
        '      "label":"data(label)","color":"#cfcfcf","font-size":"10px",'
        '      "text-valign":"center","text-halign":"center",'
        '      "background-color":"#3a4250","border-width":1,'
        '      "border-color":"#5a6270","width":80,"height":30,'
        '      "shape":"round-rectangle"}},'
        '    {selector:"edge",style:{'
        '      "label":"data(label)","color":"#9aa3ad","font-size":"9px",'
        '      "curve-style":"bezier","line-color":"#5a6270",'
        '      "target-arrow-color":"#5a6270",'
        '      "target-arrow-shape":"triangle","width":1.5}}'
        '  ];'
        '  Object.keys(palette).forEach(function(cls){'
        '    stylesheet.push({selector:"node."+cls,'
        '      style:{"background-color":palette[cls]}});'
        '  });'
        '  var nodeIds = els.nodes.map(function(n){return n.data.id;});'
        '  var connected = {};'
        '  els.edges.forEach(function(e){'
        '    connected[e.data.source]=true;'
        '    connected[e.data.target]=true;'
        '  });'
        '  var connectedNodes = nodeIds.filter(function(id){return connected[id];});'
        '  var isolatedNodes = nodeIds.filter(function(id){return !connected[id];});'
        '  cytoscape({'
        '    container: document.getElementById("ara-catalog-graph"),'
        '    elements: els,'
        '    style: stylesheet,'
        '    layout: {'
        '      name:"preset",'
        '      positions: function(node){'
        '        var id = node.id();'
        '        var ci = connectedNodes.indexOf(id);'
        '        var ii = isolatedNodes.indexOf(id);'
        '        if (ci >= 0) {'
        '          return {x: 80 + ci*140, y: 60};'
        '        }'
        '        var cols = 6;'
        '        var col = ii % cols;'
        '        var row = Math.floor(ii / cols);'
        '        return {x: 80 + col*140, y: 180 + row*70};'
        '      },'
        '      fit: true, padding: 18'
        '    },'
        '    wheelSensitivity: 0.2,'
        '  });'
        '})();'
        '</script>'
    )


def _render_weapon_catalog(wdir: Path) -> str:
    cat = wdir / "channel_catalog.json"
    if not cat.is_file():
        return ""
    try:
        data = json.loads(cat.read_text())
    except Exception:
        return ""
    chans = data.get("channels") or []
    edges = data.get("edges") or []
    rows = "".join(
        "<tr>"
        f"<td><code>{e(c.get('name',''))}</code></td>"
        f"<td><code>{e(c.get('sink_subsystem',''))}</code></td>"
        f"<td><code>{e(', '.join(c.get('callers') or []))}</code></td>"
        "</tr>"
        for c in chans[:50]
    )
    return (
        f'<section class="card"><h2>Channel catalog '
        f'<span class="muted">({len(chans)} channels · {len(edges)} edges)</span></h2>'
        + (_render_catalog_graph(chans, edges) if chans else "")
        + '<table class="ara-table"><thead><tr>'
        '<th>channel</th><th>sink</th><th>callers</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></section>'
    )


def _render_weapon_policy(wdir: Path) -> str:
    pdir = wdir / "policy"
    if not pdir.is_dir():
        return ""
    files = [p for p in ("WALLS.md", "bulwark.md", "rhema.md")
             if (pdir / p).is_file()]
    if not files:
        return ""
    parts = ['<section class="card"><h2>Policy stack</h2>']
    parts.append(
        '<p class="muted">Layered ON TOP of operator standing rules '
        'when this weapon is equipped. <code>ara weapon equip</code> '
        'appends these between sentinel markers; <code>unequip</code> '
        'strips between markers.</p>'
    )
    for fname in files:
        text = (pdir / fname).read_text()
        parts.append(
            f'<h3><code>{e(fname)}</code></h3>'
            f'<pre class="ara-pre">{e(text)}</pre>'
        )
    parts.append('</section>')
    return "".join(parts)


def _render_weapon_calibration(wdir: Path) -> str:
    """Render the calibration report card if a report has been written.
    Empty string when no report yet (so weapon detail stays compact)."""
    rpath = wdir / "calibration_report.json"
    if not rpath.is_file():
        return ""
    try:
        data = json.loads(rpath.read_text())
    except Exception:
        return ""
    blocked = bool(data.get("seal_blocked"))
    badge = ('<span class="ara-badge bad">seal blocked</span>'
             if blocked else
             '<span class="ara-badge ok">passed</span>')
    mean_p5 = data.get("mean_precision_at_5") or 0.0
    mean_r10 = data.get("mean_recall_at_10") or 0.0
    parts = [
        '<section class="card"><h2>Calibration ' + badge + '</h2>'
        f'<p>{data.get("queries_passed", 0)} / '
        f'{data.get("queries_total", 0)} queries passed · '
        f'mean precision@5 <code>{mean_p5:.3f}</code> · '
        f'mean recall@10 <code>{mean_r10:.3f}</code></p>'
    ]
    blocking = data.get("blocking_reasons") or []
    if blocking:
        parts.append('<p class="muted">Blocking reasons:</p><ul>')
        for r in blocking:
            parts.append(f'<li>{e(str(r))}</li>')
        parts.append('</ul>')
    cases = data.get("cases") or []
    if cases:
        rows = "".join(
            "<tr>"
            f'<td>{e(str(c.get("query","")))}</td>'
            f'<td><code>{e(", ".join(c.get("expected") or []))}</code></td>'
            f'<td><code>{e(", ".join((c.get("returned") or [])[:5]))}</code></td>'
            f'<td>{float(c.get("precision_at_5", 0.0)):.2f}</td>'
            f'<td>{float(c.get("recall_at_10", 0.0)):.2f}</td>'
            f'<td><span class="{"ara-badge ok" if c.get("passed") else "ara-badge bad"}">'
            f'{"pass" if c.get("passed") else "fail"}</span></td>'
            "</tr>"
            for c in cases
        )
        parts.append(
            '<table class="ara-table"><thead><tr>'
            '<th>query</th><th>expected</th><th>top 5 returned</th>'
            '<th>P@5</th><th>R@10</th><th></th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )
    parts.append(
        '<p class="muted" style="margin-top:8px">Re-run with '
        '<code>ara weapon calibrate ' + e(wdir.name) + '</code></p>'
        '</section>'
    )
    return "".join(parts)


def _render_weapon_seal(wdir: Path) -> str:
    seal = wdir / "seal.json"
    if not seal.is_file():
        return ""
    try:
        data = json.loads(seal.read_text())
    except Exception:
        return ""
    sealed_at = data.get("sealed_at")
    if isinstance(sealed_at, (int, float)):
        sealed_at_text = _fmt_ts(sealed_at)
    else:
        sealed_at_text = str(sealed_at or "")
    rows = (
        f"<tr><td>weapon_hash</td><td><code>{e(str(data.get('weapon_hash','')))}</code></td></tr>"
        f"<tr><td>weapon_target_id</td><td><code>{e(str(data.get('weapon_target_id','')))}</code></td></tr>"
        f"<tr><td>forge_id</td><td><code>{e(str(data.get('forge_id','')))}</code></td></tr>"
        f"<tr><td>operator_id</td><td>{e(str(data.get('operator_id','')))}</td></tr>"
        f"<tr><td>sealed_at</td><td>{e(sealed_at_text)}</td></tr>"
        f"<tr><td>warnings</td><td><code>{e(json.dumps(data.get('warnings') or []))}</code></td></tr>"
    )
    return (
        '<section class="card"><h2>Seal</h2>'
        f'<table class="ara-table"><tbody>{rows}</tbody></table></section>'
    )


# ---------- /forge ----------

def _forge_mat_count(fdir: Path) -> int:
    mats = fdir / "mats"
    if not mats.is_dir():
        return 0
    return sum(
        1 for p in mats.iterdir()
        if p.is_file() and not p.name.endswith(".meta.json")
    )


def _forge_index_counts(fdir: Path) -> dict[str, int]:
    return {
        "mats": _forge_mat_count(fdir),
        "manifest": len(_read_jsonl(fdir / "manifest.jsonl")),
        "extractions": len(_read_jsonl(fdir / "extractions.jsonl")),
        "audit": len(_read_jsonl(fdir / "audit.jsonl")),
    }


def _forge_count_label(n: int, noun: str) -> str:
    suffix = "" if n == 1 else "s"
    if noun == "manifest":
        suffix = ""
    return f"{n} {noun}{suffix}"


def _render_forge_session_card(
    fid: str, sess: dict, counts: dict[str, int], token: str,
) -> str:
    phase = str(sess.get("phase", "") or "unknown")
    target = str(sess.get("weapon_target_id", "") or "unassigned")
    flow = "".join(
        f'<code>{e(step)}</code>'
        for step in _forge_flow_steps(phase, counts)
    )
    card = _render_loadout_card(
        "weapon",
        "Weapon",
        target,
        f"phase {phase}",
        f"/forge/{e(fid)}",
        token,
        glyph="W",
        meta_rows=[
            ("phase", phase),
            ("mats", str(counts["mats"])),
            ("extract", str(counts["extractions"])),
        ],
    )
    return (
        f'<article class="forge-session-card forge-mini-weapon" data-phase="{e(phase)}">'
        f'{card}'
        '<div class="forge-mini-meta">'
        f'<span><b>{e(fid)}</b></span>'
        f'<span>{e(_forge_count_label(counts["mats"], "mat"))} · '
        f'{e(_forge_count_label(counts["manifest"], "manifest"))} · '
        f'{e(_forge_count_label(counts["extractions"], "extraction"))}</span>'
        '</div>'
        f'<div class="forge-flow" data-phase="{e(phase)}">'
        '<span>Phase flow</span>'
        f'{flow}'
        '</div>'
        '</article>'
    )


def _forge_flow_steps(phase: str, counts: dict[str, int]) -> list[str]:
    mats = _forge_count_label(counts["mats"], "mat")
    manifest = _forge_count_label(counts["manifest"], "manifest")
    extracts = _forge_count_label(counts["extractions"], "extraction")
    audit = _forge_count_label(counts["audit"], "audit event")
    if phase == "deposit":
        return [mats, "upload mats", "finalize deposit"]
    if phase == "manifest":
        return [mats, "index manifest", manifest]
    if phase == "exhaust":
        return [manifest, "Hermes exhaust", extracts]
    if phase == "review":
        return [extracts, "operator review", audit]
    if phase == "synthesize":
        return [extracts, "draft alphabets", "seal next"]
    if phase == "sealed":
        return ["sealed weapon", manifest, extracts]
    if phase == "aborted":
        return ["aborted", audit, "restore loadout"]
    return [phase or "unknown", mats, audit]


def render_forge(state_root: Path, token: str) -> str:
    fdir = state_root / "forge"
    # alphabet-pack options: built-in + operator-forged runtime packs
    from ..forge.alphabets import pack as _pack_mod
    try:
        _pack_names = list(_pack_mod.list_packs(state_root))
    except Exception:
        _pack_names = ["offsec"]
    if "offsec" not in _pack_names:
        _pack_names = ["offsec", *_pack_names]
    pack_opts = "".join(
        f'<option value="{e(n)}"{" selected" if n == "offsec" else ""}>{e(n)}</option>'
        for n in _pack_names
    )
    from ara.forge import chunk_workflows as _cw
    workflow_opts = "".join(
        f'<option value="{e(w["name"])}"'
        f'{" selected" if w["name"] == _cw.DEFAULT else ""}>'
        f'{e(w["name"])}{" (forged)" if w.get("forged") else ""} — '
        f'{e((w.get("description") or "")[:56])}</option>'
        for w in _cw.list_workflows(state_root)
    ) or f'<option value="{_cw.DEFAULT}">{_cw.DEFAULT}</option>'
    parts = [
        f'<link rel="stylesheet" href="{_static_css_href("armor-card", token)}">',
        '<section class="forge-workbench">',
        '<div class="forge-hero">',
        '<div class="forge-hero-copy">',
        '<div class="ara-eyebrow">Create</div>',
        '<h2 class="ara-h">Forge</h2>'
        '<p class="ara-sub">Shape weapons from source mats, manifest coverage, '
        'Hermes extraction, and sealed runtime alphabets.</p>'
        '<div class="forge-rune-row">'
        '<span>mats</span><span>manifest</span><span>exhaust</span>'
        '<span>synthesize</span><span>seal</span>'
        '</div>'
        '</div>',
        '<div class="forge-hero-art" aria-hidden="true">'
        '<span class="forge-anvil"></span>'
        '<span class="forge-spark forge-spark-a"></span>'
        '<span class="forge-spark forge-spark-b"></span>'
        '<span class="forge-spark forge-spark-c"></span>'
        '</div>',
        f'<a class="ara-btn ara-btn-primary forge-pack-link" href="/forge/packs?t={e(token)}">'
        'Browse packs</a>',
        '</div>',
        '<details class="card forge-create-card" open>'
        '<summary class="forge-create-summary">'
        '<div class="ara-eyebrow">Create</div>'
        '<div class="forge-create-head">'
        '<div><h3>Start a forge session</h3>'
        '<p class="ara-sub">Create the blacksmith posture, then move through the phase rail from deposit to seal.</p></div>'
        '<span class="forge-phase-pill">new weapon</span>'
        '</div>'
        '</summary>'
        f'<form class="ara-form forge-create-form" method="post" action="/forge/create?t={e(token)}">'
        '<div class="forge-form-grid">'
        '<label class="forge-field">Kind<select name="kind" id="forge-kind">'
        '<option value="weapon">weapon</option>'
        '<option value="pack">pack (forge a new alphabet pack from mats)</option>'
        '</select></label>'
        '<label class="forge-field forge-field-primary">'
        '<span id="forge-primary-label">Weapon target</span>'
        '<input type="text" name="weapon_target_id" required placeholder="hermes-default-v2"></label>'
        '<label class="forge-field">Operator<input type="text" name="operator_id" '
        'placeholder="armory"></label>'
        f'<label class="forge-field forge-weapon-only">Alphabet pack'
        f'<select name="alphabet_pack">{pack_opts}</select></label>'
        f'<label class="forge-field forge-pack-only" style="display:none">'
        f'Investigation workflow'
        f'<select name="chunk_workflow">{workflow_opts}</select></label>'
        '<label class="forge-field forge-weapon-only">Coverage threshold<input type="number" step="0.05" '
        'min="0" max="1" name="coverage_threshold" value="1.0"></label>'
        '<label class="forge-field forge-weapon-only">Predecessor weapon<input type="text" '
        'name="weapon_predecessor_id" placeholder="optional"></label>'
        '<label class="forge-field">Task id<input type="text" name="engagement_id" '
        'placeholder="optional"></label>'
        '</div>'
        # progressive enhancement: relabel the primary field + hide the
        # weapon-only fields when the operator picks the pack kind. The form
        # still submits correctly without JS (weapon is the default).
        '<script>(function(){'
        'var k=document.getElementById("forge-kind");if(!k)return;'
        'var lbl=document.getElementById("forge-primary-label");'
        'var form=k.closest("form");'
        'var primary=form.querySelector("[name=weapon_target_id]");'
        'function sync(){var pack=k.value==="pack";'
        'if(lbl)lbl.textContent=pack?"Pack name":"Weapon target";'
        'if(primary)primary.placeholder=pack?"webapp-offsec-v1":"hermes-default-v2";'
        'form.querySelectorAll(".forge-weapon-only").forEach(function(el){'
        'el.style.display=pack?"none":"";'
        'el.querySelectorAll("input,select").forEach(function(f){f.disabled=pack;});'
        '});'
        'form.querySelectorAll(".forge-pack-only").forEach(function(el){'
        'el.style.display=pack?"":"none";});}'
        'k.addEventListener("change",sync);sync();'
        '})();</script>'
        '<div class="forge-create-actions">'
        '<label class="ara-check"><input type="checkbox" '
        'name="allow_starter_alphabets"> include starter alphabets</label>'
        '<label class="ara-check forge-pack-only"><input type="checkbox" '
        'name="skip_policy"> skip policy generation (hermes-default only)</label>'
        '<button class="ara-btn ara-btn-primary" type="submit">Create forge</button>'
        '</div>'
        '</form>'
        '</details>',
        _render_creation_preview(
            "weapon",
            "Create weapon preview",
            "new-weapon-preview",
            "Forged weapons receive deterministic card art from their weapon id.",
            glyph="W",
            rows=[("seed", "weapon id"), ("state", "draft")],
        ),
    ]
    if not fdir.is_dir():
        parts.append(
            '<div class="ara-empty">No forge sessions yet. '
            'Run <code>ara forge new &lt;weapon_target_id&gt;</code> '
            'to start one.</div>'
        )
        parts.append('</section>')
        return _page("Forge", "forge", "".join(parts), token)
    rows: list[tuple[str, dict, dict[str, int]]] = []
    for child in sorted(fdir.iterdir(), key=lambda p: -p.stat().st_mtime):
        if not child.is_dir():
            continue
        sess_path = child / "session.json"
        if not sess_path.is_file():
            continue
        try:
            sess = json.loads(sess_path.read_text())
        except Exception:
            continue
        rows.append((child.name, sess, _forge_index_counts(child)))
    if not rows:
        parts.append('<div class="ara-empty">No forge sessions yet.</div>')
        parts.append('</section>')
        return _page("Forge", "forge", "".join(parts), token)
    totals = Counter()
    phases = Counter()
    for _fid, sess, counts in rows:
        totals.update(counts)
        phases[str(sess.get("phase", "") or "unknown")] += 1
    phase_text = ", ".join(f"{v} {k}" for k, v in phases.most_common())
    parts.append(
        '<div class="forge-overview">'
        '<div><span>Sessions</span>'
        f'<strong>{len(rows)}</strong><small>{e(phase_text)}</small></div>'
        '<div><span>Mats</span>'
        f'<strong>{totals["mats"]}</strong><small>operator source material</small></div>'
        '<div><span>Extractions</span>'
        f'<strong>{totals["extractions"]}</strong><small>channel outputs</small></div>'
        '</div>'
    )
    parts.append(
        '<section class="forge-history">'
        '<div class="forge-history-head">'
        '<div><div class="ara-eyebrow">Recent outputs</div>'
        '<h3>Forge hand</h3>'
        '<p class="ara-sub">Small weapon cards for active and sealed forge sessions. '
        'Use the Weapons page for normal equip, swap, and inspection.</p></div>'
        '</div>'
        '<div class="forge-session-hand">'
    )
    for fid, sess, counts in rows:
        parts.append(_render_forge_session_card(fid, sess, counts, token))
    parts.append('</div></section>')
    parts.append('</section>')
    return _page("Forge", "forge", "".join(parts), token)


def _render_forge_mats_panel(state_root: Path, forge_id: str, token: str,
                             phase: str) -> str:
    try:
        from ..forge.session import ForgeSession
        from ..forge.phases import deposit as deposit_mod
        sess = ForgeSession.load(state_root, forge_id)
        mats = deposit_mod.list_mats(sess)
    except Exception:
        mats = []
    rows = []
    for rec in mats:
        remove = ""
        if phase == "deposit":
            remove = (
                f'<form method="post" action="/forge/{e(forge_id)}/mats/remove?t={e(token)}" '
                'style="margin:0">'
                f'<input type="hidden" name="mat_filename" value="{e(rec.mat_filename)}">'
                '<button class="ara-btn ara-btn-ghost" type="submit">Remove</button>'
                '</form>'
            )
        rows.append(
            '<tr>'
            f'<td><code>{e(rec.mat_filename)}</code></td>'
            f'<td>{rec.size_bytes}</td>'
            f'<td><code>{e(rec.sha256[:12])}</code></td>'
            f'<td>{remove}</td>'
            '</tr>'
        )
    upload = ""
    finalize = ""
    if phase == "deposit":
        upload = (
            f'<form class="ara-form" method="post" enctype="multipart/form-data" '
            f'action="/forge/{e(forge_id)}/mats/upload?t={e(token)}">'
            '<label>Upload mat<input type="file" name="file" required></label>'
            '<button class="ara-btn ara-btn-primary" type="submit">Upload mat</button>'
            '</form>'
        )
        finalize = (
            f'<form method="post" action="/forge/{e(forge_id)}/deposit/finalize?t={e(token)}">'
            '<button class="ara-btn ara-btn-primary" type="submit">Finalize deposit</button>'
            '</form>'
        )
    table = (
        '<table class="ara-table forge-mat-table"><thead><tr>'
        '<th>file</th><th>bytes</th><th>sha256</th><th></th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        if rows else '<p class="muted">No mats deposited yet.</p>'
    )
    return (
        '<section class="card forge-mats-card"><div class="forge-card-heading">'
        '<div><div class="ara-eyebrow">Deposit</div><h2>Mats</h2></div>'
        f'<span class="forge-phase-pill">{len(mats)} file{"s" if len(mats) != 1 else ""}</span>'
        '</div>'
        + upload + table + finalize +
        '</section>'
    )


def _render_forge_action_panel(forge_id: str, token: str, sess: dict) -> str:
    phase = str(sess.get("phase") or "")
    target = str(sess.get("weapon_target_id") or "")
    controls: list[str] = []
    if phase in {"manifest", "review"}:
        controls.append(
            '<div class="forge-action-card"><span>Manifest</span><h3>Index mats</h3>'
            '<p>Rebuild manifest elements from deposited source material.</p>'
            f'<form method="post" action="/forge/{e(forge_id)}/manifest/run?t={e(token)}">'
            '<button class="ara-btn ara-btn-primary" type="submit">Run manifest</button>'
            '</form></div>'
        )
    if phase == "review":
        controls.append(
            '<div class="forge-action-card"><span>Review</span><h3>Open exhaust</h3>'
            '<p>Approve the manifest and move the session into extraction.</p>'
            f'<form method="post" action="/forge/{e(forge_id)}/manifest/approve?t={e(token)}">'
            '<button class="ara-btn ara-btn-primary" type="submit">Approve manifest</button>'
            '</form></div>'
        )
    kind = str((sess.get("config") or {}).get("kind") or "weapon")
    if phase == "exhaust" and kind == "pack":
        controls.append(
            '<div class="forge-action-card forge-action-card-wide"><span>Propose</span><h3>Propose vocabulary</h3>'
            '<p>The contained <b>Codex+Pen</b> agent reads the mats and proposes '
            'the pack vocabulary (alphabets, entries, bindings) via its channels '
            '— fully sandboxed. Bound the run with batch size and timeout; run '
            'again to cover more.</p>'
            f'<form class="ara-form" method="post" action="/forge/{e(forge_id)}/exhaust/run?t={e(token)}">'
            '<div class="forge-form-grid">'
            '<label>Batch size<input type="number" min="1" name="batch_size" value="8"></label>'
            '<label>Timeout seconds<input type="number" min="5" name="iteration_timeout_s" value="600"></label>'
            '</div>'
            '<button class="ara-btn ara-btn-primary" type="submit">Propose vocabulary</button>'
            '</form></div>'
        )
        controls.append(
            '<div class="forge-action-card"><span>Fallback</span><h3>Stateless review</h3>'
            '<p>Run the cheap heuristic pass into a separate candidate pool, then '
            'the agent reviews it (keep/skip) — run only AFTER the agent\'s own '
            'forge. Kept candidates become real proposals; the rest are dropped.</p>'
            f'<form method="post" action="/forge/{e(forge_id)}/exhaust/fallback?t={e(token)}">'
            '<button class="ara-btn ara-btn-primary" type="submit">Run stateless review</button>'
            '</form></div>'
        )
    elif phase == "exhaust":
        controls.append(
            '<div class="forge-action-card forge-action-card-wide"><span>Hermes</span><h3>Run bounded exhaust</h3>'
            '<p>Call the contained Hermes loop for a bounded batch of pending manifest elements.</p>'
            f'<form class="ara-form" method="post" action="/forge/{e(forge_id)}/exhaust/run?t={e(token)}">'
            '<div class="forge-form-grid">'
            '<label>Target coverage<input type="number" step="0.05" min="0" max="1" '
            'name="target_coverage" value="0.5"></label>'
            '<label>Batch size<input type="number" min="1" name="batch_size" value="5"></label>'
            '<label>Max iterations<input type="number" min="1" name="max_iterations" value="1"></label>'
            '<label>Timeout seconds<input type="number" min="5" name="iteration_timeout_s" value="120"></label>'
            '</div>'
            '<button class="ara-btn ara-btn-primary" type="submit">Run Hermes exhaust</button>'
            '</form></div>'
        )
        controls.append(
            '<div class="forge-action-card"><span>Heuristic</span><h3>Auto-extract</h3>'
            '<p>Deterministically emit pack alphabet bodies from report-like mats.</p>'
            f'<form method="post" action="/forge/{e(forge_id)}/exhaust/auto-extract?t={e(token)}">'
            '<button class="ara-btn ara-btn-primary" type="submit">Auto-extract</button>'
            '</form></div>'
        )
        controls.append(
            '<div class="forge-action-card"><span>Gate</span><h3>Advance review</h3>'
            '<p>Close exhaust once coverage is sufficient, or force the gate.</p>'
            f'<form method="post" action="/forge/{e(forge_id)}/exhaust/advance-review?t={e(token)}">'
            '<label class="ara-check"><input type="checkbox" name="force"> force coverage gate</label>'
            '<button class="ara-btn ara-btn-primary" type="submit">Advance to review</button>'
            '</form></div>'
        )
    if phase in {"exhaust", "review"}:
        controls.append(
            '<div class="forge-action-card"><span>Synthesis</span><h3>Draft weapon</h3>'
            '<p>Compile extraction events into the weapon draft and policy stack.</p>'
            f'<form method="post" action="/forge/{e(forge_id)}/synthesize?t={e(token)}">'
            '<label class="ara-check"><input type="checkbox" name="force"> force coverage gate</label>'
            '<button class="ara-btn ara-btn-primary" type="submit">Synthesize draft</button>'
            '</form></div>'
        )
    if phase == "review":
        controls.append(
            '<div class="forge-action-card forge-action-card-wide"><span>Seal</span><h3>Seal weapon</h3>'
            '<p>Validate the draft and write the final weapon directory.</p>'
            f'<form class="ara-form" method="post" action="/forge/{e(forge_id)}/seal?t={e(token)}">'
            f'<label>Weapon id<input type="text" name="weapon_id" value="{e(target)}"></label>'
            '<button class="ara-btn ara-btn-primary" type="submit">Seal weapon</button>'
            '</form></div>'
        )
    if phase not in {"sealed", "aborted"}:
        controls.append(
            '<div class="forge-action-card forge-action-card-danger"><span>Stop</span><h3>Abort forge</h3>'
            '<p>End this forge session and restore the prior loadout.</p>'
            f'<form class="ara-form" method="post" action="/forge/{e(forge_id)}/abort?t={e(token)}">'
            '<label>Abort reason<input type="text" name="reason" '
            'placeholder="operator aborted via Armory"></label>'
            '<button class="ara-btn ara-btn-ghost" type="submit">Abort forge</button>'
            '</form></div>'
        )
    if not controls:
        controls.append('<p class="muted">No phase actions available.</p>')
    return (
        '<section class="card forge-controls-card"><div class="forge-card-heading">'
        '<div><div class="ara-eyebrow">Phase controls</div><h2>Forge controls</h2></div>'
        f'<span class="forge-phase-pill">{e(phase or "unknown")}</span>'
        '</div>'
        '<div class="forge-action-grid">'
        + "".join(controls) +
        '</div></section>'
    )


def _forge_live_mtime(fdir: Path) -> float | None:
    candidates = [
        fdir / "session.json",
        fdir / "manifest.jsonl",
        fdir / "extractions.jsonl",
        fdir / "audit.jsonl",
        fdir / "weapon_draft.json",
    ]
    mtimes: list[float] = []
    for path in candidates:
        try:
            if path.is_file():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def _forge_hearth_labels(phase: str) -> tuple[str, str, str]:
    labels = {
        "deposit": (
            "Heating ore",
            "Waiting on source mats before the billet can be shaped.",
            "ore loaded",
        ),
        "manifest": (
            "Sorting mats",
            "The forge is indexing deposited material into manifest elements.",
            "manifesting",
        ),
        "review": (
            "Operator review",
            "Review manifest elements and approve before the next forge strike.",
            "review gate",
        ),
        "exhaust": (
            "Hammering exhaust",
            "Hermes extracts weapon material from uncovered manifest elements.",
            "hammering",
        ),
        "synthesize": (
            "Drawing blade",
            "Extracted material is being formed into the weapon draft.",
            "synthesizing",
        ),
        "seal": (
            "Quenching seal",
            "Validators are hardening the final weapon artifact.",
            "sealing",
        ),
        "sealed": (
            "Weapon sealed",
            "The billet is finished and written as a sealed weapon.",
            "complete",
        ),
        "aborted": (
            "Forge cooled",
            "This forge session was aborted and the prior loadout restored.",
            "stopped",
        ),
        "reforge_proposal": (
            "Reworking billet",
            "A reforge proposal is staged for operator approval.",
            "proposal",
        ),
    }
    return labels.get(
        phase,
        ("Forge active", "Session state is being read from the forge files.", phase or "unknown"),
    )


def _render_forge_hearth(fdir: Path, sess: dict) -> str:
    phase = str(sess.get("phase") or "unknown")
    counts = _forge_index_counts(fdir)
    manifest_rows = _read_jsonl(fdir / "manifest.jsonl")
    extraction_rows = _read_jsonl(fdir / "extractions.jsonl")
    live_manifest = [
        row for row in manifest_rows
        if not row.get("dropped_by_operator") and row.get("id")
    ]
    covered_ids = {
        str(row.get("manifest_element_id"))
        for row in extraction_rows
        if row.get("manifest_element_id")
    }
    total = len(live_manifest)
    covered = sum(1 for row in live_manifest if str(row.get("id")) in covered_ids)
    percent = round((covered / total) * 100) if total else 0
    title, detail, state_label = _forge_hearth_labels(phase)
    terminal = phase in {"sealed", "aborted"}
    state_class = "forge-hearth-done" if phase == "sealed" else (
        "forge-hearth-stopped" if phase == "aborted" else "forge-hearth-live"
    )
    last_mtime = _forge_live_mtime(fdir)
    last_label = _fmt_ago(last_mtime) if last_mtime else "not yet"
    pending = max(total - covered, 0)
    refresh = (
        '<script>(function(){var h=document.querySelector("[data-forge-live=\'1\']");'
        'if(h){setTimeout(function(){location.reload();},7000);}}());</script>'
        if not terminal else ""
    )
    live_attr = 'data-forge-live="1"' if not terminal else ""
    return (
        f'<section class="card forge-hearth {state_class}" '
        'aria-label="Forge hearth status" '
        f'data-forge-phase="{e(phase)}" '
        f'{live_attr}>'
        '<div class="forge-hearth-copy">'
        '<div class="ara-eyebrow">Forge hearth</div>'
        f'<h2>{e(title)}</h2>'
        f'<p class="ara-sub">{e(detail)}</p>'
        '<div class="forge-hearth-progress" aria-label="coverage">'
        f'<span style="width:{percent}%"></span>'
        '</div>'
        '<div class="forge-hearth-stats">'
        f'<div><span>coverage</span><strong>{covered} / {total}</strong><small>{percent}%</small></div>'
        f'<div><span>pending</span><strong>{pending}</strong><small>manifest elements</small></div>'
        f'<div><span>mats</span><strong>{counts["mats"]}</strong><small>source files</small></div>'
        f'<div><span>extracts</span><strong>{counts["extractions"]}</strong><small>channel events</small></div>'
        '</div>'
        '<div class="forge-hearth-foot">'
        f'<span class="forge-phase-pill">{e(state_label)}</span>'
        f'<span>last strike: {e(last_label)}</span>'
        '</div>'
        '</div>'
        '<div class="forge-hearth-scene" aria-hidden="true">'
        '<span class="forge-hearth-backglow"></span>'
        '<span class="forge-hearth-hammer"></span>'
        '<span class="forge-hearth-anvil"></span>'
        '<span class="forge-hearth-ore"></span>'
        '<span class="forge-hearth-finished-blade"></span>'
        '<span class="forge-hearth-spark spark-one"></span>'
        '<span class="forge-hearth-spark spark-two"></span>'
        '<span class="forge-hearth-spark spark-three"></span>'
        '</div>'
        f'</section>{refresh}'
    )


_KIND_SHORT: dict[str, str] = {
    "PDF_PAGE": "PDF",
    "MD_SECTION": "MD",
    "CODE_BLOCK": "CODE",
    "REPORT_SECTION": "RPT",
    "IMAGE": "IMG",
    "URL_SNAPSHOT": "URL",
    "GENERIC_TEXT": "TXT",
    "UNKNOWN": "?",
}

_KIND_CSS: dict[str, str] = {
    "PDF_PAGE":      "mk-pdf",
    "MD_SECTION":    "mk-md",
    "CODE_BLOCK":    "mk-code",
    "REPORT_SECTION":"mk-rpt",
    "IMAGE":         "mk-img",
    "URL_SNAPSHOT":  "mk-url",
    "GENERIC_TEXT":  "mk-txt",
}


def _render_manifest_panel(
    fdir: Path,
    forge_id: str,
    token: str,
    phase: str,
    manifest_rows: list[dict],
) -> str:
    if not manifest_rows:
        return ""

    total = len(manifest_rows)
    dropped = sum(1 for m in manifest_rows if m.get("dropped_by_operator"))
    active = total - dropped
    kinds: Counter[str] = Counter((m.get("kind") or "?") for m in manifest_rows)
    can_edit = phase in {"manifest", "review"}

    # ---- CSS (injected once per panel) ----
    css = (
        '<style>'
        '.mf-filter{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:14px}'
        '.mf-tab{font-size:10px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;'
        'padding:4px 12px;border-radius:999px;cursor:pointer;border:1px solid var(--outline-variant);'
        'background:var(--surface-container-high);color:var(--text-dim);transition:all 120ms}'
        '.mf-tab.active{background:color-mix(in srgb,var(--primary) 14%,transparent);'
        'border-color:color-mix(in srgb,var(--primary) 38%,transparent);color:var(--primary)}'
        '.mf-table{width:100%;border-collapse:collapse}'
        '.mf-table th{font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;'
        'color:var(--text-mute);padding:4px 8px 8px;text-align:left;border-bottom:1px solid var(--rule)}'
        '.mf-table td{padding:8px 8px;border-bottom:1px solid color-mix(in srgb,var(--rule) 55%,transparent);'
        'vertical-align:middle;font-size:12px;color:var(--on-surface-variant)}'
        '.mf-table tr:last-child td{border-bottom:none}'
        '.mf-table tr.mf-dropped td{opacity:.45}'
        '.mf-kind{display:inline-block;font-family:var(--font-mono);font-size:9px;font-weight:700;'
        'text-transform:uppercase;letter-spacing:.05em;padding:2px 6px;border-radius:999px;'
        'border:1px solid;white-space:nowrap}'
        '.mk-pdf{color:#d9836b;border-color:rgba(217,131,107,.35);background:rgba(217,131,107,.08)}'
        '.mk-md{color:#7eb37a;border-color:rgba(126,179,122,.35);background:rgba(126,179,122,.08)}'
        '.mk-code{color:#4aa6cc;border-color:rgba(74,166,204,.35);background:rgba(74,166,204,.08)}'
        '.mk-rpt{color:#ffd166;border-color:rgba(255,209,102,.35);background:rgba(255,209,102,.08)}'
        '.mk-img,.mk-url,.mk-txt{color:#8a96a8;border-color:rgba(138,150,168,.35);background:rgba(138,150,168,.08)}'
        '.mf-eid{font-family:var(--font-mono);font-size:10px;color:var(--text-mute);white-space:nowrap}'
        '.mf-preview{max-width:300px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}'
        '.mf-btn{font-family:var(--font-display);font-size:9px;font-weight:800;letter-spacing:.06em;'
        'text-transform:uppercase;padding:4px 9px;border-radius:var(--radius-md);cursor:pointer;border:0}'
        '.mf-btn-drop{background:rgba(217,131,107,.15);color:#d9836b;'
        'box-shadow:inset 0 0 0 1px rgba(217,131,107,.28)}'
        '.mf-btn-undrop{background:rgba(74,166,204,.15);color:#4aa6cc;'
        'box-shadow:inset 0 0 0 1px rgba(74,166,204,.28)}'
        '.mf-count-badge{font-size:10px;color:var(--text-dim);margin-left:3px}'
        '@media(max-width:680px){'
        '.mf-preview{max-width:120px}'
        '.mf-col-mat{display:none}'
        '}'
        '</style>'
    )

    # ---- summary row ----
    summary = (
        f'<p>{total} elements'
        f' · dropped: <strong>{dropped}</strong></p>'
        f'<p class="muted">By kind: {e(_fmt_dist(kinds))}</p>'
    )

    # ---- filter tabs ----
    uid = forge_id[:8]
    tabs = (
        f'<div class="mf-filter" id="mf-tabs-{uid}">'
        f'<button class="mf-tab active" onclick="mfFilter(this,\'all\',\'{uid}\')">'
        f'All<span class="mf-count-badge">({total})</span></button>'
        f'<button class="mf-tab" onclick="mfFilter(this,\'active\',\'{uid}\')">'
        f'Active<span class="mf-count-badge">({active})</span></button>'
        f'<button class="mf-tab" onclick="mfFilter(this,\'dropped\',\'{uid}\')">'
        f'Dropped<span class="mf-count-badge">({dropped})</span></button>'
        f'</div>'
    )

    # ---- element rows ----
    rows_html: list[str] = []
    for m in manifest_rows:
        eid = str(m.get("id") or "")
        kind_raw = str(m.get("kind") or "UNKNOWN").upper()
        kind_label = _KIND_SHORT.get(kind_raw, kind_raw[:4])
        kind_css = _KIND_CSS.get(kind_raw, "mk-txt")
        is_dropped = bool(m.get("dropped_by_operator"))
        preview = str(m.get("extracted_text") or "").replace("\n", " ").strip()
        preview = preview[:120] if preview else "(no text)"
        src = str(m.get("source_mat_id") or "")
        src_short = src.split("/")[-1][:40] if src else "—"
        eid_short = eid[:14] + "…" if len(eid) > 14 else eid

        if can_edit:
            if is_dropped:
                action_url = f'/forge/{e(forge_id)}/manifest/undrop?t={e(token)}'
                btn = (
                    f'<form method="post" action="{action_url}" style="display:inline">'
                    f'<input type="hidden" name="element_id" value="{e(eid)}">'
                    '<button class="mf-btn mf-btn-undrop" type="submit">Undrop</button>'
                    '</form>'
                )
            else:
                action_url = f'/forge/{e(forge_id)}/manifest/drop?t={e(token)}'
                btn = (
                    f'<form method="post" action="{action_url}" style="display:inline">'
                    f'<input type="hidden" name="element_id" value="{e(eid)}">'
                    '<button class="mf-btn mf-btn-drop" type="submit">Drop</button>'
                    '</form>'
                )
        else:
            btn = '<span class="muted" style="font-size:10px">—</span>' if not is_dropped else \
                  '<span class="mf-kind mk-pdf" style="opacity:.5">dropped</span>'

        row_class = 'mf-dropped' if is_dropped else ''
        row_data = f'data-dropped="{"true" if is_dropped else "false"}"'
        rows_html.append(
            f'<tr class="{row_class}" {row_data}>'
            f'<td><span class="mf-kind {kind_css}">{e(kind_label)}</span></td>'
            f'<td class="mf-eid">{e(eid_short)}</td>'
            f'<td class="mf-col-mat muted" style="font-size:11px">{e(src_short)}</td>'
            f'<td class="mf-preview">{e(preview)}</td>'
            f'<td>{btn}</td>'
            f'</tr>'
        )

    col_header = (
        '<thead><tr>'
        '<th>Kind</th><th>ID</th>'
        '<th class="mf-col-mat">Mat</th>'
        '<th>Preview</th>'
        f'<th>{"Action" if can_edit else ""}</th>'
        '</tr></thead>'
    )

    table_body = "<tbody>" + "".join(rows_html) + "</tbody>"

    use_details = total > 30
    table_html = f'<table class="mf-table" id="mf-tbl-{uid}">{col_header}{table_body}</table>'
    if use_details:
        table_html = (
            f'<details><summary style="cursor:pointer;font-size:13px;color:var(--text-dim);margin-bottom:10px">'
            f'Show all {total} elements</summary>{table_html}</details>'
        )

    # ---- filter JS ----
    js = (
        '<script>'
        'function mfFilter(btn,mode,uid){'
        'document.querySelectorAll("#mf-tabs-"+uid+" .mf-tab").forEach(function(t){t.classList.remove("active")});'
        'btn.classList.add("active");'
        'document.querySelectorAll("#mf-tbl-"+uid+" tbody tr").forEach(function(r){'
        'if(mode==="all")r.style.display="";'
        'else if(mode==="active")r.style.display=(r.dataset.dropped==="true")?"none":"";'
        'else r.style.display=(r.dataset.dropped==="false")?"none":"";'
        '});}'
        '</script>'
    )

    return (
        f'<section class="card">{css}'
        '<div class="ara-eyebrow">Manifest</div>'
        '<h2>Source elements</h2>'
        f'{summary}'
        f'{tabs}'
        f'{table_html}'
        f'{js}'
        '</section>'
    )


def render_forge_detail(state_root: Path, forge_id: str, token: str,
                          *, flash: str | None = None) -> str:
    fdir = state_root / "forge" / forge_id
    if not fdir.is_dir() or not (fdir / "session.json").is_file():
        body = (
            '<section class="card"><h2>Forge session not found</h2>'
            f'<p><code>{e(forge_id)}</code> has no '
            f'<code>session.json</code>.</p>'
            f'<p><a href="/forge?t={e(token)}">← back</a></p>'
            '</section>'
        )
        return _page("Forge", "forge", body, token)
    try:
        sess = json.loads((fdir / "session.json").read_text())
    except Exception:
        sess = {}
    parts: list[str] = []
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section class="card forge-detail-hero">'
        '<div>'
        '<div class="ara-eyebrow">Forge session</div>'
        f'<h2><code>{e(forge_id)}</code></h2>'
        f'<p>Weapon target: <code>{e(str(sess.get("weapon_target_id","")))}</code></p>'
        '</div>'
        '<div class="forge-detail-side">'
        f'<span class="forge-phase-pill">{e(str(sess.get("phase","")))}</span>'
        f'<a class="ara-btn ara-btn-ghost" href="/forge?t={e(token)}">Forge sessions</a>'
        f'<a class="ara-btn ara-btn-ghost" href="/temper/create?t={e(token)}&amp;forge={e(forge_id)}">Use in Tempering</a>'
        '</div>'
        '</section>'
    )
    phase = str(sess.get("phase") or "")
    parts.append(_render_forge_hearth(fdir, sess))
    parts.append(_render_forge_action_panel(forge_id, token, sess))
    parts.append(_render_forge_mats_panel(state_root, forge_id, token, phase))
    # Manifest panel
    manifest_path = fdir / "manifest.jsonl"
    manifest_rows = _read_jsonl(manifest_path)
    parts.append(_render_manifest_panel(fdir, forge_id, token, phase, manifest_rows))
    extractions = _read_jsonl(fdir / "extractions.jsonl")
    if extractions:
        chans: Counter[str] = Counter(
            (x.get("channel") or "?") for x in extractions
        )
        auto = sum(
            1 for x in extractions
            if (x.get("citation") or {}).get("auto")
        )
        parts.append(
            '<section class="card"><h2>Extractions</h2>'
            f'<p>{len(extractions)} total · {auto} auto-extracted</p>'
            f'<p class="muted">By channel: {e(_fmt_dist(chans))}</p>'
            '</section>'
        )
    # Reforge proposal — only render when one exists
    parts.append(_render_proposal_card(fdir, forge_id, token, sess))
    # Audit chain length
    audit_rows = _read_jsonl(fdir / "audit.jsonl")
    if audit_rows:
        parts.append(
            '<section class="card"><h2>Audit</h2>'
            f'<p>{len(audit_rows)} events. '
            f'<a href="/forge/{e(forge_id)}/audit?t={e(token)}">'
            'view chain →</a></p></section>'
        )
    if (sess.get("config") or {}).get("kind") == "pack":
        parts.append(render_pack_review(state_root, forge_id, token))
    return _page(f"Forge · {forge_id}", "forge", "".join(parts), token)


def _render_proposal_card(fdir: Path, forge_id: str, token: str,
                            sess: dict) -> str:
    """Empty string when no proposal.json — proposal card otherwise."""
    ppath = fdir / "proposal.json"
    if not ppath.is_file():
        return ""
    try:
        prop = json.loads(ppath.read_text())
    except Exception:
        return ""
    applied = prop.get("applied_at") is not None
    badge = ('<span class="ara-badge ok">approved</span>' if applied
             else '<span class="ara-badge warn">pending</span>')
    parts: list[str] = [
        f'<section class="card"><h2>Reforge proposal {badge}</h2>'
        f'<p><code>{e(prop.get("id",""))}</code> · '
        f'predecessor <code>{e(prop.get("weapon_predecessor_id",""))}</code> · '
        f'triggered_by <code>{e(prop.get("triggered_by",""))}</code></p>'
    ]
    if applied:
        parts.append(
            f'<p class="muted">Approved by '
            f'<code>{e(prop.get("approved_by") or "")}</code> at '
            f'{e(_fmt_ago(prop.get("applied_at")))}.</p>'
        )
    diffs = prop.get("component_diffs") or []
    if not diffs:
        parts.append('<p class="muted">No component-level changes — '
                     'proposal is a pure metadata reforge.</p>')
    else:
        rows = "".join(
            "<tr>"
            f'<td><code>{e(d.get("bucket",""))}</code></td>'
            f'<td>{len(d.get("added") or [])}</td>'
            f'<td>{len(d.get("removed") or [])}</td>'
            f'<td>{len(d.get("modified") or [])}</td>'
            "</tr>"
            for d in diffs
        )
        parts.append(
            '<table class="ara-table"><thead><tr>'
            '<th>bucket</th><th>added</th><th>removed</th><th>modified</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
            f'<p style="margin-top:8px;"><a href="/forge/{e(forge_id)}'
            f'/proposal?t={e(token)}">view full diff →</a></p>'
        )
    if prop.get("rubric_changes"):
        parts.append('<p class="muted">Carries rubric changes.</p>')
    if prop.get("synonym_changes"):
        parts.append('<p class="muted">Carries synonym changes.</p>')
    nq = prop.get("new_calibration_queries") or []
    if nq:
        parts.append(
            f'<p class="muted">Adds {len(nq)} calibration '
            f'{"query" if len(nq) == 1 else "queries"}.</p>'
        )
    # Approve button only when pending and phase is REFORGE_PROPOSAL
    if not applied and sess.get("phase") == "reforge_proposal":
        parts.append(
            f'<form method="post" '
            f'action="/forge/{e(forge_id)}/proposal/approve" '
            'style="margin-top:8px;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<button type="submit" class="ara-btn">approve proposal</button>'
            '</form>'
        )
    parts.append('</section>')
    return "".join(parts)


def _fmt_component_summary(c: dict) -> str:
    """Pick a short label for a component dict — prefer ``*_id`` /
    ``id`` / ``name`` keys; fall back to bucket-agnostic content hash."""
    for k in ("crumb_id", "shape_id", "store_id", "line_id",
              "fact_shape_id", "chunk_id", "id", "name"):
        v = c.get(k)
        if v:
            return str(v)
    return "(unnamed)"


def _render_component_block(c: dict) -> str:
    label = _fmt_component_summary(c)
    pretty = json.dumps(c, sort_keys=True, indent=2, default=str)
    return (
        '<details style="margin:6px 0;">'
        f'<summary><code>{e(label)}</code></summary>'
        '<pre style="background:var(--card,#111);'
        'border:1px solid var(--border,#2a2a2a);'
        'border-radius:6px;padding:10px;overflow:auto;'
        f'font-size:12px;color:#cfcfcf;">{e(pretty)}</pre>'
        '</details>'
    )


def _render_modified_block(m: dict) -> str:
    """``m`` is ``{"id": ..., "before": ..., "after": ...}``."""
    label = m.get("id") or _fmt_component_summary(m.get("after") or {})
    before = json.dumps(m.get("before") or {}, sort_keys=True,
                        indent=2, default=str)
    after = json.dumps(m.get("after") or {}, sort_keys=True,
                        indent=2, default=str)
    pre_style = (
        'background:var(--card,#111);'
        'border:1px solid var(--border,#2a2a2a);'
        'border-radius:6px;padding:10px;overflow:auto;'
        'font-size:12px;color:#cfcfcf;margin:0;'
    )
    return (
        '<details style="margin:6px 0;">'
        f'<summary><code>{e(str(label))}</code></summary>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;'
        'gap:8px;margin-top:6px;">'
        f'<div><p class="muted" style="margin:0 0 4px;">before</p>'
        f'<pre style="{pre_style}">{e(before)}</pre></div>'
        f'<div><p class="muted" style="margin:0 0 4px;">after</p>'
        f'<pre style="{pre_style}">{e(after)}</pre></div>'
        '</div></details>'
    )


def render_proposal_detail(state_root: Path, forge_id: str,
                             token: str) -> str:
    """Full diff drill-down for ``<forge_dir>/proposal.json``."""
    fdir = state_root / "forge" / forge_id
    ppath = fdir / "proposal.json"
    if not ppath.is_file():
        body = (
            '<section class="card"><h2>Proposal not found</h2>'
            f'<p><code>{e(forge_id)}</code> has no '
            f'<code>proposal.json</code>.</p>'
            f'<p><a href="/forge/{e(forge_id)}?t={e(token)}">'
            '← forge session</a></p></section>'
        )
        return _page("Proposal", "forge", body, token)
    try:
        prop = json.loads(ppath.read_text())
    except Exception:
        return _page(
            "Proposal", "forge",
            '<section class="card"><h2>Proposal unreadable</h2>'
            '<p>Failed to parse proposal.json.</p></section>',
            token,
        )

    applied = prop.get("applied_at") is not None
    badge = ('<span class="ara-badge ok">approved</span>' if applied
             else '<span class="ara-badge warn">pending</span>')
    parts: list[str] = [
        '<section class="card">'
        f'<h2>Reforge proposal {badge}</h2>'
        f'<p><a href="/forge/{e(forge_id)}?t={e(token)}">'
        '← forge session</a></p>'
        f'<p><code>{e(prop.get("id",""))}</code> · '
        f'predecessor <code>{e(prop.get("weapon_predecessor_id",""))}</code></p>'
        '</section>'
    ]

    diffs = prop.get("component_diffs") or []
    if not diffs:
        parts.append(
            '<section class="card"><p class="muted">'
            'No component-level changes.</p></section>'
        )
    for d in diffs:
        bucket = d.get("bucket", "")
        added = d.get("added") or []
        removed = d.get("removed") or []
        modified = d.get("modified") or []
        parts.append(
            f'<section class="card"><h3><code>{e(bucket)}</code> '
            f'<span class="muted">'
            f'(+{len(added)} / −{len(removed)} / ~{len(modified)})'
            '</span></h3>'
        )
        if added:
            parts.append('<h4>Added</h4>')
            for c in added:
                parts.append(_render_component_block(c))
        if removed:
            parts.append('<h4>Removed</h4>')
            for c in removed:
                parts.append(_render_component_block(c))
        if modified:
            parts.append('<h4>Modified</h4>')
            for m in modified:
                parts.append(_render_modified_block(m))
        parts.append('</section>')

    rationales = prop.get("rationales") or {}
    if rationales:
        rows = "".join(
            "<tr>"
            f'<td><code>{e(str(k))}</code></td>'
            f'<td>{e(str(v))}</td>'
            "</tr>"
            for k, v in rationales.items()
        )
        parts.append(
            '<section class="card"><h2>Rationales</h2>'
            '<table class="ara-table"><thead><tr>'
            '<th>component</th><th>reason</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></section>'
        )

    nq = prop.get("new_calibration_queries") or []
    if nq:
        rows = "".join(
            "<tr>"
            f'<td>{e(str(q.get("query","")))}</td>'
            f'<td><code>{e(", ".join(q.get("expected_top_ids") or []))}</code></td>'
            f'<td>{q.get("min_precision_at_5", "")}</td>'
            "</tr>"
            for q in nq
        )
        parts.append(
            '<section class="card"><h2>New calibration queries</h2>'
            '<table class="ara-table"><thead><tr>'
            '<th>query</th><th>expected</th><th>min P@5</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></section>'
        )

    return _page(f"Proposal · {forge_id}", "forge",
                  "".join(parts), token)


def handle_proposal_approve(state_root: Path, forge_id: str,
                              ) -> tuple[bool, str]:
    """Approve the reforge proposal and return (ok, flash)."""
    from ..forge.reforge import driver as reforge_driver
    from ..forge.session import ForgeSession
    try:
        sess = ForgeSession.load(state_root, forge_id)
    except Exception as exc:
        return False, f"Could not load session: {exc}"
    try:
        proposal = reforge_driver.approve(sess, approved_by="armory")
    except reforge_driver.ReforgeError as exc:
        return False, f"Approve failed: {exc}"
    return True, f"Approved proposal {proposal.id}; phase → REVIEW."


def render_forge_audit(state_root: Path, forge_id: str, token: str) -> str:
    """Hash-chained audit log viewer for a forge session."""
    fdir = state_root / "forge" / forge_id
    apath = fdir / "audit.jsonl"
    parts: list[str] = []
    parts.append(
        '<section class="card"><h2>Audit chain</h2>'
        f'<p><a href="/forge/{e(forge_id)}?t={e(token)}">'
        '← forge session</a></p>'
    )
    if not apath.is_file():
        parts.append('<p class="muted">No audit.jsonl yet.</p></section>')
        return _page("Audit", "forge", "".join(parts), token)

    from ..audit import verify_chain
    ok, last_line, err = verify_chain(apath)
    if ok:
        badge = '<span class="ara-badge ok">verified</span>'
        verdict = (
            f'<p>{badge} {last_line} event'
            f'{"s" if last_line != 1 else ""} — chain intact.</p>'
        )
    else:
        badge = '<span class="ara-badge bad">broken</span>'
        verdict = (
            f'<p>{badge} chain broke at line {last_line}: '
            f'<code>{e(err or "")}</code>.</p>'
        )
    parts.append(verdict)
    parts.append('</section>')

    rows = _read_jsonl(apath)
    if not rows:
        return _page("Audit", "forge", "".join(parts), token)
    body: list[str] = []
    for i, r in enumerate(rows, start=1):
        kind = r.get("kind", "")
        ts = r.get("ts")
        prev = (r.get("prev_hash") or "")[:16]
        self_h = (r.get("self_hash") or "")[:16]
        rec_body = r.get("body") or {}
        pretty = json.dumps(rec_body, sort_keys=True, indent=2, default=str)
        body.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{e(_fmt_ago(ts))}</td>"
            f"<td><code>{e(kind)}</code></td>"
            f"<td><code>{e(prev)}…</code></td>"
            f"<td><code>{e(self_h)}…</code></td>"
            "<td>"
            '<details><summary class="muted">body</summary>'
            '<pre style="background:var(--card,#111);'
            'border:1px solid var(--border,#2a2a2a);'
            'border-radius:6px;padding:10px;overflow:auto;'
            f'font-size:12px;color:#cfcfcf;margin:6px 0 0;">{e(pretty)}</pre>'
            '</details></td>'
            "</tr>"
        )
    parts.append(
        '<section class="card"><table class="ara-table"><thead><tr>'
        '<th>#</th><th>at</th><th>kind</th><th>prev</th>'
        '<th>self</th><th>body</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table></section>'
    )
    return _page(f"Audit · {forge_id}", "forge", "".join(parts), token)


# ---------- /forge/packs (built-in alphabet packs) ----------

def render_packs(state_root: Path, token: str, flash: str = "") -> str:
    """List alphabet packs — built-in + operator-forged runtime store."""
    from ..forge.alphabets import pack as pack_mod
    builtin = set(pack_mod.list_builtin_packs())
    rd = pack_mod.runtime_packs_dir(state_root)
    runtime = {p.stem for p in rd.glob("*.json")} if rd.is_dir() else set()
    names = sorted(builtin | runtime)
    parts = ['<section class="card"><h2>Alphabet packs</h2>'
             f'<p><a href="/forge?t={e(token)}">← forge</a></p>']
    if flash:
        parts.append(f'<p class="flash">{e(flash)}</p>')
    if not names:
        parts.append('<p class="muted">No packs.</p></section>')
        return _page("Packs", "packs", "".join(parts), token)
    rows: list[str] = []
    for name in names:
        try:
            pk = pack_mod.resolve_pack(name, state_root)
        except Exception:
            continue
        in_rt, in_bi = name in runtime, name in builtin
        origin = "runtime" if in_rt and not in_bi else ("built-in" if in_bi and not in_rt else "runtime+built-in")
        # forged (runtime-only) packs can be promoted into the repo built-ins
        action = (
            f'<form method="post" action="/forge/packs/{e(name)}/promote?t={e(token)}" '
            'style="display:inline">'
            '<button class="ara-btn" type="submit">Promote</button></form>'
        ) if in_rt and not in_bi else ""
        rows.append(
            "<tr>"
            f'<td><a href="/forge/packs/{e(name)}?t={e(token)}">'
            f'<code>{e(name)}</code></a></td>'
            f'<td>{e(pk.version)}</td>'
            f'<td>{e(origin)}</td>'
            f'<td>{len(pk.alphabets)}</td>'
            f'<td>{len(pk.chunk_templates)}</td>'
            f'<td>{action}</td>'
            "</tr>"
        )
    parts.append(
        '<table class="ara-table"><thead><tr>'
        '<th>name</th><th>version</th><th>origin</th><th>alphabets</th>'
        '<th>chunks</th><th></th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></section>'
    )
    return _page("Packs", "packs", "".join(parts), token)


def render_pack_detail(state_root: Path, name: str, token: str) -> str:
    from ..forge.alphabets import pack as pack_mod
    try:
        pk = pack_mod.resolve_pack(name, state_root)  # runtime + built-in
    except FileNotFoundError:
        body = (
            '<section class="card"><h2>Pack not found</h2>'
            f'<p><code>{e(name)}</code> is not a known pack.</p>'
            f'<p><a href="/forge/packs?t={e(token)}">← packs</a></p>'
            '</section>'
        )
        return _page("Pack", "packs", body, token)

    rd = pack_mod.runtime_packs_dir(state_root)
    origin = ("runtime" if (rd / f"{name}.json").exists()
              and name not in set(pack_mod.list_builtin_packs()) else "built-in")
    parts: list[str] = []
    parts.append(
        '<section class="card">'
        f'<h2><code>{e(pk.name)}</code> '
        f'<span class="muted">v{e(pk.version)} · {origin}</span></h2>'
        f'<p><a href="/forge/packs?t={e(token)}">← packs</a></p>'
        f'<p>{e(pk.description or "")}</p>'
        '</section>'
    )

    # Alphabets — name, shape, description, and the actual starter vocabulary
    if pk.alphabets:
        blocks = []
        for a in pk.alphabets:
            chips = "".join(
                f'<span class="ara-chip" title="{e(str(s.get("label","")))}">'
                f'{e(str(s.get("id","")))}</span>'
                for s in a.starter
            ) or '<span class="muted">no starter entries</span>'
            blocks.append(
                '<div class="pack-alpha">'
                f'<h3><code>{e(a.name)}</code> '
                f'<span class="muted">{e(a.shape.value)} · '
                f'{len(a.starter)} entries</span></h3>'
                + (f'<p class="muted">{e(a.description or "")}</p>'
                   if a.description else "")
                + f'<div class="pack-chips">{chips}</div></div>'
            )
        parts.append(
            '<section class="card"><h2>Alphabets '
            f'<span class="muted">({len(pk.alphabets)})</span></h2>'
            + "".join(blocks) + '</section>'
        )

    # Chunk templates — same cytoscape graph used for sealed weapons
    if pk.chunk_templates:
        parts.append(
            '<section class="card"><h2>Chunk templates '
            f'<span class="muted">({len(pk.chunk_templates)})</span></h2>'
            + _render_chunk_graph(list(pk.chunk_templates))
        )
        rows = "".join(
            "<tr>"
            f"<td><code>{e(c.get('id',''))}</code>"
            + (' <span class="ara-badge ok">entry</span>'
               if c.get("is_entry") else "")
            + (' <span class="ara-badge">terminal</span>'
               if c.get("is_terminal") else "")
            + (' <span class="ara-badge warn">finding</span>'
               if c.get("can_emit_finding") else "")
            + "</td>"
            f"<td>{e(c.get('title',''))}</td>"
            f"<td><code>{e(', '.join(c.get('tool_surface') or []))}</code></td>"
            f"<td><code>{e(', '.join((c.get('exits') or {}).keys()))}</code></td>"
            "</tr>"
            for c in pk.chunk_templates
        )
        parts.append(
            '<table class="ara-table"><thead><tr>'
            '<th>id</th><th>title</th><th>tool surface</th><th>exits</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></section>'
        )

    # Manifest detection keywords
    kws = pk.manifest_detection.report_section_keywords
    if kws:
        parts.append(
            '<section class="card"><h2>Manifest detection</h2>'
            '<p class="muted">Report-section keywords '
            'used during deposit:</p>'
            '<p><code>' + e(", ".join(kws)) + '</code></p></section>'
        )

    # Validator bindings
    if pk.validator_bindings:
        rows = "".join(
            "<tr>"
            f'<td><code>{e(b.field)}</code></td>'
            f'<td><code>{e(b.rule)}</code></td>'
            f'<td><code>{e(b.alphabet)}</code></td>'
            "</tr>"
            for b in pk.validator_bindings
        )
        parts.append(
            '<section class="card"><h2>Validator bindings</h2>'
            '<table class="ara-table"><thead><tr>'
            '<th>field</th><th>rule</th><th>alphabet</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></section>'
        )

    # Policy defaults preview (first ~600 chars per file)
    if pk.policy_defaults:
        parts.append('<section class="card"><h2>Policy defaults</h2>')
        for fname, body in pk.policy_defaults.items():
            preview = body if len(body) <= 600 else body[:600] + "\n…"
            parts.append(
                f'<h3><code>{e(fname)}</code></h3>'
                f'<pre style="background:var(--card,#111);'
                f'border:1px solid var(--border,#2a2a2a);'
                f'border-radius:6px;padding:10px;overflow:auto;'
                f'font-size:12px;color:#cfcfcf;">{e(preview)}</pre>'
            )
        parts.append('</section>')

    return _page(f"Pack · {name}", "packs", "".join(parts), token)


# ---------- equip / unequip handlers (POST) ----------

def handle_equip(state_root: Path, weapon_id: str) -> tuple[bool, str]:
    """Returns (ok, flash_message)."""
    if not weapons_mod.weapon_exists(state_root, weapon_id):
        return (False,
                f"Weapon {weapon_id!r} not sealed; nothing to equip.")
    weapons_mod.set_equipped_weapon(state_root, weapon_id)
    summary = weapons_mod.equip_policy(state_root, weapon_id)
    overlaid = ", ".join(summary["overlaid"]) if summary["overlaid"] else "none"
    return (True,
            f"Equipped {weapon_id}; policy overlaid: {overlaid}.")


def handle_unequip(state_root: Path) -> tuple[bool, str]:
    prior = weapons_mod.equipped_weapon_id(state_root)
    if prior is None:
        return (False, "No weapon equipped.")
    summary = weapons_mod.unequip_policy(state_root)
    weapons_mod.set_equipped_weapon(state_root, None)
    stripped = ", ".join(summary["stripped"]) if summary["stripped"] else "none"
    return (True,
            f"Unequipped {prior}; policy stripped: {stripped}.")


# ---------- /engagement/findings ----------

_FINDING_STATUS_CLASS = {
    "draft": "ara-badge",
    "operator_review": "ara-badge",
    "signed_off": "ara-badge ok",
    "rescinded": "ara-badge bad",
    "auto_dismissed": "ara-badge bad",
}


def _findings_for(state_root: Path) -> tuple[Path | None, list[dict]]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return None, []
    return sd, list(_latest_per_id(_read_jsonl(sd / "findings.jsonl")).values())


def render_findings(state_root: Path, token: str, *,
                     status_filter: str | None = None,
                     flash: str | None = None) -> str:
    sd, findings = _findings_for(state_root)
    parts: list[str] = []
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section class="card"><h2>Findings</h2>'
        f'<p><a href="/engagement?t={e(token)}">← task</a></p>'
    )
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Findings", "engagement", "".join(parts), token)
    parts.append(f'<p class="muted">Session: <code>{e(sd.name)}</code></p>')
    # Filter row
    statuses = ["all", "draft", "operator_review", "signed_off",
                "rescinded", "auto_dismissed"]
    parts.append('<div style="margin-bottom:8px;">')
    for s in statuses:
        active = (status_filter == s) or (status_filter is None and s == "all")
        cls = "ara-badge ok" if active else "ara-badge"
        href = (f'/engagement/findings?t={e(token)}'
                + (f'&status={e(s)}' if s != "all" else ''))
        parts.append(
            f'<a href="{href}" class="{cls}" '
            f'style="margin-right:6px;text-decoration:none;">{e(s)}</a>'
        )
    parts.append('</div>')

    if status_filter and status_filter != "all":
        findings = [f for f in findings if f.get("status") == status_filter]
    findings.sort(
        key=lambda f: (-float(f.get("severity_score") or 0.0),
                       -float(f.get("created_at") or 0.0)),
    )
    _NOT_CONFIRMED = {"false_positive", "non_exploitable",
                      "non_reproducible", "unsure"}

    def _disp_badge(f: dict) -> str:
        disp = f.get("disposition")
        if not disp:
            return '<span class="ara-badge">unreviewed</span>'
        cls = "ok" if disp == "confirmed" else "warn"
        return f'<span class="ara-badge {cls}">{e(disp)}</span>'

    def _row(f: dict, *, with_reason: bool = False) -> str:
        cells = (
            f'<td><a href="/engagement/findings/{e(f["id"])}?t={e(token)}">'
            f'<code>{e(f["id"])}</code></a></td>'
            f"<td>{e(f.get('title', '')[:80])}</td>"
            f"<td><code>{e(f.get('category', ''))}</code></td>"
            f'<td><span class="{_FINDING_STATUS_CLASS.get(f.get("status",""), "ara-badge")}">'
            f'{e(f.get("status", ""))}</span></td>'
            f'<td>{_disp_badge(f)}</td>'
            f'<td>{e(f.get("severity_name", ""))} '
            f'<span class="muted">({f.get("severity_score", "?")})</span></td>'
            f'<td>{e(_fmt_ago(f.get("created_at")))}</td>'
        )
        if with_reason:
            cells += (f'<td class="muted">'
                      f'{e(str(f.get("disposition_reason") or "")[:140])}</td>')
        return f"<tr>{cells}</tr>"

    def _table(items: list, *, with_reason: bool = False) -> str:
        head = ('<th>id</th><th>title</th><th>category</th><th>status</th>'
                '<th>disposition</th><th>severity</th><th>filed</th>')
        if with_reason:
            head += '<th>reason</th>'
        rows = "".join(_row(f, with_reason=with_reason) for f in items)
        return ('<table class="ara-table"><thead><tr>'
                f'{head}</tr></thead><tbody>{rows}</tbody></table>')

    confirmed = [f for f in findings
                 if f.get("disposition") not in _NOT_CONFIRMED]
    not_conf = [f for f in findings
                if f.get("disposition") in _NOT_CONFIRMED]

    # Main section: confirmed + not-yet-judged findings.
    parts.append(f'<h3>Findings ({len(confirmed)})</h3>')
    parts.append(_table(confirmed) if confirmed
                 else '<p class="muted">No findings.</p>')
    parts.append('</section>')

    # Separate section: flagged / not confirmed — visible, for operator review.
    parts.append('<section class="card"><h2>Not Confirmed — needs review</h2>')
    parts.append(
        '<p class="muted">Flagged by the finding-check phase as '
        'false-positive / non-exploitable / non-reproducible / unsure. These '
        'are NOT hidden — review each and promote any that are real via its '
        'detail page.</p>'
    )
    parts.append(f'<h3>{len(not_conf)} flagged</h3>')
    parts.append(_table(not_conf, with_reason=True) if not_conf
                 else '<p class="muted">None.</p>')
    parts.append('</section>')
    return _page("Findings", "engagement", "".join(parts), token)


# ---------- Spoils: editable findings-by-severity + leads ----------

_SPOILS_SEV_ORDER = ["critical", "high", "medium", "low", "info"]
_SPOILS_SEVERITY_OPTS = ["info", "low", "medium", "high", "critical"]
_SPOILS_DISPOSITION_OPTS = ["", "confirmed", "false_positive",
                            "non_exploitable", "non_reproducible", "unsure"]
_SPOILS_NOT_CONFIRMED = {"false_positive", "non_exploitable",
                         "non_reproducible", "unsure"}
_SPOILS_LEAD_STATUS_OPTS = ["open", "investigating", "promoted",
                            "confirmed", "dismissed"]


def _spoils_select(name: str, options: list, current, *,
                   blank_label: str | None = None) -> str:
    out = []
    for o in options:
        label = blank_label if (o == "" and blank_label) else (o or "—")
        sel = " selected" if str(current or "") == str(o) else ""
        out.append(f'<option value="{e(o)}"{sel}>{e(label)}</option>')
    return f'<select name="{e(name)}">{"".join(out)}</select>'


def _operator_visible_leads(leads: list) -> list:
    """Drop internal housekeeping leads (system-repair commit-debt placeholders)
    from operator-facing views — they're bookkeeping, not real leads."""
    out = []
    for ld in leads:
        if str(ld.get("origin") or "") == "system_repair":
            continue
        if str(ld.get("title") or "").startswith("system_repair:"):
            continue
        out.append(ld)
    return out


def _spoils_claim(f: dict) -> str:
    v = f.get("verification")
    return str(v.get("claim") or "") if isinstance(v, dict) else ""


def _spoils_impact(f: dict) -> str:
    imp = f.get("impact")
    if isinstance(imp, dict):
        if imp.get("summary"):
            return str(imp["summary"])
        cia = [f"{k}={imp[k]}" for k in
               ("confidentiality", "integrity", "availability") if k in imp]
        return ", ".join(cia)
    return str(imp or "")


def _spoils_remediation(f: dict) -> str:
    fc = f.get("fix_custom")
    if isinstance(fc, dict):
        return str(fc.get("summary") or fc.get("what_changed")
                   or fc.get("why_it_fixes") or "")
    return str(f.get("fix_template_id") or "")


def _spoils_finding_card(f: dict, token: str) -> str:
    fid = f.get("id", "")
    ev = f.get("evidence_refs") or []
    disp = f.get("disposition")
    if disp in _SPOILS_NOT_CONFIRMED:
        badge = f'<span class="ara-badge warn">{e(disp)}</span>'
    elif disp == "confirmed":
        badge = '<span class="ara-badge ok">confirmed</span>'
    else:
        badge = '<span class="ara-badge">unreviewed</span>'
    edit = (
        f'<form class="ara-form" method="post" '
        f'action="/engagement/spoils/{e(fid)}/edit?t={e(token)}">'
        f'<label class="wide">Title<input type="text" name="title" '
        f'value="{e(f.get("title",""))}"></label>'
        f'<label>Severity {_spoils_select("severity", _SPOILS_SEVERITY_OPTS, (f.get("severity_name") or "").lower())}</label>'
        f'<label>Category<input type="text" name="category" value="{e(f.get("category",""))}"></label>'
        f'<label>Disposition {_spoils_select("disposition", _SPOILS_DISPOSITION_OPTS, disp, blank_label="unreviewed")}</label>'
        f'<label class="wide">Reason / why-FP<input type="text" name="disposition_reason" '
        f'value="{e(f.get("disposition_reason") or "")}"></label>'
        f'<label class="wide">Claim / how it was confirmed<textarea name="claim" rows="3">'
        f'{e(_spoils_claim(f))}</textarea></label>'
        f'<label class="wide">Impact<textarea name="impact_note" rows="2">'
        f'{e(_spoils_impact(f))}</textarea></label>'
        f'<label class="wide">Remediation / fix<textarea name="remediation" rows="2">'
        f'{e(_spoils_remediation(f))}</textarea></label>'
        f'<label class="wide">Affected assets (comma-separated)<input type="text" name="affected_assets" '
        f'value="{e(", ".join(f.get("affected_assets") or []))}"></label>'
        f'<label class="wide">Details / notes<textarea name="notes" rows="4">'
        f'{e(f.get("notes") or "")}</textarea></label>'
        f'<button type="submit" class="ara-btn ara-btn-primary">Save</button>'
        f'</form>'
    )
    attach = (
        f'<form class="ara-form spoils-attach" method="post" enctype="multipart/form-data" '
        f'action="/engagement/spoils/{e(fid)}/attach?t={e(token)}">'
        f'<label>Attach file<input type="file" name="file"></label>'
        f'<label>Note<input type="text" name="summary" placeholder="optional"></label>'
        f'<button type="submit" class="ara-btn ara-btn-ghost">Attach</button>'
        f'</form>'
    )
    return (
        f'<div class="spoils-card" id="f-{e(fid)}">'
        f'<div class="spoils-head"><span class="spoils-id">{e(fid)}</span> {badge}'
        f'<span class="muted">filed {e(_fmt_ago(f.get("created_at")))} · '
        f'<a href="/engagement/findings/{e(fid)}?t={e(token)}">detail →</a></span></div>'
        f'{edit}'
        f'<div class="spoils-evidence">Evidence ({len(ev)}): '
        f'{_evidence_links(ev, token)}</div>'
        f'{attach}'
        f'</div>'
    )


def _spoils_lead_card(ld: dict, token: str) -> str:
    lid = ld.get("id", "")
    reasoning = ld.get("reasoning") or ld.get("note") or ld.get("reason") or ""
    edit = (
        f'<form class="ara-form" method="post" '
        f'action="/engagement/spoils/lead/{e(lid)}/edit?t={e(token)}">'
        f'<label class="wide">Title<input type="text" name="title" '
        f'value="{e(ld.get("title",""))}"></label>'
        f'<label>Status {_spoils_select("status", _SPOILS_LEAD_STATUS_OPTS, ld.get("status"))}</label>'
        f'<label class="wide">Reasoning<textarea name="reasoning" rows="3">'
        f'{e(reasoning)}</textarea></label>'
        f'<button type="submit" class="ara-btn ara-btn-primary">Save</button>'
        f'</form>'
    )
    ev = ld.get("evidence_refs") or []
    prio = ld.get("priority_operator_override") or ld.get("priority_computed") or "—"
    sig = ld.get("priority_signals") or {}
    strength = sig.get("evidence_strength") if isinstance(sig, dict) else None
    origin = ld.get("origin") or "—"
    ptest = ld.get("proposed_test")
    meta = (
        f'<div class="spoils-evidence">priority <code>{e(prio)}</code>'
        + (f' · evidence-strength <code>{strength:.2f}</code>'
           if isinstance(strength, (int, float)) else '')
        + f' · origin <code>{e(origin)}</code>'
        + f' · evidence ({len(ev)}): <code>{e(", ".join(ev)) if ev else "—"}</code>'
        + (f'<br>proposed test: {e(str(ptest)[:200])}' if ptest else '')
        + '</div>'
    )
    return (
        f'<div class="spoils-card">'
        f'<div class="spoils-head"><span class="spoils-id">{e(lid)}</span> '
        f'<span class="ara-badge">{e(ld.get("status",""))}</span></div>'
        f'{edit}{meta}</div>'
    )


def _spoils_has_content(d: Path) -> bool:
    for name in ("findings.jsonl", "leads.jsonl"):
        try:
            p = d / name
            if p.exists() and p.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _spoils_session_dir(state_root: Path) -> Path | None:
    """Resolve the session Spoils should show: the active session if it has
    findings/leads, else the most-recent session that does (so a just-finished
    run's spoils are visible even when the 'active engagement' marker has moved
    on). Bounded lookback keeps it off the 3935-session slow path."""
    sd = _resolve_session_dir(state_root)
    # A live run always wins — the operator is watching it, so show it even
    # before it has produced any findings/leads (facts-only, or empty). Only
    # when the resolved session is NOT running do we require content (so an
    # idle, empty session doesn't shadow the last real engagement).
    if sd is not None and (_session_is_running(sd) or _spoils_has_content(sd)):
        return sd
    root = Path(state_root) / "sessions"
    if not root.is_dir():
        return sd
    import os
    entries: list[tuple[float, Path]] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        entries.append((entry.stat().st_mtime, Path(entry.path)))
                except OSError:
                    continue
    except OSError:
        return sd
    entries.sort(key=lambda x: -x[0])
    for _mt, p in entries[:60]:
        if _spoils_has_content(p):
            return p
    return sd


def _spoils_all_sessions(state_root: Path, limit: int = 40) -> list[Path]:
    """Recent session dirs that have findings/leads, newest first."""
    import os
    root = Path(state_root) / "sessions"
    if not root.is_dir():
        return []
    entries: list[tuple[float, Path]] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        entries.append((entry.stat().st_mtime, Path(entry.path)))
                except OSError:
                    continue
    except OSError:
        return []
    entries.sort(key=lambda x: -x[0])
    out: list[Path] = []
    for _mt, p in entries[:400]:
        if _spoils_has_content(p):
            out.append(p)
            if len(out) >= limit:
                break
    return out


def _spoils_loadout_by_session(
    state_root: Path, session_dirs: list[Path],
) -> dict[str, dict]:
    """Map session name -> {task_id, armor, trim, temper, weapon} by matching
    the session's started_at to the nearest task-run created_at (<=120s)."""
    import os
    runs: list[tuple[float, dict]] = []
    troot = Path(state_root) / "task-runs"
    if troot.is_dir():
        try:
            with os.scandir(troot) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    try:
                        m = json.loads(
                            (Path(entry.path) / "session.json").read_text())
                    except Exception:  # noqa: BLE001
                        continue
                    ca = m.get("created_at")
                    if isinstance(ca, (int, float)):
                        runs.append((float(ca), {
                            "task_id": m.get("task_id"),
                            "armor": m.get("armor_id"),
                            "trim": m.get("trim_id"),
                            "temper": m.get("temper_id"),
                            "weapon": m.get("weapon_id"),
                        }))
        except OSError:
            pass
    out: dict[str, dict] = {}
    for sd in session_dirs:
        st = None
        try:
            st = json.loads(
                (sd / "session.json").read_text()).get("started_at")
        except Exception:  # noqa: BLE001
            st = None
        lo: dict = {}
        if isinstance(st, (int, float)) and runs:
            best = min(runs, key=lambda r: abs(r[0] - float(st)))
            if abs(best[0] - float(st)) <= 120:
                lo = best[1]
        out[sd.name] = lo
    return out


def _spoils_loadout_pills(lo: dict) -> str:
    """Tiny letter pills for the equipped loadout; hover shows the full name."""
    if not lo:
        return ""
    specs = [
        ("A", "armor", lo.get("armor"), "armor"),
        ("W", "weapon", lo.get("weapon"), "weapon"),
        ("T", "trim", lo.get("trim"), "trim"),
        ("M", "temper", lo.get("temper"), "temper"),
    ]
    pills = "".join(
        f'<span class="spoils-pill spoils-pill-{cls}" '
        f'title="{e(kind)}: {e(str(val))}">{ltr}</span>'
        for ltr, kind, val, cls in specs if val
    )
    return f'<span class="spoils-pills">{pills}</span>' if pills else ""


def _spoils_disp_cat(f: dict) -> str:
    disp = f.get("disposition") or ""
    if disp in _SPOILS_NOT_CONFIRMED:
        return "notconf"
    if disp == "confirmed":
        return "confirmed"
    return "unreviewed"


def _spoils_fact_card(ft: dict, token: str) -> str:
    ev = ft.get("evidence_refs") or []
    conf = ft.get("confidence")
    conf_s = f" · conf {conf}" if conf not in (None, "") else ""
    return (
        '<div class="spoils-card">'
        f'<div class="spoils-head"><span class="spoils-id">{e(ft.get("id",""))}</span>'
        f'<span class="muted">filed {e(_fmt_ago(ft.get("asserted_at") or ft.get("created_at")))}'
        f'{e(conf_s)}</span></div>'
        f'<div style="padding:4px 0;font-size:13px;color:var(--on-surface,#e6edf3)">'
        f'{e(str(ft.get("note") or ft.get("statement") or ""))}</div>'
        f'<div class="spoils-evidence">Evidence ({len(ev)}): '
        f'{_evidence_links(ev, token)}</div>'
        '</div>')


def _spoils_session_section(sd: Path, lo: dict, token: str) -> tuple[str, dict]:
    """Render one session's findings (grouped by severity) + leads, plus the
    per-session header (clickable id, task, loadout pills). Returns
    (html, counts) where counts aggregates sev/disp/lead tallies."""
    findings = list(_latest_per_id(_read_jsonl(sd / "findings.jsonl")).values())
    leads = _operator_visible_leads(
        list(_latest_per_id(_read_jsonl(sd / "leads.jsonl")).values()))
    facts = list(_latest_per_id(_read_jsonl(sd / "facts.jsonl")).values())
    counts = {"sev": {}, "disp": {}, "leads": len(leads),
              "findings": len(findings), "facts": len(facts)}
    buckets: dict[str, list] = {s: [] for s in _SPOILS_SEV_ORDER}
    for f in findings:
        sn = (f.get("severity_name") or "info").lower()
        if sn not in buckets:
            sn = "info"
        buckets[sn].append(f)
        counts["sev"][sn] = counts["sev"].get(sn, 0) + 1
        dc = _spoils_disp_cat(f)
        counts["disp"][dc] = counts["disp"].get(dc, 0) + 1

    task = lo.get("task_id") or ""
    head = (
        '<div class="spoils-session-head">'
        f'<a class="spoils-session-link" '
        f'href="/courtyard/{e(sd.name)}?t={e(token)}" '
        f'title="open session in Courtyard"><code>{e(sd.name[:16])}</code></a>'
        + (f'<span class="muted"> · task <code>{e(task)}</code></span>'
           if task else "")
        + _spoils_loadout_pills(lo)
        + f'<span class="spoils-session-counts">{len(findings)} finding'
        f'{"s" if len(findings) != 1 else ""} · {len(leads)} '
        f'lead{"s" if len(leads) != 1 else ""}</span>'
        '</div>')

    _grp = ('style="margin-top:12px;border:1px solid var(--outline-variant,#30363d);'
            'border-radius:8px;background:var(--surface-container-lowest,#161b22)"')
    _gs = ('style="cursor:pointer;padding:10px 14px;font-weight:600;font-size:14px;'
           'color:var(--on-surface,#e6edf3)"')
    _ent = ('style="margin:6px 0;border:1px solid var(--outline-variant,#30363d);'
            'border-radius:6px"')
    _es = ('style="cursor:pointer;padding:8px 12px;font-size:13px;'
           'color:var(--on-surface,#e6edf3);word-break:break-word"')
    _pad = 'style="padding:2px 12px 12px"'

    body: list[str] = [f'<section class="card spoils-session">{head}']

    # ── Findings (collapsible group, collapsed by default) ───────────
    body.append(f'<details class="spoils-group" {_grp}><summary {_gs}>Findings '
                f'<span class="spoils-count">{len(findings)}</span></summary>'
                f'<div {_pad}>')
    for sev in _SPOILS_SEV_ORDER:
        items = sorted(buckets[sev],
                       key=lambda x: -float(x.get("created_at") or 0.0))
        if not items:
            continue
        sev_cls = ("bad" if sev in ("critical", "high")
                   else "warn" if sev == "medium" else "ok")
        body.append(
            f'<h3 class="spoils-sev-head"><span class="ara-badge {sev_cls}">'
            f'{sev.upper()}</span> <span class="spoils-count">{len(items)}'
            f'</span></h3>')
        for f in items:
            fid = f.get("id", "")
            body.append(
                f'<details class="spoils-item" data-sev="{e(sev)}" '
                f'data-disp="{e(_spoils_disp_cat(f))}" {_ent}>'
                f'<summary {_es}><code>{e(fid)}</code> '
                f'{e(str(f.get("title") or "")[:90])}</summary>'
                f'<div {_pad}>{_spoils_finding_card(f, token)}'
                f'{_polish_panel("finding", fid, token)}</div></details>')
    if not findings:
        body.append('<p class="muted">No findings.</p>')
    body.append('</div></details>')

    # ── Leads (collapsible) ──────────────────────────────────────────
    body.append(f'<details class="spoils-group" {_grp}><summary {_gs}>Leads '
                f'<span class="spoils-count">{len(leads)}</span></summary>'
                f'<div {_pad}>')
    for ld in leads:
        lid = ld.get("id", "")
        body.append(
            f'<details class="spoils-item" {_ent}><summary {_es}>'
            f'<code>{e(lid)}</code> {e(str(ld.get("title") or "")[:90])} '
            f'<span class="muted">· {e(str(ld.get("status") or ""))}</span></summary>'
            f'<div {_pad}>{_spoils_lead_card(ld, token)}'
            f'{_polish_panel("lead", lid, token)}</div></details>')
    if not leads:
        body.append('<p class="muted">No leads.</p>')
    body.append('</div></details>')

    # ── Facts (collapsible) ──────────────────────────────────────────
    body.append(f'<details class="spoils-group" {_grp}><summary {_gs}>Facts '
                f'<span class="spoils-count">{len(facts)}</span></summary>'
                f'<div {_pad}>')
    for ft in sorted(facts,
                     key=lambda x: -float(x.get("asserted_at")
                                          or x.get("created_at") or 0.0)):
        ftid = ft.get("id", "")
        body.append(
            f'<details class="spoils-item" {_ent}><summary {_es}>'
            f'<code>{e(ftid)}</code> '
            f'{e(str(ft.get("note") or ft.get("statement") or "")[:90])}</summary>'
            f'<div {_pad}>{_spoils_fact_card(ft, token)}'
            f'{_polish_panel("fact", ftid, token)}</div></details>')
    if not facts:
        body.append('<p class="muted">No facts.</p>')
    body.append('</div></details>')

    body.append('</section>')
    return "".join(body), counts


_SPOILS_FILTER_JS = """
<script>
window._sf={sev:'all',disp:'all'};
function spoilsFilter(kind,val,btn){
  window._sf[kind]=val;
  document.querySelectorAll('.spoils-filter[data-kind="'+kind+'"]').forEach(
    function(b){b.classList.toggle('active', b===btn);});
  var sev=window._sf.sev, disp=window._sf.disp;
  document.querySelectorAll('.spoils-item').forEach(function(el){
    var ok=(sev==='all'||el.dataset.sev===sev)&&(disp==='all'||el.dataset.disp===disp);
    el.style.display=ok?'':'none';});
  document.querySelectorAll('.spoils-session').forEach(function(sec){
    var vis=Array.from(sec.querySelectorAll('.spoils-item')).some(
      function(i){return i.style.display!=='none';});
    var showAll=(sev==='all'&&disp==='all');
    sec.style.display=(vis||showAll)?'':'none';});
}
</script>
"""


def render_spoils(state_root: Path, token: str, *,
                  flash: str | None = None, scope: str = "active",
                  session: str | None = None) -> str:
    """Spoils: findings (grouped by severity) and leads, editable, with
    cross-session scope, header counts + filters, and per-session loadout."""
    scope = "all" if scope == "all" else "active"
    sel_session = (session or "").strip() or None
    if sel_session and (state_root / "sessions" / sel_session).is_dir():
        # A specific session was picked from the dropdown.
        session_dirs = [state_root / "sessions" / sel_session]
    elif scope == "all":
        session_dirs = _spoils_all_sessions(state_root, limit=40)
    else:
        sd = _spoils_session_dir(state_root)
        session_dirs = [sd] if sd is not None else []
    loadouts = _spoils_loadout_by_session(state_root, session_dirs)

    parts: list[str] = [_SPOILS_FILTER_JS]
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')

    total = {"sev": {}, "disp": {}, "leads": 0, "findings": 0, "facts": 0}
    sections: list[str] = []
    for sd in session_dirs:
        html, c = _spoils_session_section(sd, loadouts.get(sd.name, {}), token)
        sections.append(html)
        for k in ("sev", "disp"):
            for kk, vv in c[k].items():
                total[k][kk] = total[k].get(kk, 0) + vv
        total["leads"] += c["leads"]
        total["findings"] += c["findings"]
        total["facts"] += c.get("facts", 0)

    # ---- header: scope toggle + counts + filters -----------------------
    def _scope_link(key: str, label: str) -> str:
        active = " active" if scope == key else ""
        return (f'<a class="ara-btn ara-btn-ghost spoils-scope{active}" '
                f'href="/engagement/spoils?t={e(token)}&scope={key}">'
                f'{label}</a>')

    def _fbtn(kind: str, val: str, label: str, cls: str = "") -> str:
        active = " active" if val == "all" else ""
        return (f'<button class="spoils-filter {cls}{active}" data-kind="{kind}"'
                f' onclick="spoilsFilter(\'{kind}\',\'{val}\',this)">{label}'
                '</button>')

    sev_chips = "".join(
        f'<span class="spoils-cnt">{total["sev"].get(s, 0)} {s}</span>'
        for s in _SPOILS_SEV_ORDER if total["sev"].get(s))
    disp_chips = "".join(
        f'<span class="spoils-cnt">{total["disp"].get(k, 0)} {lbl}</span>'
        for k, lbl in (("confirmed", "confirmed"), ("notconf", "not-confirmed"),
                       ("unreviewed", "unreviewed")) if total["disp"].get(k))

    sev_filters = "".join([
        _fbtn("sev", "all", "All")] + [
        _fbtn("sev", s, s.capitalize()) for s in _SPOILS_SEV_ORDER
        if total["sev"].get(s)])
    disp_filters = "".join([
        _fbtn("disp", "all", "All"),
        _fbtn("disp", "confirmed", "Confirmed"),
        _fbtn("disp", "notconf", "Not-confirmed"),
        _fbtn("disp", "unreviewed", "Unreviewed")])

    # Session picker: "Current session" button + a dropdown to pick a specific
    # session (findings/leads/facts count per option for quick scanning).
    _cur_active = " active" if (scope == "active" and not sel_session) else ""
    _current_btn = (
        f'<a class="ara-btn ara-btn-ghost spoils-scope{_cur_active}" '
        f'href="/engagement/spoils?t={e(token)}&scope=active">Current session</a>')

    def _sess_option(_sd: Path) -> str:
        _sid = _sd.name

        def _n(_name: str) -> int:
            _p = _sd / _name
            try:
                return sum(1 for _ in _p.open()) if _p.exists() else 0
            except OSError:
                return 0
        _sel = " selected" if sel_session == _sid else ""
        return (f'<option value="{e(_sid)}"{_sel}>{e(_sid[:12])} · '
                f'{_n("findings.jsonl")}f/{_n("leads.jsonl")}l/'
                f'{_n("facts.jsonl")}ft</option>')

    _sess_dropdown = (
        f'<select onchange="location.href=this.value?'
        f'\'/engagement/spoils?t={e(token)}&session=\'+encodeURIComponent(this.value):'
        f'\'/engagement/spoils?t={e(token)}&scope=active\'" '
        'style="background:var(--surface-container-lowest,#161b22);'
        'color:var(--on-surface,#e6edf3);border:1px solid '
        'var(--outline-variant,#30363d);border-radius:6px;padding:6px 10px;'
        'font-family:inherit;font-size:12px;max-width:30ch;margin-left:8px">'
        '<option value="">— pick a session —</option>'
        + "".join(_sess_option(_s) for _s in _spoils_all_sessions(state_root, limit=40))
        + '</select>')

    scope_note = ("this session" if sel_session
                  else "across the last 40 sessions" if scope == "all"
                  else "the active engagement")
    parts.append(
        '<section class="card"><h2>Spoils</h2>'
        f'<p class="muted">Findings &amp; leads for {scope_note} — edit any '
        'field, add notes, attach files. Loadout pills: hover for the equipped '
        'armor / weapon / trim / temper; the session id opens Courtyard.</p>'
        f'<div class="spoils-scopes">{_current_btn}'
        f'{_scope_link("all", "All recent")}{_sess_dropdown}</div>'
        f'<div class="spoils-summary">{total["findings"]} findings · '
        f'{total["leads"]} leads · {total["facts"]} facts &nbsp; '
        f'{sev_chips} {disp_chips}</div>'
        '<div class="spoils-filters"><span class="muted">severity</span>'
        f'{sev_filters}<span class="muted">disposition</span>{disp_filters}'
        '</div>'
        '</section>')

    if not session_dirs:
        parts.append('<section class="card"><p class="muted">'
                     'No sessions with findings or leads yet.</p></section>')
    else:
        parts.extend(sections)
    return _page("Spoils", "spoils", "".join(parts), token)


def _evidence_binder_for(state_root: Path, eid: str, session_id: str | None):
    """Return (session_dir, EvidenceBinder, record) for an evidence id — the
    active session first, then any session that holds it."""
    from ara.runtime.binder.api import EvidenceBinder
    tried = []
    sd = _resolve_session_dir(state_root, session_id)
    if sd is not None:
        tried.append(sd)
    sessions = state_root / "sessions"
    if sessions.is_dir():
        for c in sessions.iterdir():
            if c.is_dir() and c not in tried:
                tried.append(c)
    for cand in tried:
        if not (cand / "evidence").is_dir():
            continue
        try:
            b = EvidenceBinder(cand)
            rec = b.get_record(eid)
        except Exception:  # noqa: BLE001
            continue
        if rec is not None:
            return cand, b, rec
    return None, None, None


def render_evidence_detail(state_root: Path, eid: str, token: str,
                           *, session_id: str | None = None,
                           flash: str | None = None) -> str:
    sd, binder, rec = _evidence_binder_for(state_root, eid, session_id)
    parts: list[str] = [
        '<section class="card"><h2>Evidence</h2>'
        f'<p><a href="/engagement/spoils?t={e(token)}">← spoils</a></p>'
    ]
    if rec is None:
        parts.append(f'<p class="muted">Evidence <code>{e(eid)}</code> '
                     'not found in any session.</p></section>')
        return _page("Evidence", "engagement", "".join(parts), token)
    meta = getattr(rec, "source_metadata", None) or {}
    rows = [
        ("id", eid), ("source", getattr(rec, "source", "")),
        ("summary", getattr(rec, "summary", "")),
        ("content_type", getattr(rec, "content_type", "") or "(unknown)"),
        ("size_bytes", getattr(rec, "size_bytes", "")),
        ("sha256", getattr(rec, "sha256", "")),
        ("session", sd.name),
    ]
    for k in ("url", "method", "status", "status_code", "host", "path"):
        if meta.get(k) not in (None, ""):
            rows.append((k, meta[k]))
    tbl = "".join(
        f'<tr><td style="padding:4px 10px;color:var(--text-mute,#8b949e)">{e(str(k))}'
        f'</td><td style="padding:4px 10px"><code style="word-break:break-all">'
        f'{e(str(v))}</code></td></tr>' for k, v in rows)
    parts.append(f'<table style="border-collapse:collapse"><tbody>{tbl}</tbody></table>')
    # Raw content — text inline (capped), binary as a download link.
    ct = str(getattr(rec, "content_type", "") or "").lower()
    raw = b""
    try:
        raw = binder.get(eid) or b""
    except Exception:  # noqa: BLE001
        raw = b""
    is_text = ct.startswith("text/") or "json" in ct or "xml" in ct or "html" in ct
    if not is_text and raw:
        try:
            raw[:4096].decode("utf-8")
            is_text = True
        except Exception:  # noqa: BLE001
            is_text = False
    dl = f'/engagement/evidence/{e(eid)}/raw?t={e(token)}'
    parts.append(f'<p style="margin-top:14px"><a class="ara-btn ara-btn-ghost" '
                 f'href="{dl}">download raw</a></p>')
    if is_text:
        cap = 200 * 1024
        text = raw[:cap].decode("utf-8", errors="replace")
        trunc = "\n\n… (truncated — download for full)" if len(raw) > cap else ""
        parts.append(
            '<h3 style="margin-top:12px">Raw content</h3>'
            '<pre style="white-space:pre-wrap;word-break:break-word;max-height:60vh;'
            'overflow:auto;background:var(--surface-container-lowest,#0d1117);border:1px solid '
            'var(--outline-variant,#30363d);border-radius:8px;padding:12px">'
            f'{e(text)}{e(trunc)}</pre>')
    else:
        parts.append('<p class="muted">Binary content — use “download raw”.</p>')
    parts.append('</section>')
    return _page(f"Evidence {eid}", "engagement", "".join(parts), token)


def _field_store_for(state_root: Path, field_id: str, session_id: str | None):
    """Return (session_dir, FieldStore, text) for a content-addressed field id
    (Proposal 2) — the active session first, then any session that holds it."""
    from ara.runtime.findings.field_store import FieldStore
    tried = []
    sd = _resolve_session_dir(state_root, session_id)
    if sd is not None:
        tried.append(sd)
    sessions = state_root / "sessions"
    if sessions.is_dir():
        for c in sessions.iterdir():
            if c.is_dir() and c not in tried:
                tried.append(c)
    for cand in tried:
        if not (cand / "fields" / "index.jsonl").is_file():
            continue
        try:
            fs = FieldStore(cand)
            text = fs.get(field_id)
        except Exception:  # noqa: BLE001
            continue
        if text is not None:
            return cand, fs, text
    return None, None, None


def render_field_detail(state_root: Path, field_id: str, token: str,
                        *, session_id: str | None = None,
                        flash: str | None = None) -> str:
    """Viewer for a single content-addressed finding field object."""
    sd, fs, text = _field_store_for(state_root, field_id, session_id)
    kind = field_id.split(":", 1)[0] if ":" in field_id else "field"
    parts: list[str] = [
        '<section class="card"><h2>Field object</h2>'
        f'<p><a href="/engagement/spoils?t={e(token)}">← spoils</a></p>'
    ]
    if text is None:
        parts.append(f'<p class="muted">Field <code>{e(field_id)}</code> '
                     'not found in any session.</p></section>')
        return _page("Field", "engagement", "".join(parts), token)
    # Which findings currently point at this object (provenance back-links).
    refs_from = []
    try:
        import json as _json
        mp = sd / "fields" / "finding_field_map.jsonl"
        if mp.is_file():
            for line in mp.open():
                try:
                    r = _json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                vals = []
                for v in (r.get("refs") or {}).values():
                    vals.extend(v if isinstance(v, list) else [v])
                if field_id in vals and r.get("finding_id") not in refs_from:
                    refs_from.append(r.get("finding_id"))
    except Exception:  # noqa: BLE001
        pass
    rows = [
        ("id", field_id), ("kind", kind), ("session", sd.name),
        ("length", str(len(text))),
    ]
    tbl = "".join(
        f'<tr><td style="padding:4px 10px;color:var(--text-mute,#8b949e)">{e(str(k))}'
        f'</td><td style="padding:4px 10px"><code style="word-break:break-all">'
        f'{e(str(v))}</code></td></tr>' for k, v in rows)
    parts.append(f'<table style="border-collapse:collapse"><tbody>{tbl}</tbody></table>')
    if refs_from:
        links = " ".join(
            f'<a href="/engagement/findings/{e(str(x))}?t={e(token)}">'
            f'<code>{e(str(x))}</code></a>' for x in refs_from if x)
        parts.append(f'<p style="margin-top:10px">Referenced by: {links}</p>')
    parts.append(
        '<h3 style="margin-top:12px">Content</h3>'
        '<pre style="white-space:pre-wrap;word-break:break-word;max-height:60vh;'
        'overflow:auto;background:var(--surface-container-lowest,#0d1117);border:1px solid '
        'var(--outline-variant,#30363d);border-radius:8px;padding:12px">'
        f'{e(text)}</pre>')
    parts.append('</section>')
    return _page(f"Field {field_id}", "engagement", "".join(parts), token)


def _evidence_links(refs: list, token: str) -> str:
    """Render evidence ids as clickable links to the evidence viewer."""
    if not refs:
        return '<span class="muted">(none)</span>'
    return " ".join(
        f'<a href="/engagement/evidence/{e(str(r))}?t={e(token)}">'
        f'<code>{e(str(r))}</code></a>' for r in refs)


def _promotion_readiness(f: dict, token: str, sess_dir: Path | None = None) -> str:
    """First-class display of the fields a finding must carry to be promoted
    (mirrors the sign-off gate): reason, impact, remediation, affected assets,
    how-confirmed, evidence. Shows the value or flags it missing."""
    impact = f.get("impact") or {}
    fix = f.get("fix_custom") or {}
    ver = f.get("verification") or {}
    assets = f.get("affected_assets") or []
    impact_s = (", ".join(f"{k}={v}" for k, v in impact.items())
                if isinstance(impact, dict) and impact else "")
    fix_s = ""
    if isinstance(fix, dict) and fix:
        fix_s = str(fix.get("what_changed") or fix.get("verification_method") or "yes")
    elif f.get("fix_template_id"):
        fix_s = f"template {f.get('fix_template_id')}"
    ver_s = ""
    if isinstance(ver, dict) and ver:
        ver_s = str(ver.get("method") or ver.get("status") or "yes")
    elif isinstance(fix, dict) and fix.get("verification_method"):
        ver_s = str(fix.get("verification_method"))
    rows = [
        ("Reason / description", str(f.get("notes") or "").strip()
         or str(f.get("title") or "").strip()),
        ("Impact", impact_s),
        ("Remediation / fix", fix_s),
        ("Affected assets", ", ".join(str(a) for a in assets)),
        ("How confirmed", ver_s),
    ]
    cells = ""
    for label, val in rows:
        ok = bool(val)
        mark = ("<span style='color:#3fb950'>✓</span>" if ok
                else "<span style='color:#f85149'>✕ missing</span>")
        cells += (
            f'<tr><td style="padding:5px 10px;white-space:nowrap;color:'
            f'var(--text-mute,#8b949e)">{e(label)}</td>'
            f'<td style="padding:5px 10px">{mark}</td>'
            f'<td style="padding:5px 10px">{e(str(val)[:300])}</td></tr>')
    # evidence row (clickable)
    ev = f.get("evidence_refs") or []
    ev_mark = ("<span style='color:#3fb950'>✓</span>" if ev
               else "<span style='color:#f85149'>✕ missing</span>")
    cells += (
        f'<tr><td style="padding:5px 10px;color:var(--text-mute,#8b949e)">Evidence'
        f'</td><td style="padding:5px 10px">{ev_mark}</td>'
        f'<td style="padding:5px 10px">{_evidence_links(ev, token)}</td></tr>')
    # Proposal 2: content-addressed field-object provenance (if the finding was
    # filed after per-field addressing landed). Each id links to the field viewer.
    prov = ""
    if sess_dir is not None:
        try:
            from ara.runtime.findings.field_store import FieldStore
            refs = FieldStore(sess_dir).refs_for(str(f.get("id") or ""))
        except Exception:  # noqa: BLE001
            refs = {}
        if refs:
            ids = []
            for v in refs.values():
                ids.extend(v if isinstance(v, list) else [v])
            links = " ".join(
                f'<a href="/engagement/field/{e(str(i))}?t={e(token)}">'
                f'<code>{e(str(i))}</code></a>' for i in ids if i)
            prov = (
                '<p class="muted" style="margin:10px 0 0;font-size:12px">'
                f'Field provenance (content-addressed): {links}</p>')
    return (
        '<div class="card" style="margin-top:14px"><h3 style="margin:0 0 4px">'
        'Promotion readiness</h3>'
        '<p class="muted" style="margin:0 0 8px;font-size:12.5px">A finding must '
        'carry all of these to be signed off (mechanically enforced).</p>'
        f'<table style="border-collapse:collapse;width:100%">{cells}</table>'
        f'{prov}</div>')


def _polish_panel(item_type: str, item_id: str, token: str) -> str:
    """Operator-actions panel: focused-task buttons + optional custom instructions.
    All buttons share one form; the clicked button submits its ``mode``."""
    from . import polish as _polish
    btns = [(m, lbl) for (it, m), (lbl, _d) in _polish.MODES.items() if it == item_type]
    if not btns:
        return ""
    btn_html = "".join(
        f'<button type="submit" name="mode" value="{e(m)}" '
        f'class="ara-btn{"" if i == 0 else " ara-btn-ghost"}" '
        f'style="margin:3px 5px 3px 0">{e(lbl)}</button>'
        for i, (m, lbl) in enumerate(btns)
    )
    return (
        '<div class="card" style="margin-top:16px">'
        '<h3 style="margin:0 0 6px">Operator actions</h3>'
        '<p class="muted" style="margin:0 0 10px;font-size:13px">Dispatch a focused '
        '~10&#8209;minute agent task on this item. Add optional instructions, then '
        'press any button. The agent can request more time with a justification.</p>'
        f'<form method="post" action="/engagement/polish?t={e(token)}">'
        f'<input type="hidden" name="t" value="{e(token)}">'
        f'<input type="hidden" name="item_type" value="{e(item_type)}">'
        f'<input type="hidden" name="item_id" value="{e(item_id)}">'
        '<textarea name="instructions" rows="2" '
        'placeholder="Optional custom instructions — e.g. focus on the admin '
        'endpoint, reuse the cookie from f:03c7b2…" '
        'style="width:100%;box-sizing:border-box;margin-bottom:10px;padding:8px 10px;'
        'border-radius:8px;border:1px solid var(--outline-variant,#30363d);'
        'background:var(--surface-container-lowest,#0d1117);color:inherit;font:inherit;'
        'font-family:inherit;resize:vertical"></textarea>'
        f'<div>{btn_html}</div>'
        '</form></div>'
    )


def render_finding_detail(state_root: Path, finding_id: str, token: str,
                            *, flash: str | None = None) -> str:
    sd, findings = _findings_for(state_root)
    parts: list[str] = []
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section class="card"><h2>Finding</h2>'
        f'<p><a href="/engagement/findings?t={e(token)}">← findings</a></p>'
    )
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Finding", "engagement", "".join(parts), token)
    f = next((x for x in findings if x.get("id") == finding_id), None)
    if f is None:
        parts.append('<p class="muted">Finding not found.</p></section>')
        return _page("Finding", "engagement", "".join(parts), token)

    # Header card
    status = f.get("status", "")
    parts.append(
        f'<h3>{e(f.get("title", ""))}</h3>'
        f'<p><code>{e(finding_id)}</code> · '
        f'<span class="{_FINDING_STATUS_CLASS.get(status, "ara-badge")}">'
        f'{e(status)}</span> · '
        f'<code>{e(f.get("category", ""))}</code> · '
        f'severity <code>{e(f.get("severity_name", ""))}</code> '
        f'(score {f.get("severity_score", "?")})</p>'
    )
    if status == "draft":
        parts.append(
            '<form method="post" '
            f'action="/engagement/findings/{e(finding_id)}/sign_off" '
            'style="display:inline-block;margin-right:8px;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<button type="submit" class="ara-btn">sign off</button>'
            '</form>'
            '<form method="post" '
            f'action="/engagement/findings/{e(finding_id)}/queue_for_review" '
            'style="display:inline-block;margin-right:8px;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<button type="submit" class="ara-btn ara-btn-ghost">'
            'queue for review</button>'
            '</form>'
            '<form method="post" '
            f'action="/engagement/findings/{e(finding_id)}/rescind" '
            'style="display:inline-block;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<input type="hidden" name="reason" value="rescinded via armory">'
            '<button type="submit" class="ara-btn ara-btn-ghost">rescind</button>'
            '</form>'
        )
    elif status == "operator_review":
        parts.append(
            '<form method="post" '
            f'action="/engagement/findings/{e(finding_id)}/sign_off" '
            'style="display:inline-block;margin-right:8px;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<button type="submit" class="ara-btn">sign off</button>'
            '</form>'
            '<form method="post" '
            f'action="/engagement/findings/{e(finding_id)}/return_to_draft" '
            'style="display:inline-block;margin-right:8px;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<button type="submit" class="ara-btn ara-btn-ghost">'
            'return to draft</button>'
            '</form>'
            '<form method="post" '
            f'action="/engagement/findings/{e(finding_id)}/rescind" '
            'style="display:inline-block;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<input type="hidden" name="reason" value="rescinded via armory">'
            '<button type="submit" class="ara-btn ara-btn-ghost">rescind</button>'
            '</form>'
        )
    elif status == "signed_off":
        parts.append(
            '<form method="post" '
            f'action="/engagement/findings/{e(finding_id)}/rescind" '
            'style="display:inline-block;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<input type="hidden" name="reason" value="rescinded via armory">'
            '<button type="submit" class="ara-btn ara-btn-ghost">rescind</button>'
            '</form>'
        )
    parts.append('</section>')

    parts.append(_promotion_readiness(f, token, sd))
    parts.append(_polish_panel("finding", finding_id, token))

    # Body card — fields + provenance
    sev_cell = f.get("severity_cell") or []
    parts.append(
        '<section class="card"><h2>Record</h2>'
        '<table class="ara-table"><tbody>'
        f'<tr><td>severity_cell</td><td><code>{e(json.dumps(sev_cell))}</code></td></tr>'
        f'<tr><td>impact</td><td><code>{e(json.dumps(f.get("impact") or {}))}</code></td></tr>'
        f'<tr><td>affected_assets</td><td><code>{e(", ".join(f.get("affected_assets") or []))}</code></td></tr>'
        f'<tr><td>fix_template_id</td><td><code>{e(f.get("fix_template_id") or "")}</code></td></tr>'
        f'<tr><td>evidence_refs</td><td>{_evidence_links(f.get("evidence_refs") or [], token)}</td></tr>'
        f'<tr><td>fact_refs</td><td><code>{e(", ".join(f.get("fact_refs") or []))}</code></td></tr>'
        f'<tr><td>lead_refs</td><td><code>{e(", ".join(f.get("lead_refs") or []))}</code></td></tr>'
        f'<tr><td>provenance_chain</td><td><code>{e(", ".join(f.get("provenance_chain") or []))}</code></td></tr>'
        f'<tr><td>activation_id</td><td><code>{e(f.get("activation_id") or "")}</code></td></tr>'
        f'<tr><td>signed_off_by</td><td>{e(f.get("signed_off_by") or "")}</td></tr>'
        f'<tr><td>content_hash</td><td><code>{e(f.get("content_hash") or "")}</code></td></tr>'
        '</tbody></table></section>'
    )
    fix_custom = f.get("fix_custom")
    if fix_custom:
        parts.append(
            '<section class="card"><h2>Custom fix</h2>'
            '<table class="ara-table"><tbody>'
            f'<tr><td>what_changed</td><td>{e(fix_custom.get("what_changed",""))}</td></tr>'
            f'<tr><td>why_it_fixes</td><td>{e(fix_custom.get("why_it_fixes",""))}</td></tr>'
            f'<tr><td>verification_method</td><td>{e(fix_custom.get("verification_method",""))}</td></tr>'
            '</tbody></table></section>'
        )
    return _page(f"Finding · {finding_id}", "engagement",
                  "".join(parts), token)


# ---------- /engagement/leads ----------

_LEAD_STATUS_CLASS = {
    "open": "ara-badge",
    "investigating": "ara-badge ok",
    "promoted": "ara-badge ok",
    "confirmed": "ara-badge ok",
    "dismissed_operator": "ara-badge",
    "dismissed_auto": "ara-badge bad",
    "superseded": "ara-badge",
    "archived": "ara-badge",
}

_PRIORITY_CLASS = {
    "critical": "ara-badge bad",
    "high": "ara-badge warn",
    "medium": "ara-badge",
    "low": "ara-badge",
}


def render_leads(state_root: Path, token: str, *,
                  status_filter: str | None = None,
                  flash: str | None = None) -> str:
    sd = _resolve_session_dir(state_root)
    parts: list[str] = []
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section class="card"><h2>Leads</h2>'
        f'<p><a href="/engagement?t={e(token)}">← task</a></p>'
    )
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Leads", "engagement", "".join(parts), token)
    leads = _operator_visible_leads(list(_latest_per_id(_read_jsonl(sd / "leads.jsonl")).values()))
    statuses = ["all", "open", "investigating", "promoted", "confirmed",
                "dismissed_operator", "dismissed_auto", "archived"]
    parts.append('<div style="margin-bottom:8px;">')
    for s in statuses:
        active = (status_filter == s) or (status_filter is None and s == "all")
        cls = "ara-badge ok" if active else "ara-badge"
        href = (f'/engagement/leads?t={e(token)}'
                + (f'&status={e(s)}' if s != "all" else ''))
        parts.append(
            f'<a href="{href}" class="{cls}" '
            f'style="margin-right:6px;text-decoration:none;">{e(s)}</a>'
        )
    parts.append('</div>')
    if status_filter and status_filter != "all":
        leads = [ld for ld in leads if ld.get("status") == status_filter]

    # Sort: open/investigating first, then by priority (critical>high>med>low)
    _PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    leads.sort(key=lambda ld: (
        0 if ld.get("status") in ("open", "investigating") else 1,
        _PRIORITY_ORDER.get(
            ld.get("priority_operator_override")
            or ld.get("priority_computed") or "low", 9
        ),
        -float(ld.get("created_at") or 0.0),
    ))
    if not leads:
        parts.append('<p class="muted">No leads match.</p></section>')
        return _page("Leads", "engagement", "".join(parts), token)
    rows: list[str] = []
    for ld in leads:
        status = ld.get("status", "")
        priority = (ld.get("priority_operator_override")
                    or ld.get("priority_computed") or "")
        action_form = ""
        if status in ("open", "investigating"):
            actions: list[str] = []
            for a, label in (("investigate", "investigate"),
                              ("promote", "promote"),
                              ("dismiss", "dismiss")):
                actions.append(
                    f'<form method="post" '
                    f'action="/engagement/leads/{e(ld["id"])}/{a}" '
                    f'style="display:inline;margin:0 2px;">'
                    f'<input type="hidden" name="t" value="{e(token)}">'
                    + ('<input type="hidden" name="reason" value="dismissed via armory">'
                       if a == "dismiss" else "")
                    + f'<button type="submit" class="ara-btn ara-btn-ghost">{label}</button>'
                    '</form>'
                )
            action_form = "".join(actions)
        rows.append(
            "<tr>"
            f'<td><a href="/engagement/leads/{e(ld["id"])}?t={e(token)}">'
            f'<code>{e(ld["id"])}</code></a></td>'
            f'<td>{e((ld.get("title", "") or "")[:90])}</td>'
            f'<td><span class="{_PRIORITY_CLASS.get(priority, "ara-badge")}">'
            f'{e(priority)}</span></td>'
            f'<td><span class="{_LEAD_STATUS_CLASS.get(status, "ara-badge")}">'
            f'{e(status)}</span></td>'
            f'<td>{e(_fmt_ago(ld.get("created_at")))}</td>'
            f'<td>{action_form}</td>'
            "</tr>"
        )
    parts.append(
        '<table class="ara-table"><thead><tr>'
        '<th>id</th><th>title</th><th>priority</th><th>status</th>'
        '<th>filed</th><th></th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></section>'
    )
    return _page("Leads", "engagement", "".join(parts), token)


def render_lead_detail(state_root: Path, lead_id: str, token: str,
                          *, flash: str | None = None) -> str:
    sd = _resolve_session_dir(state_root)
    parts: list[str] = []
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section class="card"><h2>Lead</h2>'
        f'<p><a href="/engagement/leads?t={e(token)}">← leads</a></p>'
    )
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Lead", "engagement", "".join(parts), token)
    leads = _operator_visible_leads(list(_latest_per_id(_read_jsonl(sd / "leads.jsonl")).values()))
    ld = next((x for x in leads if x.get("id") == lead_id), None)
    if ld is None:
        parts.append('<p class="muted">Lead not found.</p></section>')
        return _page("Lead", "engagement", "".join(parts), token)

    status = ld.get("status", "")
    priority = (ld.get("priority_operator_override")
                or ld.get("priority_computed") or "")
    parts.append(
        f'<h3>{e(ld.get("title", ""))}</h3>'
        f'<p><code>{e(lead_id)}</code> · '
        f'<span class="{_LEAD_STATUS_CLASS.get(status, "ara-badge")}">'
        f'{e(status)}</span> · '
        f'<span class="{_PRIORITY_CLASS.get(priority, "ara-badge")}">'
        f'{e(priority)}</span></p>'
    )
    # Action buttons inline at top — same set the queue exposes
    if status in ("open", "investigating"):
        parts.append('<p style="margin-top:6px;">')
        for a, label in (("investigate", "investigate"),
                          ("promote", "promote"),
                          ("dismiss", "dismiss")):
            parts.append(
                f'<form method="post" '
                f'action="/engagement/leads/{e(lead_id)}/{a}" '
                f'style="display:inline;margin-right:6px;">'
                f'<input type="hidden" name="t" value="{e(token)}">'
                + ('<input type="hidden" name="reason" '
                   'value="dismissed via armory">' if a == "dismiss" else "")
                + f'<button type="submit" class="ara-btn ara-btn-ghost">{label}</button>'
                '</form>'
            )
        parts.append('</p>')
    parts.append('</section>')

    parts.append(_polish_panel("lead", lead_id, token))

    # Body — reasoning + identifying refs
    reasoning = ld.get("reasoning") or ""
    parts.append(
        '<section class="card"><h2>Reasoning</h2>'
        f'<pre style="white-space:pre-wrap;">{e(reasoning)}</pre>'
        '</section>'
    )
    proposed = ld.get("proposed_test")
    if proposed:
        parts.append(
            '<section class="card"><h2>Proposed test</h2>'
            f'<pre style="white-space:pre-wrap;">{e(proposed)}</pre>'
            '</section>'
        )

    parts.append(
        '<section class="card"><h2>Record</h2>'
        '<table class="ara-table"><tbody>'
        f'<tr><td>origin</td><td>{e(ld.get("origin", ""))}</td></tr>'
        f'<tr><td>activation_id</td><td><code>{e(ld.get("activation_id", ""))}</code></td></tr>'
        f'<tr><td>derived_from_crumb</td><td><code>{e(ld.get("derived_from_crumb") or "")}</code></td></tr>'
        f'<tr><td>evidence_refs</td><td>{_evidence_links(ld.get("evidence_refs") or [], token)}</td></tr>'
        f'<tr><td>fact_refs</td><td><code>{e(", ".join(ld.get("fact_refs") or []))}</code></td></tr>'
        f'<tr><td>affected_assets</td><td><code>{e(", ".join(ld.get("affected_assets") or []))}</code></td></tr>'
        f'<tr><td>priority_computed</td><td>{e(ld.get("priority_computed", ""))}</td></tr>'
        f'<tr><td>priority_agent_proposed</td><td>{e(ld.get("priority_agent_proposed") or "")}</td></tr>'
        f'<tr><td>priority_operator_override</td><td>{e(ld.get("priority_operator_override") or "")}</td></tr>'
        f'<tr><td>stability_s</td><td>{ld.get("stability_s", "?")}</td></tr>'
        f'<tr><td>created_at</td><td>{e(_fmt_ago(ld.get("created_at")))}</td></tr>'
        f'<tr><td>last_reviewed_at</td><td>{e(_fmt_ago(ld.get("last_reviewed_at")))}</td></tr>'
        f'<tr><td>closed_at</td><td>{e(_fmt_ago(ld.get("closed_at")))}</td></tr>'
        f'<tr><td>closed_by</td><td>{e(ld.get("closed_by") or "")}</td></tr>'
        f'<tr><td>death_signal</td><td>{e(ld.get("death_signal") or "")}</td></tr>'
        '</tbody></table></section>'
    )

    history = ld.get("review_history") or []
    if history:
        rows = "".join(
            "<tr>"
            f'<td>{e(_fmt_ago(h.get("at")))}</td>'
            f'<td>{e(h.get("kind") or "")}</td>'
            f'<td>{e(h.get("by") or "")}</td>'
            f'<td>{e(h.get("note") or "")}</td>'
            "</tr>"
            for h in history
        )
        parts.append(
            '<section class="card"><h2>Review history</h2>'
            '<table class="ara-table"><thead><tr>'
            '<th>at</th><th>kind</th><th>by</th><th>note</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></section>'
        )

    enrichments = ld.get("enrichments") or []
    if enrichments:
        rows = "".join(
            "<tr>"
            f'<td>{e(en.get("field") or "")}</td>'
            f'<td>{e(en.get("status") or "")}</td>'
            f'<td>{e(en.get("actor") or "")}</td>'
            f'<td><code>{e(json.dumps(en.get("proposed_value")))}</code></td>'
            "</tr>"
            for en in enrichments
        )
        parts.append(
            '<section class="card"><h2>Enrichments</h2>'
            '<table class="ara-table"><thead><tr>'
            '<th>field</th><th>status</th><th>actor</th><th>value</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></section>'
        )

    return _page(f"Lead · {lead_id}", "engagement",
                  "".join(parts), token)


# ---------- /engagement/anomalies ----------

_ANOMALY_STATUS_CLASS = {
    "pending": "ara-badge warn",
    "promoted_to_lead": "ara-badge ok",
    "dismissed_operator": "ara-badge",
    "auto_dismissed_dup": "ara-badge",
}


def render_anomalies(state_root: Path, token: str, *,
                       flash: str | None = None) -> str:
    sd = _resolve_session_dir(state_root)
    parts: list[str] = []
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section class="card"><h2>Anomalies</h2>'
        f'<p><a href="/engagement?t={e(token)}">← task</a></p>'
    )
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Anomalies", "engagement", "".join(parts), token)
    anomalies = list(_latest_per_id(_read_jsonl(sd / "anomalies.jsonl")).values())
    pending = [a for a in anomalies if a.get("status") == "pending"]
    others = [a for a in anomalies if a.get("status") != "pending"]
    parts.append(
        f'<p class="muted">Session: <code>{e(sd.name)}</code> · '
        f'{len(pending)} pending of {len(anomalies)} total</p>'
    )
    if not anomalies:
        parts.append('<p class="muted">No anomalies recorded.</p></section>')
        return _page("Anomalies", "engagement", "".join(parts), token)
    if pending:
        parts.append('<h3>Pending</h3>')
        parts.append(_render_anomaly_table(pending, token, with_actions=True))
    if others:
        parts.append('<h3>History</h3>')
        parts.append(_render_anomaly_table(others, token, with_actions=False))
    parts.append('</section>')
    return _page("Anomalies", "engagement", "".join(parts), token)


def _render_anomaly_table(rows: list[dict], token: str,
                            *, with_actions: bool) -> str:
    body: list[str] = []
    for a in rows:
        dv = a.get("divergence") or {}
        score = dv.get("raw_score")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "—"
        threshold = a.get("threshold_at_emission")
        action_form = ""
        if with_actions:
            promote = (
                f'<form method="post" '
                f'action="/engagement/anomalies/{e(a["id"])}/promote" '
                f'style="display:inline;margin:0 2px;">'
                f'<input type="hidden" name="t" value="{e(token)}">'
                f'<input type="hidden" name="title" '
                f'value="Anomaly {e(a["id"])} from crumb {e(a.get("crumb_id",""))}">'
                f'<button type="submit" class="ara-btn">promote → lead</button>'
                '</form>'
            )
            dismiss = (
                f'<form method="post" '
                f'action="/engagement/anomalies/{e(a["id"])}/dismiss" '
                f'style="display:inline;margin:0 2px;">'
                f'<input type="hidden" name="t" value="{e(token)}">'
                f'<input type="hidden" name="reason" value="dismissed via armory">'
                f'<button type="submit" class="ara-btn ara-btn-ghost">dismiss</button>'
                '</form>'
            )
            action_form = promote + dismiss
        body.append(
            "<tr>"
            f'<td><a href="/engagement/anomalies/{e(a["id"])}?t={e(token)}">'
            f'<code>{e(a["id"])}</code></a></td>'
            f'<td><code>{e(a.get("crumb_id",""))}</code></td>'
            f'<td><code>{e(a.get("evidence_id",""))}</code></td>'
            f'<td>{e(score_str)} / {e(str(threshold) if threshold is not None else "—")}</td>'
            f'<td><span class="{_ANOMALY_STATUS_CLASS.get(a.get("status",""), "ara-badge")}">'
            f'{e(a.get("status",""))}</span></td>'
            f'<td>{e(_fmt_ago(a.get("detected_at")))}</td>'
            f'<td>{action_form}</td>'
            "</tr>"
        )
    return (
        '<table class="ara-table"><thead><tr>'
        '<th>id</th><th>crumb</th><th>evidence</th>'
        '<th>score / threshold</th><th>status</th>'
        '<th>detected</th><th></th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>'
    )


def render_evidence(state_root: Path, token: str, *,
                       source_filter: str | None = None,
                       activation_filter: str | None = None,
                       limit: int = 200) -> str:
    """Browse the evidence binder's sqlite index for the active session."""
    sd = _resolve_session_dir(state_root)
    parts: list[str] = []
    parts.append(
        '<section class="card"><h2>Evidence binder</h2>'
        f'<p><a href="/engagement?t={e(token)}">← task</a></p>'
    )
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Evidence", "engagement", "".join(parts), token)
    db_path = sd / "evidence" / "index.db"
    if not db_path.is_file():
        parts.append('<p class="muted">No evidence captured yet.</p></section>')
        return _page("Evidence", "engagement", "".join(parts), token)

    where: list[str] = []
    params: list = []
    if source_filter:
        where.append("source = ?")
        params.append(source_filter)
    if activation_filter:
        where.append("captured_during_activation_id = ?")
        params.append(activation_filter)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM evidence{where_sql}", params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT id, sha256, captured_at, captured_during_activation_id, "
            f"source, summary, size_bytes, content_type, masked "
            f"FROM evidence{where_sql} "
            f"ORDER BY captured_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        # Top-N source histogram for sidebar context
        sources = conn.execute(
            "SELECT source, COUNT(*) AS c FROM evidence "
            "GROUP BY source ORDER BY c DESC LIMIT 8"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        parts.append(
            f'<p class="muted">Could not read evidence index: '
            f'<code>{e(str(exc))}</code></p></section>'
        )
        return _page("Evidence", "engagement", "".join(parts), token)
    finally:
        conn.close()

    parts.append(
        f'<p class="muted">Session <code>{e(sd.name)}</code> · '
        f'{total} evidence record'
        f'{"s" if total != 1 else ""} '
        + (f'(showing {len(rows)})' if total > len(rows) else "")
        + '</p>'
    )
    if source_filter or activation_filter:
        parts.append(
            f'<p><a href="/engagement/evidence?t={e(token)}">'
            'clear filters</a></p>'
        )

    if sources:
        chips = "".join(
            f'<a href="/engagement/evidence?t={e(token)}'
            f'&source={e(s["source"])}" class="ara-badge" '
            'style="text-decoration:none;margin-right:6px;">'
            f'{e(s["source"])} <span class="muted">({s["c"]})</span></a>'
            for s in sources
        )
        parts.append(
            '<p class="muted" style="margin-top:0">Top sources: '
            + chips + '</p>'
        )
    parts.append('</section>')

    if not rows:
        parts.append(
            '<section class="card"><p class="muted">'
            'No evidence matches.</p></section>'
        )
        return _page("Evidence", "engagement", "".join(parts), token)

    body = "".join(
        "<tr>"
        f'<td><code>{e(r["id"])}</code></td>'
        f'<td>{e(_fmt_ago(r["captured_at"]))}</td>'
        f'<td><code>{e(r["source"] or "")}</code></td>'
        + (f'<td><a href="/engagement/evidence?t={e(token)}'
           f'&activation={e(r["captured_during_activation_id"] or "")}">'
           f'<code>{e(r["captured_during_activation_id"] or "")}</code></a></td>'
           if r["captured_during_activation_id"] else
           '<td><code></code></td>')
        + f'<td>{e((r["summary"] or "")[:100])}</td>'
        f'<td class="muted">{r["size_bytes"]} B'
        + (' · masked' if r["masked"] else "")
        + '</td>'
        f'<td><code>{e((r["sha256"] or "")[:12])}…</code></td>'
        "</tr>"
        for r in rows
    )
    parts.append(
        '<section class="card"><table class="ara-table"><thead><tr>'
        '<th>id</th><th>captured</th><th>source</th><th>activation</th>'
        '<th>summary</th><th>size</th><th>sha256</th>'
        f'</tr></thead><tbody>{body}</tbody></table></section>'
    )
    return _page("Evidence", "engagement", "".join(parts), token)


def render_anomaly_detail(state_root: Path, anomaly_id: str, token: str,
                              *, flash: str | None = None) -> str:
    sd = _resolve_session_dir(state_root)
    parts: list[str] = []
    if flash:
        parts.append(f'<div class="ara-flash">{e(flash)}</div>')
    parts.append(
        '<section class="card"><h2>Anomaly</h2>'
        f'<p><a href="/engagement/anomalies?t={e(token)}">← anomalies</a></p>'
    )
    if sd is None:
        parts.append('<p class="muted">No active session.</p></section>')
        return _page("Anomaly", "engagement", "".join(parts), token)
    rows = list(_latest_per_id(_read_jsonl(sd / "anomalies.jsonl")).values())
    a = next((x for x in rows if x.get("id") == anomaly_id), None)
    if a is None:
        parts.append('<p class="muted">Anomaly not found.</p></section>')
        return _page("Anomaly", "engagement", "".join(parts), token)

    status = a.get("status", "")
    dv = a.get("divergence") or {}
    score = dv.get("raw_score")
    score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
    threshold = a.get("threshold_at_emission")
    parts.append(
        f'<p><code>{e(anomaly_id)}</code> · '
        f'<span class="{_ANOMALY_STATUS_CLASS.get(status, "ara-badge")}">'
        f'{e(status)}</span> · '
        f'crumb <code>{e(a.get("crumb_id", ""))}</code> · '
        f'evidence <code>{e(a.get("evidence_id", ""))}</code></p>'
        f'<p class="muted">Score <code>{e(score_str)}</code> · '
        f'threshold <code>{e(str(threshold)) if threshold is not None else "—"}</code> · '
        f'detected {e(_fmt_ago(a.get("detected_at")))}</p>'
    )
    if status == "pending":
        parts.append(
            f'<form method="post" '
            f'action="/engagement/anomalies/{e(anomaly_id)}/promote" '
            'style="display:inline;margin-right:6px;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            f'<input type="hidden" name="title" '
            f'value="Anomaly {e(anomaly_id)} from crumb {e(a.get("crumb_id",""))}">'
            '<button type="submit" class="ara-btn">promote → lead</button>'
            '</form>'
            f'<form method="post" '
            f'action="/engagement/anomalies/{e(anomaly_id)}/dismiss" '
            'style="display:inline;">'
            f'<input type="hidden" name="t" value="{e(token)}">'
            '<input type="hidden" name="reason" value="dismissed via armory">'
            '<button type="submit" class="ara-btn ara-btn-ghost">dismiss</button>'
            '</form>'
        )
    parts.append('</section>')

    # Divergence breakdown — list each non-empty bucket
    div_buckets: list[tuple[str, object]] = []
    for k in ("missing_required", "unexpected_fields",
                "structural_violations", "line_matcher_failures",
                "section_matcher_failures"):
        v = dv.get(k) or []
        if v:
            div_buckets.append((k, v))
    pmm = dv.get("pattern_mismatches") or {}
    if pmm:
        div_buckets.append(("pattern_mismatches", pmm))
    if div_buckets:
        parts.append('<section class="card"><h2>Divergence</h2>')
        for label, val in div_buckets:
            pretty = json.dumps(val, sort_keys=True, indent=2, default=str)
            parts.append(
                f'<h3><code>{e(label)}</code></h3>'
                '<pre style="background:var(--card,#111);'
                'border:1px solid var(--border,#2a2a2a);'
                'border-radius:6px;padding:10px;overflow:auto;'
                f'font-size:12px;color:#cfcfcf;">{e(pretty)}</pre>'
            )
        parts.append('</section>')

    parts.append(
        '<section class="card"><h2>Record</h2>'
        '<table class="ara-table"><tbody>'
        f'<tr><td>activation_id</td><td><code>{e(a.get("activation_id", ""))}</code></td></tr>'
        f'<tr><td>expected_shape_hash</td><td><code>{e(a.get("expected_shape_hash", ""))}</code></td></tr>'
        f'<tr><td>promoted_lead_id</td><td><code>{e(a.get("promoted_lead_id") or "")}</code></td></tr>'
        f'<tr><td>dismissed_at</td><td>{e(_fmt_ago(a.get("dismissed_at")))}</td></tr>'
        f'<tr><td>dismissed_by</td><td>{e(a.get("dismissed_by") or "")}</td></tr>'
        f'<tr><td>dismiss_reason</td><td>{e(a.get("dismiss_reason") or "")}</td></tr>'
        '</tbody></table></section>'
    )
    return _page(f"Anomaly · {anomaly_id}", "engagement",
                  "".join(parts), token)


# ---------- mutation handlers (POST) ----------

def _operator_ctx_for(state_root: Path, session_dir: Path, *,
                        weapon_dir: Path | None):
    """Build the channel registry + ChannelContext used by armory POSTs."""
    from ..runtime.bootstrap import install_engagement_channels
    from ..runtime.channels.base import ChannelContext
    sessions_root = state_root / "sessions"
    install_engagement_channels(
        sessions_root=sessions_root,
        weapon_dir=weapon_dir,
    )
    activation_id = None
    a_path = session_dir / "activation.txt"
    if a_path.is_file():
        activation_id = a_path.read_text().strip() or None
    return ChannelContext(
        engagement_id=session_dir.name,
        activation_id=activation_id or "_armory_op",
        caller="operator",
        audit_sink=session_dir / "events.jsonl",
    )


def _weapon_dir_for(state_root: Path) -> Path | None:
    wid = weapons_mod.equipped_weapon_id(state_root)
    if not wid:
        return None
    return weapons_mod.weapon_dir(state_root, wid)


def handle_finding_sign_off(state_root: Path, finding_id: str,
                              operator_id: str | None) -> tuple[bool, str]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session."
    from ..runtime.channels import registry
    ctx = _operator_ctx_for(state_root, sd, weapon_dir=_weapon_dir_for(state_root))
    try:
        ch = registry.get("finding.sign_off")
        ch.call({"finding_id": finding_id,
                 "operator_id": operator_id or "armory"}, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"Sign-off failed: {exc}"
    return True, f"Signed off finding {finding_id}."


def handle_finding_rescind(state_root: Path, finding_id: str,
                             reason: str) -> tuple[bool, str]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session."
    from ..runtime.channels import registry
    ctx = _operator_ctx_for(state_root, sd, weapon_dir=_weapon_dir_for(state_root))
    try:
        ch = registry.get("finding.rescind")
        ch.call({"finding_id": finding_id,
                 "reason": reason or "rescinded"}, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"Rescind failed: {exc}"
    return True, f"Rescinded finding {finding_id}."


def handle_finding_queue_for_review(state_root: Path, finding_id: str,
                                       *, note: str | None = None,
                                       ) -> tuple[bool, str]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session."
    from ..runtime.channels import registry
    ctx = _operator_ctx_for(state_root, sd, weapon_dir=_weapon_dir_for(state_root))
    try:
        ch = registry.get("finding.queue_for_review")
        body: dict = {"finding_id": finding_id}
        if note:
            body["note"] = note
        ch.call(body, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue-for-review failed: {exc}"
    return True, f"Queued finding {finding_id} for review."


def handle_finding_return_to_draft(state_root: Path, finding_id: str,
                                       *, note: str | None = None,
                                       ) -> tuple[bool, str]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session."
    from ..runtime.channels import registry
    ctx = _operator_ctx_for(state_root, sd, weapon_dir=_weapon_dir_for(state_root))
    try:
        ch = registry.get("finding.return_to_draft")
        body: dict = {"finding_id": finding_id}
        if note:
            body["note"] = note
        ch.call(body, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"Return-to-draft failed: {exc}"
    return True, f"Returned finding {finding_id} to draft."


def handle_lead_action(state_root: Path, lead_id: str, action: str,
                         *, reason: str | None = None) -> tuple[bool, str]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session."
    name_map = {
        "investigate": "lead.investigate",
        "touch": "lead.touch",
        "promote": "lead.promote",
        "dismiss": "lead.dismiss",
    }
    channel_name = name_map.get(action)
    if channel_name is None:
        return False, f"Unknown lead action: {action}."
    from ..runtime.channels import registry
    ctx = _operator_ctx_for(state_root, sd, weapon_dir=_weapon_dir_for(state_root))
    body: dict = {"lead_id": lead_id}
    if action == "dismiss":
        body["reason"] = reason or "dismissed"
    try:
        ch = registry.get(channel_name)
        ch.call(body, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"{action} failed: {exc}"
    return True, f"{action.capitalize()} → lead {lead_id}."


def handle_anomaly_promote(state_root: Path, anomaly_id: str,
                              title: str | None) -> tuple[bool, str]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session."
    from ..runtime.channels import registry
    ctx = _operator_ctx_for(state_root, sd, weapon_dir=_weapon_dir_for(state_root))
    try:
        ch = registry.get("anomaly.promote")
        ch.call({"anomaly_id": anomaly_id,
                 "title": title or f"Anomaly {anomaly_id}"}, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"Promote failed: {exc}"
    return True, f"Promoted anomaly {anomaly_id} → lead."


def handle_anomaly_dismiss(state_root: Path, anomaly_id: str,
                              reason: str | None) -> tuple[bool, str]:
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session."
    from ..runtime.channels import registry
    ctx = _operator_ctx_for(state_root, sd, weapon_dir=_weapon_dir_for(state_root))
    try:
        ch = registry.get("anomaly.dismiss")
        ch.call({"anomaly_id": anomaly_id,
                 "reason": reason or "dismissed via armory"}, ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"Dismiss failed: {exc}"
    return True, f"Dismissed anomaly {anomaly_id}."
