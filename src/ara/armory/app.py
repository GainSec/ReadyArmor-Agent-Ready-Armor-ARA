"""Armory HTTP server. stdlib only, localhost-only by default."""

from __future__ import annotations

import http.server
import json
import socketserver
import os
import signal
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .. import approvals as approvals_mod
from .. import armors as armors_mod
from .. import baseline as baseline_mod
from .. import debug_log
from .. import overrides as overrides_mod
from .. import process_state as process_state_mod
from .. import temper as temper_mod
from .. import task_preflight as task_preflight_mod
from .. import task_channel_gate as task_channel_gate_mod
from .. import task_session as task_session_mod
from .. import weapons as weapons_mod
from ..audit import verify_chain
from . import views
from .auth import ensure_token, ensure_viewer_token


DEFAULT_PORT = 4770
TASK_DEPTHS = {
    "full",
    "enum_only",
    "passive_probe",
    "safe_mutation",
    "authenticated",
    "exploit_validation",
    "writeup_only",
}


def _kill_session(state_root: Path, session_id: str) -> bool:
    """SIGTERM the supervisor pid recorded for a session, escalating to
    SIGKILL after 2s if it doesn't exit. Returns True if a signal was
    sent, False if the pid was unknown / already gone."""
    import errno as _errno
    import os as _os
    import signal as _signal
    import time as _time
    meta_path = state_root / "sessions" / session_id / "session.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False
    pid = meta.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        _os.kill(pid, _signal.SIGTERM)
    except OSError as exc:
        if exc.errno == _errno.ESRCH:
            return False
        raise
    # Best-effort escalation.
    for _ in range(20):
        _time.sleep(0.1)
        try:
            _os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == _errno.ESRCH:
                return True
            break
    try:
        _os.kill(pid, _signal.SIGKILL)
    except OSError:
        pass
    return True


def _descendant_pids(pid: int) -> list[int]:
    import subprocess as _subprocess

    if not isinstance(pid, int) or pid <= 0:
        return []
    try:
        res = _subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            text=True,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return []
    children: dict[int, list[int]] = {}
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            child = int(parts[0])
            parent = int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)
    out: list[int] = []
    stack = list(children.get(pid, ()))
    while stack:
        child = stack.pop(0)
        if child in out:
            continue
        out.append(child)
        stack.extend(children.get(child, ()))
    return sorted(out)


def _signal_task_pid(pid: object) -> bool:
    """Terminate an Armory task launcher process group when possible."""
    import errno as _errno
    import os as _os
    import signal as _signal
    import time as _time

    if not isinstance(pid, int) or pid <= 0:
        return False
    if not process_state_mod.is_pid_live(pid):
        return False
    descendants = _descendant_pids(pid)
    try:
        _os.killpg(pid, _signal.SIGTERM)
    except OSError as exc:
        if exc.errno == _errno.ESRCH:
            return False
        if exc.errno in (_errno.EPERM, getattr(_errno, "ENOTSUP", -1)):
            _os.kill(pid, _signal.SIGTERM)
        else:
            try:
                _os.kill(pid, _signal.SIGTERM)
            except OSError as inner:
                if inner.errno == _errno.ESRCH:
                    return False
                raise
    for child in descendants:
        if process_state_mod.is_pid_live(child):
            try:
                _os.kill(child, _signal.SIGTERM)
            except OSError:
                pass
    for _ in range(20):
        _time.sleep(0.1)
        live_descendants = [
            child for child in descendants
            if process_state_mod.is_pid_live(child)
        ]
        if not process_state_mod.is_pid_live(pid) and not live_descendants:
            return True
    try:
        _os.killpg(pid, _signal.SIGKILL)
    except OSError:
        try:
            _os.kill(pid, _signal.SIGKILL)
        except OSError:
            pass
    for child in descendants:
        if process_state_mod.is_pid_live(child):
            try:
                _os.kill(child, _signal.SIGKILL)
            except OSError:
                pass
    return True


def _latest_task_marker_path(state_root: Path) -> Path | None:
    launch_dir = state_root / "task-launches"
    if not launch_dir.is_dir():
        return None
    candidates = [p for p in launch_dir.glob("task-*.json") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _stop_task_session(state_root: Path, session_id: str | None = None) -> bool:
    killed = _kill_session(state_root, session_id) if session_id else False
    marker_path = _latest_task_marker_path(state_root)
    debug_log.emit(
        "armory", "task.stop.requested",
        state_root=state_root,
        session_id=session_id,
        marker_path=str(marker_path) if marker_path else None,
        killed_session=killed,
    )
    if marker_path is None:
        return killed
    try:
        marker = json.loads(marker_path.read_text())
    except Exception:
        return killed
    if not isinstance(marker, dict):
        return killed
    status = str(marker.get("status") or "requested")
    if status not in {"exited", "stopped", "failed"}:
        killed = _signal_task_pid(marker.get("pid")) or killed
    marker["status"] = "stopped" if killed else ("stale" if status == "requested" else status)
    marker["stopped_at"] = time.time()
    try:
        tmp = marker_path.with_suffix(marker_path.suffix + ".tmp")
        tmp.write_text(json.dumps(marker, indent=2, sort_keys=True))
        tmp.replace(marker_path)
    except OSError:
        pass
    debug_log.emit(
        "armory", "task.stop.completed",
        state_root=state_root,
        session_id=session_id,
        task_id=marker.get("task_run_id"),
        pid=marker.get("pid"),
        status=marker.get("status"),
        killed=killed,
        marker_path=str(marker_path),
    )
    return killed


def _truthy_form_value(value: object) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "off", "no")


def _resolve_worker_executable(worker_cmd: list[str]) -> list[str]:
    if not worker_cmd:
        return worker_cmd
    exe = Path(worker_cmd[0])
    if exe.is_absolute() or exe.name != "hermes":
        return worker_cmd
    local_hermes = Path.home() / ".local" / "bin" / "hermes"
    if local_hermes.exists():
        return [str(local_hermes), *worker_cmd[1:]]
    return worker_cmd


def _preflight_active_trim_for_task(state_root: Path) -> tuple[bool, str]:
    result = task_preflight_mod.check_active_trim_for_task(state_root)
    return result.ok, result.message


def _active_task_trim_context(state_root: Path) -> dict:
    result = task_preflight_mod.check_active_trim_for_task(state_root)
    out: dict = {
        "armor": result.armor,
        "weapon_id": result.weapon_id,
        "trim_id": result.trim_id,
        "temper_id": result.temper_id,
        "window_end": result.window_end,
        "target": "",
    }
    if not result.armor or not result.trim_id:
        return out
    try:
        from .. import trims as _trims_mod
        meta = _trims_mod.load_trim(state_root, result.armor, result.trim_id)
    except Exception:
        return out
    out["target"] = getattr(meta, "target", "") or ""
    return out


def _build_default_hermes_task_prompt(
    *,
    operator_prompt: str,
    task_depth: str,
    context: dict,
) -> str:
    target = str(context.get("target") or "active trim target")
    trim_id = str(context.get("trim_id") or "none")
    window_end = str(context.get("window_end") or "not specified")
    operator_line = (
        f"\nOperator initial message: {operator_prompt}"
        if operator_prompt else ""
    )
    return (
        "Cycle ${I} of the durable ARA task.\n"
        f"Active trim: {trim_id}.\n"
        f"Target/scope: {target}.\n"
        f"Trim stop time: {window_end}.\n"
        "You are autonomous: continue the equipped weapon's current phase, "
        "advance to the next concrete action, and do not ask the operator "
        "what the goal is.\n"
        "A '## Work already committed this engagement' section may appear "
        "above these instructions. It is your durable memory from earlier "
        "cycles (the hermes conversation is time-boxed and may reset, but the "
        "forge store persists). Treat everything listed there as already "
        "established: do NOT repeat that enumeration or re-derive those facts. "
        "Continue forward from it toward untested vulnerability hypotheses.\n"
        "Use contained terminal/process tooling for target URLs. "
        "Use browser tooling only if the active Hermes profile exposes it. "
        "Do not use curl or wget; hermes-default denies them mechanically. "
        "For simple HTTP checks, use an allowed runtime such as python3 "
        "with urllib/request libraries, then immediately persist the "
        "observation through an ARA channel before another target action. "
        "Do not use web_extract or web_search for target URLs. External "
        "search/extraction is only for forge-sealed weapon resources or "
        "explicit operator approval.\n"
        "Structured ARA channel output is required before a phase can be "
        "considered complete. In Hermes, call the exposed MCP tools named "
        "mcp_ara_forge_fact_assert for verified observations, "
        "mcp_ara_forge_lead_file for hypotheses needing follow-up, "
        "mcp_ara_forge_finding_file only for confirmed findings, "
        "mcp_ara_forge_evidence_search/mcp_ara_forge_evidence_get only to "
        "look up captured evidence, and mcp_ara_forge_chunks_emit_tag to advance "
        "the equipped weapon's phase. These tools map to canonical ARA "
        "channels fact.assert, lead.file, finding.file, evidence.search, "
        "evidence.get, and chunks.emit_tag in the audit logs. Evidence lookup "
        "channels do not satisfy task progress or clear action debt; after "
        "looking up evidence, file a fact.assert, lead.file, finding.file, or "
        "chunks.emit_tag record. "
        "Do not let the investigation drift as repeated facts only: after a "
        "small number of target actions, ARA will require a branch update via "
        "lead.file, finding.file, or chunks.emit_tag. That branch update can "
        "open a new hypothesis, refine or close a lead, promote a confirmed "
        "finding, or move through the weapon graph when the evidence supports "
        "it. It does not have to force a phase change. "
        "Do not end a cycle with only prose or terminal output: persist "
        "verified observations with mcp_ara_forge_fact_assert and follow-up "
        "work with mcp_ara_forge_lead_file whenever target interaction "
        "produced usable evidence. If policy denies a tool or shell command, "
        "do not retry terminal blindly; file a lead describing the blocked "
        "action and the next allowed approach. "
        "If a channel call is rejected, report the rejection and continue.\n"
        "Progression strategy: enumerate broadly in the entry chunk, but you "
        "decide when the core surface is mapped (technology, DNS, key paths, "
        "auth surface). When you judge enum sufficient, OR you find something "
        "promising (an exposed admin panel, an auth-bypass hint, a leaked "
        "secret, a suspicious endpoint), do not keep enumerating: advance the "
        "weapon phase with mcp_ara_forge_chunks_emit_tag toward probe to "
        "investigate it, then to intel, then to writeup to draft the finding, "
        "then return to enum to continue broad coverage. Prefer depth on a "
        "promising lead over endless breadth. As your window elapses, shift "
        "from breadth toward driving a promising lead all the way to a "
        "written-up finding.\n"
        "End each cycle with NEXT_PHASE and the next concrete action."
        f"{operator_line}"
    )


def _build_current_chunk_context_python() -> str:
    return (
        "import json,os,pathlib;"
        "root=pathlib.Path(os.environ.get('ARA_STATE_ROOT') or '.');"
        "sid=str(os.environ.get('ARA_SESSION_ID') or '');"
        "weapon=str(os.environ.get('ARA_TASK_WEAPON_ID') or '').strip();"
        "hist=root/'sessions'/sid/'chunk_history.jsonl';"
        "chunk_id='';"
        "\nif hist.is_file():\n"
        "    for line in hist.open(errors='ignore'):\n"
        "        try:\n"
        "            row=json.loads(line)\n"
        "        except Exception:\n"
        "            continue\n"
        "        chunk_id=str(row.get('to_chunk') or chunk_id)\n"
        "if not weapon:\n"
        "    p=root/'equipped_weapon'\n"
        "    if p.is_file():\n"
        "        weapon=p.read_text(errors='ignore').strip()\n"
        "if chunk_id and weapon:\n"
        "    p=root/'weapons'/weapon/'chunks'/(chunk_id + '.json')\n"
        "    try:\n"
        "        chunk=json.loads(p.read_text())\n"
        "    except Exception:\n"
        "        chunk={}\n"
        "    title=str(chunk.get('title') or chunk_id)\n"
        "    exits=chunk.get('exits') or {}\n"
        "    parts=[]\n"
        "    if isinstance(exits, dict):\n"
        "        for tag,target in exits.items():\n"
        "            nxt=''\n"
        "            desc=''\n"
        "            if isinstance(target, dict):\n"
        "                nxt=str(target.get('next_chunk_id') or '')\n"
        "                desc=str(target.get('description') or '')\n"
        "            elif isinstance(target, str):\n"
        "                nxt=target\n"
        "            text=str(tag)\n"
        "            if nxt:\n"
        "                text += ' -> ' + nxt\n"
        "            if desc:\n"
        "                text += ' (' + desc + ')'\n"
        "            parts.append(text)\n"
        "    if parts:\n"
        "        print('Active chunk: ' + chunk_id + ' - ' + title)\n"
        "        print('Valid exits: ' + ' | '.join(parts))\n"
        "        print('Use mcp_ara_forge_chunks_emit_tag with one of these tags when the chunk completion criteria are met.')\n"
        "    elif chunk_id:\n"
        "        print('Active chunk: ' + chunk_id + ' - ' + title)\n"
    )


def _build_durable_hermes_worker(
    worker_cmd: list[str],
    *,
    task_depth: str,
    operator_prompt: str,
    context: dict,
    stamp: str,
) -> list[str]:
    import shlex as _shlex

    hermes = worker_cmd[0]
    marker = f"ara-task-{stamp}"
    prompt = _build_default_hermes_task_prompt(
        operator_prompt=operator_prompt,
        task_depth=task_depth,
        context=context,
    )
    end = str(context.get("window_end") or "")
    budget = context.get("budget_seconds")
    if budget:
        # Focused / single-objective task: a fixed wall-clock budget from now,
        # independent of the (longer) engagement window. Extendable at runtime
        # via the deadline file (see ARA_TASK_DEADLINE_FILE below).
        end_expr = f"$(($(date -u +%s)+{int(budget)}))"
    elif end:
        end_expr = f"$(date -u -d {_shlex.quote(end)} +%s)"
    else:
        end_expr = '$(($(date -u +%s)+${ARA_TASK_MAX_SECONDS:-3600}))'
    structured_count_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "sid=os.environ.get('ARA_SESSION_ID');"
        "p=pathlib.Path(root or '.')/'sessions'/str(sid or '')/'events.jsonl';"
        "n=0;"
        "\nif p.is_file():\n"
        "    f=p.open()\n"
        "    for line in f:\n"
        "        try:\n"
        "            r=json.loads(line); b=r.get('body') or {}\n"
        "        except Exception:\n"
        "            continue\n"
        "        if str(r.get('kind') or '').startswith('channel.') and "
        "b.get('status') != 'rejected' and "
        "b.get('channel') in ("
        "'fact.assert','lead.file','finding.file','chunks.emit_tag'):\n"
        "            n += 1\n"
        "print(n)"
    )
    branch_count_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "sid=os.environ.get('ARA_SESSION_ID');"
        "p=pathlib.Path(root or '.')/'sessions'/str(sid or '')/'events.jsonl';"
        "n=0;"
        "\nif p.is_file():\n"
        "    f=p.open()\n"
        "    for line in f:\n"
        "        try:\n"
        "            r=json.loads(line); b=r.get('body') or {}\n"
        "        except Exception:\n"
        "            continue\n"
        "        if str(r.get('kind') or '').startswith('channel.') and "
        "b.get('status') != 'rejected' and "
        "b.get('channel') in ('lead.file','finding.file','chunks.emit_tag'):\n"
        "            n += 1\n"
        "print(n)"
    )
    # Count of currently-live findings (latest status per id, excluding
    # auto_dismissed/rescinded) — drives the finding-filing forcer.
    finding_count_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "sid=os.environ.get('ARA_SESSION_ID');"
        "p=pathlib.Path(root or '.')/'sessions'/str(sid or '')/'findings.jsonl';"
        "latest={};"
        "\nif p.is_file():\n"
        "    for line in p.open():\n"
        "        try:\n"
        "            d=json.loads(line)\n"
        "        except Exception:\n"
        "            continue\n"
        "        i=d.get('id')\n"
        "        if i:\n"
        "            latest[i]=d.get('status')\n"
        "print(sum(1 for s in latest.values() "
        "if s not in ('auto_dismissed','rescinded')))"
    )
    chunk_transition_count_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "sid=os.environ.get('ARA_SESSION_ID');"
        "events=pathlib.Path(root or '.')/'sessions'/str(sid or '')/'events.jsonl';"
        "n=0;"
        "\nif events.is_file():\n"
        "    for line in events.open():\n"
        "        try:\n"
        "            r=json.loads(line); b=r.get('body') or {}\n"
        "        except Exception:\n"
        "            continue\n"
        "        if str(r.get('kind') or '').startswith('channel.') and "
        "b.get('status') != 'rejected' and b.get('channel') == 'chunks.emit_tag':\n"
        "            n += 1\n"
        "hist=pathlib.Path(root or '.')/'sessions'/str(sid or '')/'chunk_history.jsonl';"
        "\nif hist.is_file():\n"
        "    for line in hist.open():\n"
        "        try:\n"
        "            r=json.loads(line)\n"
        "        except Exception:\n"
        "            continue\n"
        "        if str(r.get('kind') or '') != 'initialize':\n"
        "            n += 1\n"
        "print(n)"
    )
    phase_check_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "tid=os.environ.get('ARA_TASK_RUN_ID');"
        "p=pathlib.Path(root or '.')/'task-runs'/str(tid or '')/'session.json';"
        "\nif root and tid and p.is_file():\n"
        "    print(json.loads(p.read_text()).get('phase','work'))\n"
        "else:\n"
        "    print('work')"
    )
    current_chunk_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "sid=os.environ.get('ARA_SESSION_ID');"
        "p=pathlib.Path(root or '.')/'sessions'/str(sid or '')/'chunk_history.jsonl';"
        "last='';"
        "\nif p.is_file():\n"
        "    for line in p.open():\n"
        "        try:\n"
        "            r=json.loads(line)\n"
        "        except Exception:\n"
        "            continue\n"
        "        last = str(r.get('to_chunk') or last)\n"
        "print(last)"
    )
    chunk_gated_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "tid=os.environ.get('ARA_TASK_RUN_ID');"
        "p=pathlib.Path(root or '.')/'task-runs'/str(tid or '')/'session.json';"
        "gated=False;"
        "\nif root and tid and p.is_file():\n"
        "    try:\n"
        "        d=json.loads(p.read_text());"
        "        n=int(d.get('branch_updates_since_chunk_transition') or 0);"
        "        t=int(d.get('branch_chunk_gate_threshold') or 5);"
        "        gated=(str(d.get('phase') or '')=='branch_required' and n>=max(1,t))\n"
        "    except Exception:\n"
        "        gated=False\n"
        "print('1' if gated else '0')"
    )
    current_chunk_context_py = _build_current_chunk_context_python()
    # Cross-cycle grounding: summarize the facts/leads already committed to the
    # forge store so each cycle can be reminded of its own durable memory. The
    # hermes conversation is time-boxed and can be killed mid-turn; the forge
    # store is what actually persists, so re-injecting it stops the agent from
    # restarting enumeration every cycle.
    committed_summary_py = (
        "import json,os,pathlib;"
        "root=os.environ.get('ARA_STATE_ROOT');"
        "sid=os.environ.get('ARA_SESSION_ID');"
        "base=pathlib.Path(root or '.')/'sessions'/str(sid or '');"
        "facts=[];leads=[];"
        "\nfp=base/'facts.jsonl'\n"
        "if fp.is_file():\n"
        "    for line in fp.open(errors='ignore'):\n"
        "        try:\n"
        "            d=json.loads(line)\n"
        "        except Exception:\n"
        "            continue\n"
        "        pred=str(d.get('predicate') or '').strip()\n"
        "        val=d.get('value')\n"
        "        note=str(d.get('note') or '').strip()\n"
        "        v=val if isinstance(val,str) else (json.dumps(val) if val is not None else '')\n"
        "        txt=(pred+': '+str(v)) if pred else str(v)\n"
        "        if not txt.strip():\n"
        "            txt=note\n"
        "        if txt.strip():\n"
        "            facts.append(txt.strip()[:130])\n"
        "lp=base/'leads.jsonl'\n"
        "if lp.is_file():\n"
        "    for line in lp.open(errors='ignore'):\n"
        "        try:\n"
        "            d=json.loads(line)\n"
        "        except Exception:\n"
        "            continue\n"
        "        t=str(d.get('title') or '').strip()\n"
        "        if t:\n"
        "            leads.append(t[:110])\n"
        "if facts or leads:\n"
        "    print('## Work already committed this engagement')\n"
        "    print('This is your durable memory across cycles. Do NOT repeat "
        "this enumeration or re-derive these observations — continue forward "
        "from here toward untested vulnerability hypotheses.')\n"
        "    print('')\n"
        "    for f in facts[-30:]:\n"
        "        print('- fact: '+f)\n"
        "    for l in leads[-15:]:\n"
        "        print('- lead: '+l)\n"
    )
    commit_prompt = (
        "COMMIT_REQUIRED. You have taken a target action without filing a "
        "valid structured ARA channel. Do not call terminal, browser, web, "
        "or file tools. Call exactly one forge MCP tool now to record the "
        "MOST IMPORTANT real, un-filed observation from your recent actions "
        "— use its actual substance, never a placeholder:\n"
        "- mcp_ara_forge_finding_file if you have CONFIRMED a vulnerability "
        "or security issue. Provide: title, severity, description of the "
        "impact, evidence_refs (ids from evidence.search/get that PROVE it — "
        "required), and confirmation_method naming the concrete checkable "
        "identifiers present in that evidence (URL path, HTTP status, the "
        "leaked field). The finding is mechanically re-checked against the "
        "cited evidence, so cite evidence that actually demonstrates the "
        "claim;\n"
        "- mcp_ara_forge_lead_file for a concrete hypothesis or suspicious "
        "behavior worth investigating next (real title + reasoning);\n"
        "- mcp_ara_forge_fact_assert for a verified factual observation.\n"
        "Prefer finding.file when you have already confirmed an issue this "
        "cycle. Do NOT file a generic 'commit debt repair' or placeholder "
        "entry — record what you actually found."
    )
    branch_prompt = (
        "BRANCH_REQUIRED. The investigation ledger needs a branch update "
        "before any more target action. Do not call terminal, browser, web, "
        "or file tools. Call exactly one MCP tool now: "
        "mcp_ara_forge_lead_file, mcp_ara_forge_finding_file, or "
        "mcp_ara_forge_chunks_emit_tag. Use lead.file to open, refine, or "
        "close a follow-up branch; finding.file only for confirmed findings; "
        "chunks.emit_tag only when the active weapon chunk has a valid exit "
        "tag supported by the current evidence. Do not invent <go>."
    )
    chunk_transition_prompt = (
        "BRANCH_REQUIRED_CHUNK_TRANSITION. This task has filed enough branch "
        "updates in the active weapon chunk. Do not call terminal, browser, "
        "web, file, mcp_ara_forge_lead_file, mcp_ara_forge_finding_file, or "
        "mcp_ara_forge_fact_assert. Call exactly one MCP tool now: "
        "mcp_ara_forge_chunks_emit_tag. Use one valid exit tag from the "
        "Active chunk context only. Do not invent <go>."
    )
    time_advance_prompt = (
        "TIME_ADVANCE. You are past the halfway point of your engagement "
        "window and still mapping surface in the entry chunk. Do not call "
        "terminal, browser, web, file, mcp_ara_forge_fact_assert, "
        "mcp_ara_forge_lead_file, or mcp_ara_forge_finding_file. Call exactly "
        "one MCP tool now: mcp_ara_forge_chunks_emit_tag with the valid "
        "forward exit tag from the Active chunk context that advances toward "
        "probe. Do not invent a tag."
    )
    finding_force_prompt = (
        "FINDING_DECISION_REQUIRED. Your engagement window is nearly over and "
        "you have committed observations but filed ZERO findings. Stop "
        "gathering and DECIDE now. Do not call terminal, browser, web, or file "
        "tools. Review what you have CONFIRMED and call exactly one MCP tool:\n"
        "1) mcp_ara_forge_finding_file for your single STRONGEST confirmed "
        "vulnerability — provide title, severity, a short description, "
        "evidence_refs (the captured-evidence ids that PROVE it; look them up "
        "with mcp_ara_forge_evidence_search if needed), and confirmation_method "
        "naming the concrete checkable identifiers present in that evidence "
        "(the URL path, the HTTP status, the leaked field). The finding is "
        "re-checked against that evidence, so cite evidence that actually "
        "shows the issue.\n"
        "2) ONLY if, after honest review, you have genuinely confirmed NO "
        "vulnerability: call mcp_ara_forge_fact_assert with predicate "
        "'assessment.no_confirmed_findings' and a one-line reason. Do not "
        "enumerate further; decide now."
    )
    script = (
        f"END={end_expr}; "
        "START=$(date -u +%s); "
        # Extendable deadline: when ARA_TASK_DEADLINE_FILE is set (focused tasks),
        # seed it with END once and read it live each cycle so `ara task-extend`
        # can push the deadline out. Unset (normal engagement runs) => the reader
        # just echoes the fixed END, so behavior is identical.
        'if [ -n "${ARA_TASK_DEADLINE_FILE:-}" ] && [ ! -s "${ARA_TASK_DEADLINE_FILE:-}" ]; then '
        'echo "$END" > "$ARA_TASK_DEADLINE_FILE" 2>/dev/null || true; fi; '
        '_ara_deadline() { if [ -n "${ARA_TASK_DEADLINE_FILE:-}" ] && [ -s "$ARA_TASK_DEADLINE_FILE" ]; then '
        'cat "$ARA_TASK_DEADLINE_FILE" 2>/dev/null || echo "$END"; else echo "$END"; fi; }; '
        "ARA_TASK_CYCLE_TIMEOUT=${ARA_TASK_CYCLE_TIMEOUT:-90}; "
        "_ara_structured_count() { "
        f"python3.9 -c {_shlex.quote(structured_count_py)} 2>/dev/null || echo 0; "
        "}; "
        "_ara_branch_count() { "
        f"python3.9 -c {_shlex.quote(branch_count_py)} 2>/dev/null || echo 0; "
        "}; "
        "_ara_chunk_transition_count() { "
        f"python3.9 -c {_shlex.quote(chunk_transition_count_py)} 2>/dev/null || echo 0; "
        "}; "
        "_ara_task_phase() { "
        f"python3.9 -c {_shlex.quote(phase_check_py)} 2>/dev/null || echo work; "
        "}; "
        "_ara_current_chunk() { "
        f"python3.9 -c {_shlex.quote(current_chunk_py)} 2>/dev/null || true; "
        "}; "
        "_ara_current_chunk_context() { "
        # Focused tasks (deadline file set) suppress the weapon's enum-oriented
        # chunk guidance so the operator's commit-first directive dominates.
        'if [ -n "${ARA_TASK_DEADLINE_FILE:-}" ]; then return 0; fi; '
        f"python3.9 -c {_shlex.quote(current_chunk_context_py)} 2>/dev/null || true; "
        "}; "
        "_ara_committed_summary() { "
        f"python3.9 -c {_shlex.quote(committed_summary_py)} 2>/dev/null || true; "
        "}; "
        "_ara_task_chunk_gated() { "
        f"python3.9 -c {_shlex.quote(chunk_gated_py)} 2>/dev/null || echo 0; "
        "}; "
        "_ara_finding_count() { "
        f"python3.9 -c {_shlex.quote(finding_count_py)} 2>/dev/null || echo 0; "
        "}; "
        "I=1; "
        "EMPTY_CYCLES=0; "
        "COMMIT_RETRIES=0; "
        "BRANCH_RETRIES=0; "
        "FINDING_FORCES=0; "
        "CONN_FAILS=0; "
        "ARA_TASK_MAX_CONN_RETRIES=${ARA_TASK_MAX_CONN_RETRIES:-6}; "
        "ARA_TASK_CONN_BACKOFF=${ARA_TASK_CONN_BACKOFF:-10}; "
        "ARA_TASK_MAX_EMPTY_CYCLES=${ARA_TASK_MAX_EMPTY_CYCLES:-6}; "
        "ARA_TASK_MAX_COMMIT_RETRIES=${ARA_TASK_MAX_COMMIT_RETRIES:-1}; "
        "ARA_TASK_MAX_BRANCH_RETRIES=${ARA_TASK_MAX_BRANCH_RETRIES:-1}; "
        "ARA_TASK_MAX_CHUNK_TRANSITION_REPAIRS=${ARA_TASK_MAX_CHUNK_TRANSITION_REPAIRS:-2}; "
        "ARA_TASK_FINDING_FORCE_PCT=${ARA_TASK_FINDING_FORCE_PCT:-70}; "
        "ARA_TASK_MAX_FINDING_FORCES=${ARA_TASK_MAX_FINDING_FORCES:-3}; "
        'while [ "$(date -u +%s)" -lt "$(_ara_deadline)" ]; do '
        'REMAINING=$(( $(_ara_deadline) - $(date -u +%s) )); '
        'if [ "$REMAINING" -le 3 ]; then break; fi; '
        "CYCLE_TIMEOUT=$((ARA_TASK_CYCLE_TIMEOUT < REMAINING ? ARA_TASK_CYCLE_TIMEOUT : REMAINING)); "
        "CURRENT_CHUNK=$(_ara_current_chunk); "
        'OPREVIEW_NUDGE=""; '
        'if [ "$CURRENT_CHUNK" = "operator_review" ]; then '
        'if [ -n "${ARA_TASK_RUN_ID:-}" ]; then '
        'echo "[ara-task-gate] operator_review on unattended run; nudging back to work" >&2; '
        'OPREVIEW_NUDGE="NO OPERATOR IS AVAILABLE — this is an unattended run; do NOT wait for review. Continue working: drive your strongest lead to a written-up finding, or move to the next untested area. Do not re-enter operator_review."; '
        'else '
        'echo "[ara-task-gate] operator review requested; stopping durable task loop" >&2; '
        "break; "
        "fi; "
        "fi; "
        "ARA_NOW=$(date -u +%s); ARA_SPAN=$(( $(_ara_deadline) - START )); "
        'if [ "$ARA_SPAN" -le 0 ]; then PCT=100; else PCT=$(( (ARA_NOW - START) * 100 / ARA_SPAN )); fi; '
        # Phase gates (verify/branch/commit) are HARD requirements and must be
        # resolved before any advisory time-forcer. Compute phase up front and
        # gate every forcer on phase==work, so a pending gate can't be preempted
        # by a forcer that `continue`s. (Bug this fixes: at PCT>=85 the writeup
        # forcer short-circuited the loop every iteration, so a verify_required
        # tripped late in the window never reached its handler and never cleared.)
        "PHASE=$(_ara_task_phase); "
        # Finding-filing forcer: late in the window, if work has been committed
        # but NO finding filed, force a mandatory finding.file decision (or an
        # explicit no-findings assertion). Bounded by ARA_TASK_MAX_FINDING_FORCES
        # so a genuinely finding-less run isn't spun forever.
        'if [ "$PHASE" = "work" ] && [ "$PCT" -ge "$ARA_TASK_FINDING_FORCE_PCT" ] && [ "$FINDING_FORCES" -lt "$ARA_TASK_MAX_FINDING_FORCES" ] && [ "$CURRENT_CHUNK" != "enum" ] && [ "$CURRENT_CHUNK" != "operator_review" ]; then '
        "ARA_FIND_CT=$(_ara_finding_count); "
        "ARA_STRUCT_CT=$(_ara_structured_count); "
        'if [ "${ARA_FIND_CT:-0}" -eq 0 ] && [ "${ARA_STRUCT_CT:-0}" -ge 1 ]; then '
        "FINDING_FORCES=$((FINDING_FORCES+1)); "
        'echo "[ara-task-gate] finding-forcer ${PCT}%: ${ARA_STRUCT_CT} commits but 0 findings — forcing finding decision (attempt ${FINDING_FORCES}/${ARA_TASK_MAX_FINDING_FORCES})" >&2; '
        "CHUNK_CONTEXT=$(_ara_current_chunk_context); "
        "PRIOR_WORK=$(_ara_committed_summary); "
        f"FF_PROMPT=$(printf '%s\\n\\n%s\\n\\n%s' \"$PRIOR_WORK\" \"$CHUNK_CONTEXT\" {_shlex.quote(finding_force_prompt)}); "
        f'timeout "$CYCLE_TIMEOUT" {_shlex.quote(hermes)} '
        f"--accept-hooks --continue {_shlex.quote(marker)} "
        '-z "$FF_PROMPT" || true; '
        "I=$((I+1)); sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        "fi; "
        'if [ "$PHASE" = "work" ] && [ "$PCT" -ge 85 ] && [ -n "$CURRENT_CHUNK" ] && [ "$CURRENT_CHUNK" != "writeup" ] && [ "$CURRENT_CHUNK" != "operator_review" ]; then '
        'echo "[ara-task-gate] time-forcer ${PCT}%: driving $CURRENT_CHUNK toward writeup" >&2; '
        "python3.9 -m ara.cli task-force-chunk-advance >&2 || true; "
        "I=$((I+1)); sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'if [ "$PHASE" = "work" ] && [ "$PCT" -ge 50 ] && [ "$CURRENT_CHUNK" = "enum" ]; then '
        'echo "[ara-task-gate] time-forcer ${PCT}%: enum past halfway; requesting advance to probe" >&2; '
        "ARA_TF_BEFORE=$(_ara_chunk_transition_count); "
        "CHUNK_CONTEXT=$(_ara_current_chunk_context); "
        'if [ -n "$CHUNK_CONTEXT" ]; then '
        f"TA_PROMPT=$(printf '%s\\n\\n%s' \"$CHUNK_CONTEXT\" {_shlex.quote(time_advance_prompt)}); "
        "else "
        f"TA_PROMPT={_shlex.quote(time_advance_prompt)}; "
        "fi; "
        f'timeout "$CYCLE_TIMEOUT" {_shlex.quote(hermes)} '
        f"--accept-hooks --continue {_shlex.quote(marker)} "
        '-z "$TA_PROMPT" || true; '
        "ARA_TF_AFTER=$(_ara_chunk_transition_count); "
        'if [ "${ARA_TF_AFTER:-0}" -le "${ARA_TF_BEFORE:-0}" ]; then '
        'echo "[ara-task-gate] time-forcer ${PCT}%: agent did not advance; forcing" >&2; '
        "python3.9 -m ara.cli task-force-chunk-advance >&2 || true; "
        "fi; "
        "I=$((I+1)); sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        "PHASE=$(_ara_task_phase); "
        'if [ "$PHASE" = "branch_required" ]; then '
        "ARA_BRANCH_BEFORE=$(_ara_branch_count); "
        "BRANCH_RETRIES=$((BRANCH_RETRIES+1)); "
        'if [ "$BRANCH_RETRIES" -le "$ARA_TASK_MAX_BRANCH_RETRIES" ]; then '
        'echo "[ara-task-gate] branch update required before next action; running branch-only turn" >&2; '
        "ARA_CHUNK_GATED=$(_ara_task_chunk_gated); "
        "ARA_CHUNK_BEFORE=$(_ara_chunk_transition_count); "
        "CHUNK_CONTEXT=$(_ara_current_chunk_context); "
        'if [ "$ARA_CHUNK_GATED" = "1" ]; then '
        'if [ -n "$CHUNK_CONTEXT" ]; then '
        f"BRANCH_PROMPT=$(printf '%s\\n\\n%s' \"$CHUNK_CONTEXT\" {_shlex.quote(chunk_transition_prompt)}); "
        "else "
        f"BRANCH_PROMPT={_shlex.quote(chunk_transition_prompt)}; "
        "fi; "
        "else "
        f"BRANCH_PROMPT={_shlex.quote(branch_prompt)}; "
        "fi; "
        f'timeout "$CYCLE_TIMEOUT" {_shlex.quote(hermes)} '
        f"--accept-hooks --continue {_shlex.quote(marker)} "
        '-z "$BRANCH_PROMPT" || true; '
        "ARA_BRANCH_AFTER=$(_ara_branch_count); "
        "ARA_CHUNK_AFTER=$(_ara_chunk_transition_count); "
        "PHASE_AFTER=$(_ara_task_phase); "
        'if { [ "$ARA_CHUNK_GATED" = "1" ] && '
        '[ "$PHASE_AFTER" = "work" ] && '
        '[ "${ARA_CHUNK_AFTER:-0}" -gt "${ARA_CHUNK_BEFORE:-0}" ]; } || '
        '{ [ "$ARA_CHUNK_GATED" != "1" ] && '
        '[ "$PHASE_AFTER" = "work" ]; }; then '
        "EMPTY_CYCLES=0; CONN_FAILS=0; COMMIT_RETRIES=0; BRANCH_RETRIES=0; I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'if [ "$ARA_CHUNK_GATED" = "1" ]; then '
        'echo "[ara-task-gate] chunk transition missing; running system chunk transition repair" >&2; '
        "python3.9 -m ara.cli task-repair-chunk-transition; REPAIR_RC=$?; "
        "ARA_CHUNK_AFTER=$(_ara_chunk_transition_count); "
        "PHASE_AFTER=$(_ara_task_phase); "
        'if [ "$REPAIR_RC" -eq 0 ] && [ "$PHASE_AFTER" = "work" ] && '
        '[ "${ARA_CHUNK_AFTER:-0}" -gt "${ARA_CHUNK_BEFORE:-0}" ]; then '
        "EMPTY_CYCLES=0; CONN_FAILS=0; COMMIT_RETRIES=0; BRANCH_RETRIES=0; I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'if [ "$REPAIR_RC" -eq 3 ]; then '
        'echo "[ara-task-gate] CHUNK_TRANSITION_REPAIR_LIMIT_OR_NO_EXIT" >&2; break; '
        "fi; "
        'echo "[ara-task-gate] chunk transition repair failed; stopping durable task loop" >&2; '
        "break; "
        "fi; "
        "I=$((I+1)); sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'echo "[ara-task-gate] branch update missing; stopping durable task loop" >&2; '
        "break; "
        "fi; "
        'if [ "$PHASE" = "verify_required" ]; then '
        'echo "[ara-task-gate] verify required; running mechanical verification checkpoint" >&2; '
        "python3.9 -m ara.cli task-verify-findings >&2 || true; "
        "PHASE_AFTER=$(_ara_task_phase); "
        'if [ "$PHASE_AFTER" = "verify_required" ]; then '
        'echo "[ara-task-gate] verification checkpoint did not clear; stopping durable task loop" >&2; '
        "break; "
        "fi; "
        "EMPTY_CYCLES=0; CONN_FAILS=0; COMMIT_RETRIES=0; BRANCH_RETRIES=0; I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'if [ "$PHASE" = "commit_required" ]; then '
        "ARA_FACTS_BEFORE=$(_ara_structured_count); "
        "COMMIT_RETRIES=$((COMMIT_RETRIES+1)); "
        'if [ "$COMMIT_RETRIES" -le "$ARA_TASK_MAX_COMMIT_RETRIES" ]; then '
        'echo "[ara-task-gate] commit required before next action; running commit-only turn" >&2; '
        f'timeout "$CYCLE_TIMEOUT" {_shlex.quote(hermes)} '
        f"--accept-hooks --continue {_shlex.quote(marker)} "
        f"-z {_shlex.quote(commit_prompt)} || true; "
        "ARA_FACTS_AFTER=$(_ara_structured_count); "
        'if [ "${ARA_FACTS_AFTER:-0}" -gt "${ARA_FACTS_BEFORE:-0}" ]; then '
        "EMPTY_CYCLES=0; CONN_FAILS=0; COMMIT_RETRIES=0; BRANCH_RETRIES=0; I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        "I=$((I+1)); sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'echo "[ara-task-gate] commit retries exhausted; running system repair" >&2; '
        "python3.9 -m ara.cli task-repair-commit; REPAIR_RC=$?; "
        "ARA_FACTS_AFTER=$(_ara_structured_count); "
        'if [ "$REPAIR_RC" -eq 0 ] && [ "${ARA_FACTS_AFTER:-0}" -gt "${ARA_FACTS_BEFORE:-0}" ]; then '
        "EMPTY_CYCLES=0; CONN_FAILS=0; COMMIT_RETRIES=0; BRANCH_RETRIES=0; I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'if [ "$REPAIR_RC" -eq 3 ]; then '
        'echo "[ara-task-gate] REPAIR_LIMIT_EXCEEDED" >&2; break; '
        "fi; "
        'echo "[ara-task-gate] commit repair failed; stopping durable task loop" >&2; '
        "break; "
        "fi; "
        "ARA_FACTS_BEFORE=$(_ara_structured_count); "
        "CHUNK_CONTEXT=$(_ara_current_chunk_context); "
        "PRIOR_WORK=$(_ara_committed_summary); "
        'TIME_CONTEXT="You are ${PCT}% through your engagement window."; '
        'if [ "$PCT" -ge 50 ]; then TIME_CONTEXT="$TIME_CONTEXT Past halfway: prioritize driving a promising lead through probe to a written-up finding over more breadth."; fi; '
        f"TURN_PROMPT=$(printf '%s\\n\\n%s\\n\\n%s\\n\\n%s\\n\\n%s' \"$OPREVIEW_NUDGE\" \"$TIME_CONTEXT\" \"$PRIOR_WORK\" \"$CHUNK_CONTEXT\" {_shlex.quote(prompt)}); "
        'ARA_CYCLE_LOG="${TMPDIR:-/tmp}/ara-cycle-$$.log"; '
        f'timeout "$CYCLE_TIMEOUT" {_shlex.quote(hermes)} '
        f"--accept-hooks --continue {_shlex.quote(marker)} "
        '-z "$TURN_PROMPT" > "$ARA_CYCLE_LOG" 2>&1; ARA_HRC=$?; '
        'cat "$ARA_CYCLE_LOG" >&2 2>/dev/null || true; '
        "ARA_FACTS_AFTER=$(_ara_structured_count); "
        'if [ "${ARA_FACTS_AFTER:-0}" -le "${ARA_FACTS_BEFORE:-0}" ]; then '
        # Connection-death resilience: a dropped model connection (e.g. the
        # inference server shedding load under concurrency) is a transient infra
        # failure, NOT the agent finishing — so it gets its own reconnect budget
        # with exponential backoff, separate from the empty-cycle tolerance. A
        # model flap pauses+retries instead of ending the run; only a model that
        # stays unreachable across every attempt stops the loop.
        'if grep -qiE "Connection lost|ConnectionResetError|Connection reset by peer|Connection refused|no final response was produced" "$ARA_CYCLE_LOG" 2>/dev/null; then '
        "CONN_FAILS=$((CONN_FAILS+1)); "
        'if [ "$CONN_FAILS" -lt "$ARA_TASK_MAX_CONN_RETRIES" ]; then '
        "ARA_CB=$(( ARA_TASK_CONN_BACKOFF * (1 << (CONN_FAILS < 5 ? CONN_FAILS : 5)) )); "
        'echo "[ara-task-gate] model connection lost (reconnect ${CONN_FAILS}/${ARA_TASK_MAX_CONN_RETRIES}); backing off ${ARA_CB}s for the model to recover" >&2; '
        'I=$((I+1)); sleep "$ARA_CB"; continue; '
        "fi; "
        'echo "[ara-task-gate] model unreachable after ${CONN_FAILS} reconnect attempts; stopping durable task loop" >&2; '
        "break; "
        "fi; "
        "PHASE=$(_ara_task_phase); "
        'if [ "$PHASE" = "commit_required" ]; then '
        "COMMIT_RETRIES=$((COMMIT_RETRIES+1)); "
        'if [ "$COMMIT_RETRIES" -le "$ARA_TASK_MAX_COMMIT_RETRIES" ]; then '
        'echo "[ara-task-gate] commit required; running commit-only turn" >&2; '
        f'timeout "$CYCLE_TIMEOUT" {_shlex.quote(hermes)} '
        f"--accept-hooks --continue {_shlex.quote(marker)} "
        f"-z {_shlex.quote(commit_prompt)} || true; "
        "ARA_FACTS_AFTER=$(_ara_structured_count); "
        'if [ "${ARA_FACTS_AFTER:-0}" -gt "${ARA_FACTS_BEFORE:-0}" ]; then '
        "EMPTY_CYCLES=0; CONN_FAILS=0; COMMIT_RETRIES=0; BRANCH_RETRIES=0; I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        "I=$((I+1)); sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'echo "[ara-task-gate] commit retries exhausted; running system repair" >&2; '
        "python3.9 -m ara.cli task-repair-commit; REPAIR_RC=$?; "
        "ARA_FACTS_AFTER=$(_ara_structured_count); "
        'if [ "$REPAIR_RC" -eq 0 ] && [ "${ARA_FACTS_AFTER:-0}" -gt "${ARA_FACTS_BEFORE:-0}" ]; then '
        "EMPTY_CYCLES=0; CONN_FAILS=0; COMMIT_RETRIES=0; BRANCH_RETRIES=0; I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; continue; "
        "fi; "
        'if [ "$REPAIR_RC" -eq 3 ]; then '
        'echo "[ara-task-gate] REPAIR_LIMIT_EXCEEDED" >&2; break; '
        "fi; "
        'echo "[ara-task-gate] commit repair failed; stopping durable task loop" >&2; '
        "break; "
        "fi; "
        "EMPTY_CYCLES=$((EMPTY_CYCLES+1)); "
        'if [ "$EMPTY_CYCLES" -lt "$ARA_TASK_MAX_EMPTY_CYCLES" ]; then '
        'echo "[ara-task-gate] no new structured channel output this cycle '
        '(${EMPTY_CYCLES}/${ARA_TASK_MAX_EMPTY_CYCLES}); continuing" >&2; '
        "I=$((I+1)); "
        # Recovery backoff: an empty cycle is often a transient turn failure
        # (e.g. a dropped hermes<->guard connection breaking auto-approval).
        # Back off progressively so the connection has time to recover before
        # the next attempt, instead of burning the tolerance in seconds.
        "sleep $(( ${ARA_TASK_CYCLE_SLEEP:-20} + EMPTY_CYCLES * 15 )); "
        "continue; "
        "fi; "
        'echo "[ara-task-gate] structured channel output missing; '
        'stopping durable task loop" >&2; '
        "break; "
        "fi; "
        "EMPTY_CYCLES=0; "
        "CONN_FAILS=0; "
        "COMMIT_RETRIES=0; "
        "BRANCH_RETRIES=0; "
        "I=$((I+1)); "
        "sleep ${ARA_TASK_CYCLE_SLEEP:-20}; "
        "done"
    )
    return ["bash", "-lc", script]


def _should_wrap_default_hermes(worker_cmd: list[str], body: dict) -> bool:
    if not _truthy_form_value(body.get("contain")):
        return False
    if not worker_cmd:
        return False
    if Path(worker_cmd[0]).name != "hermes":
        return False
    return len(worker_cmd) == 1 and not body.get("single_turn")


def _recent_session_dirs(sessions_dir: Path, n: int = 10) -> list:
    """Top-``n`` most-recently-modified session dirs via ``os.scandir`` +
    heap-select — bounds the per-render cost so the dashboard doesn't build a
    full sorted list of thousands of session dirs on every request/tick."""
    import os as _os
    import heapq as _heapq
    try:
        with _os.scandir(sessions_dir) as it:
            ents = [(e.stat().st_mtime, e.path) for e in it if e.is_dir()]
    except OSError:
        return []
    top = _heapq.nlargest(n, ents, key=lambda x: x[0])
    return [Path(p) for _mt, p in top]


def _any_task_running(state_root: Path) -> bool:
    """True if a *task* run (a contained ``armory-task`` durable loop) is active.

    Deliberately EXCLUDES always-on service launches — notably the
    ``hermes-gateway`` — which also run under ``ara.cli launch`` with a live pid;
    counting those would permanently block operator Polish dispatch. Identified
    by the live process's cmdline carrying the ``armory-task`` contain profile.
    """
    sessions_root = state_root / "sessions"
    if not sessions_root.is_dir():
        return False
    for entry in sessions_root.iterdir():
        meta = entry / "session.json"
        if not (entry.is_dir() and _pid_is_live(meta)):
            continue
        try:
            pid = json.loads(meta.read_text()).get("pid")
        except Exception:  # noqa: BLE001
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            cmd = ""
        if "armory-task" in cmd:
            return True
    return False


def _start_polish_task(state_root: Path, body: dict) -> tuple[bool, str]:
    """Operator 'Polish' action — dispatch a short, budgeted focused task to
    confirm / refute / write up / promote ONE finding, lead, or fact, optionally
    with custom instructions. Reuses the proven `_start_task_session` path."""
    from . import polish as _polish
    from . import forge_v1_views as _fv

    item_type = str(body.get("item_type") or "").strip()
    item_id = str(body.get("item_id") or "").strip()
    mode = str(body.get("mode") or "").strip()
    instructions = str(body.get("instructions") or "").strip()
    if not _polish.is_valid(item_type, mode):
        return False, f"Unknown polish action: {item_type}/{mode}"
    if not item_id:
        return False, "Missing item id."
    if _any_task_running(state_root):
        return False, ("A task is already running — stop it before dispatching a "
                       "focused Polish task (concurrent contained runs share the "
                       "session store).")

    sd = _fv._resolve_session_dir(state_root)
    if sd is None:
        return False, "No active session to load the item from."
    item = None
    if item_type == "finding":
        _, findings = _fv._findings_for(state_root)
        item = next((x for x in findings if x.get("id") == item_id), None)
    elif item_type == "lead":
        rows = list(_fv._latest_per_id(_fv._read_jsonl(sd / "leads.jsonl")).values())
        item = next((x for x in rows if x.get("id") == item_id), None)
    elif item_type == "fact":
        rows = list(_fv._latest_per_id(_fv._read_jsonl(sd / "facts.jsonl")).values())
        item = next((x for x in rows if x.get("id") == item_id), None)
    if item is None:
        return False, f"{item_type} {item_id} not found in the active session."

    directive = _polish.build_directive(item_type, item, mode, instructions)
    ok, msg = _start_task_session(state_root, {
        "prompt": directive,
        "task_depth": _polish.task_depth_for(item_type, mode),
        "max_seconds": _polish.DEFAULT_BUDGET_SECONDS,
        "worker_cmd": "hermes",
        "contain": "on",
        # Resume the session the item lives in, so the focused task sees the
        # engagement's accumulated findings/leads/facts/evidence and writes
        # its updated finding / promoted lead back into that same session.
        "resume_session_id": sd.name,
    })
    if ok:
        debug_log.emit(
            "polish", "dispatched", state_root=state_root,
            item_type=item_type, item_id=item_id, mode=mode,
            has_instructions=bool(instructions),
        )
        return True, (f"Dispatched focused task: {_polish.label_for(item_type, mode)} "
                      f"for {item_id} (10 min budget).")
    return ok, msg


def _start_task_session(state_root: Path, body: dict) -> tuple[bool, str]:
    """Start an ARA-supervised task from Armory.

    This deliberately shells out to the same CLI path operators used before
    the web UI existed: ``python -m ara.cli launch --state-root ...``. That
    keeps session creation, armor WALLS selection, equipped-weapon runtime
    mounting, containment, MITM, and transcript wiring in one existing path.
    """
    import shlex as _shlex
    import subprocess as _subprocess

    preflight = task_preflight_mod.check_active_trim_for_task(state_root)
    if not preflight.ok:
        debug_log.emit(
            "armory", "task.start.preflight_failed",
            state_root=state_root,
            message=preflight.message,
            body=body,
        )
        return False, preflight.message
    task_run = task_session_mod.TaskRunSession.new(
        state_root,
        armor_id=preflight.armor or "",
        weapon_id=preflight.weapon_id or "",
        trim_id=preflight.trim_id or "",
        temper_id=preflight.temper_id or "",
        window_end=preflight.window_end,
        action_budget=int(os.environ.get("ARA_TASK_ACTION_BUDGET", "1") or "1"),
        branch_action_budget=int(
            os.environ.get("ARA_TASK_BRANCH_BUDGET", "3") or "3"),
        verify_budget=int(os.environ.get("ARA_TASK_VERIFY_BUDGET", "3") or "3"),
    )
    try:
        temper_projection = temper_mod.project_active_temper_for_task(
            state_root,
            preflight.armor or "",
            task_run.task_id,
            trim_id=preflight.trim_id,
            weapon_id=preflight.weapon_id,
        )
    except Exception as exc:  # noqa: BLE001
        debug_log.emit_exception(
            "armory", "task.start.temper_projection_failed",
            exc,
            state_root=state_root,
            task_id=task_run.task_id,
            armor_id=preflight.armor,
            weapon_id=preflight.weapon_id,
            trim_id=preflight.trim_id,
            temper_id=preflight.temper_id,
        )
        return False, f"Temper projection failed: {exc}"

    worker_text = str(body.get("worker_cmd") or "hermes").strip()
    try:
        worker_cmd = _shlex.split(worker_text)
    except ValueError as exc:
        debug_log.emit_exception(
            "armory", "task.start.invalid_worker",
            exc,
            state_root=state_root,
            task_id=task_run.task_id,
            worker_text=worker_text,
        )
        return False, f"Invalid worker command: {exc}"
    if not worker_cmd:
        debug_log.emit(
            "armory", "task.start.invalid_worker",
            state_root=state_root,
            task_id=task_run.task_id,
            worker_text=worker_text,
        )
        return False, "Worker command required"
    worker_cmd = _resolve_worker_executable(worker_cmd)
    task_depth = str(body.get("task_depth") or "full").strip()
    if task_depth not in TASK_DEPTHS:
        task_depth = "full"

    prompt = str(body.get("prompt") or "").strip()
    # Focused / single-objective tasks (operator "Polish" actions) run on a
    # fixed wall-clock budget instead of the full engagement window, and are
    # extendable at runtime via `ara task-extend`.
    try:
        max_seconds = int(body.get("max_seconds") or 0)
    except (TypeError, ValueError):
        max_seconds = 0
    log_dir = state_root / "task-launches"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    supervisor_mode = "custom-worker"
    run_task_body = dict(body)
    run_task_body["contain"] = "on"
    if _should_wrap_default_hermes(worker_cmd, run_task_body):
        _ctx = _active_task_trim_context(state_root)
        if max_seconds > 0:
            _ctx = dict(_ctx)
            _ctx["budget_seconds"] = max_seconds
        worker_cmd = _build_durable_hermes_worker(
            worker_cmd,
            task_depth=task_depth,
            operator_prompt=prompt,
            context=_ctx,
            stamp=stamp,
        )
        supervisor_mode = "armory-hermes-loop"
    elif prompt:
        if any("{prompt}" in part for part in worker_cmd):
            worker_cmd = [part.replace("{prompt}", prompt) for part in worker_cmd]
        elif Path(worker_cmd[0]).name == "hermes":
            worker_cmd.extend(["-z", prompt])

    profile = "armory-task"
    cmd = [
        sys.executable,
        "-m",
        "ara.cli",
        "launch",
        "--state-root",
        str(state_root),
    ]
    cmd.extend(["--contain", "--contain-profile", profile])
    cmd.extend(["--", *worker_cmd])

    log_path = log_dir / f"task-{stamp}.log"
    env = os.environ.copy()
    src_root = str(Path(__file__).resolve().parents[2])
    prior_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{src_root}{os.pathsep}{prior_pp}" if prior_pp else src_root
    )
    # Propagate the shim fail-open flag into the task's contained hermes when
    # set on the supervisor/launcher env (repair hatch — see shim.c). Explicit
    # so it survives the launch→contain env chain regardless of launcher.
    if os.environ.get("ARA_SHIM_FAILOPEN"):
        env["ARA_SHIM_FAILOPEN"] = os.environ["ARA_SHIM_FAILOPEN"]
    # Enforce net default-deny at the libc shim for autonomous task runs: the
    # active trim now projects a full net allowlist (target + control-plane) +
    # `deny net *:*`, so a connect() off that list must fail SAFE (EACCES)
    # rather than AUDIT_ONLY-leaking to the internet (the direct-socket egress
    # gap). Scoped to task runs; other session types keep legacy behavior
    # until their allowlists are migrated. Operator override honored.
    env["ARA_SHIM_NET_ENFORCE"] = os.environ.get("ARA_SHIM_NET_ENFORCE", "1")
    # Kernel-level egress enforcement (seccomp-notif) — the unbypassable
    # backstop above the shim (catches static binaries / raw syscalls). Feature-
    # detected in launch; falls back to shim-only on kernels < 5.5. Same
    # bulwark policy as the shim, so decisions are identical.
    env["ARA_SECCOMP_NET_ENFORCE"] = os.environ.get(
        "ARA_SECCOMP_NET_ENFORCE", "1")
    env["ARA_TASK_DEPTH"] = task_depth
    env["ARA_TASK_SUPERVISOR"] = supervisor_mode
    env["ARA_TASK_ARMOR_ID"] = preflight.armor or ""
    env["ARA_TASK_WEAPON_ID"] = preflight.weapon_id or ""
    env["ARA_TASK_TRIM_ID"] = preflight.trim_id or ""
    env["ARA_TASK_TEMPER_ID"] = preflight.temper_id or ""
    env["ARA_TASK_RUN_ID"] = task_run.task_id
    if max_seconds > 0:
        # Extendable deadline file for focused tasks. The loop seeds it with its
        # computed END and reads it live each cycle; `ara task-extend` bumps it.
        _dl = state_root / "task-runs" / task_run.task_id / "deadline"
        _dl.parent.mkdir(parents=True, exist_ok=True)
        env["ARA_TASK_DEADLINE_FILE"] = str(_dl)
        env["ARA_TASK_BUDGET_SECONDS"] = str(max_seconds)
    _resume_sid = str(body.get("resume_session_id") or "").strip()
    if _resume_sid and (state_root / "sessions" / _resume_sid).is_dir():
        # Attach to a prior session's forge store (findings/leads/facts/evidence)
        # so the focused task sees that context and writes updates back into it.
        env["ARA_RESUME_SESSION_ID"] = _resume_sid
    env["ARA_TASK_ABSOLUTE_PATH"] = str(
        temper_projection.get("absolute_path") or "",
    )
    env["ARA_TASK_ACTION_BUDGET"] = str(task_run.action_budget)
    env["ARA_TASK_VERIFY_BUDGET"] = str(task_run.verify_budget)
    env["ARA_DEBUG"] = "1"
    env["ARA_DEBUG_LOG"] = str(state_root / "logs" / debug_log.DEFAULT_LOG_NAME)
    debug_log.emit(
        "armory", "task.start.spawn",
        state_root=state_root,
        task_id=task_run.task_id,
        worker_command=worker_cmd,
        task_depth=task_depth,
        supervisor_mode=supervisor_mode,
        profile=profile,
        debug_log=env["ARA_DEBUG_LOG"],
        armor_id=preflight.armor,
        weapon_id=preflight.weapon_id,
        trim_id=preflight.trim_id,
        temper_id=preflight.temper_id,
        temper_projection=temper_projection,
    )
    try:
        log_f = open(log_path, "ab", buffering=0)
        proc = _subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[3]),
            stdin=_subprocess.DEVNULL,
            stdout=log_f,
            stderr=_subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        debug_log.emit_exception(
            "armory", "task.start.spawn_failed",
            exc,
            state_root=state_root,
            task_id=task_run.task_id,
            command=cmd,
        )
        return False, f"Start failed: {exc}"
    finally:
        try:
            log_f.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass

    marker = {
        "ts": time.time(),
        "pid": proc.pid,
        "command": cmd,
        "worker_command": worker_cmd,
        "task_run_id": task_run.task_id,
        "armor_id": preflight.armor,
        "weapon_id": preflight.weapon_id,
        "trim_id": preflight.trim_id,
        "temper_id": preflight.temper_id,
        "temper_projection": temper_projection,
        "task_depth": task_depth,
        "supervisor_mode": supervisor_mode,
        "hermes_profile_mode": "ara-task" if supervisor_mode == "armory-hermes-loop" else "custom",
        "mcp_required": supervisor_mode == "armory-hermes-loop",
        "shim_required": True,
        "channel_required": supervisor_mode == "armory-hermes-loop",
        "channel_contract": {
            "canonical_tools": list(task_channel_gate_mod.CANONICAL_TASK_CHANNELS),
            "bad_aliases": task_channel_gate_mod.BAD_CHANNEL_ALIASES,
            "chunk_transition_required": task_depth != "enum_only",
            "completion_source": [
                "events.jsonl channel.* rows",
                "chunk_history.jsonl swap/jump/rewind rows",
            ],
        },
        "target_external_extractors_blocked": supervisor_mode == "armory-hermes-loop",
        "status": "requested",
        "log": str(log_path),
    }
    marker_path = log_dir / f"task-{stamp}.json"
    try:
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True)
        )
    except OSError:
        pass
    debug_log.emit(
        "armory", "task.start.requested",
        state_root=state_root,
        task_id=task_run.task_id,
        pid=proc.pid,
        marker_path=str(marker_path),
        log_path=str(log_path),
        marker=marker,
    )
    _track_task_launcher(proc, marker_path)
    return True, f"Task start requested; launcher pid {proc.pid}"


def _track_task_launcher(proc, marker_path: Path) -> None:
    """Reap an Armory-started launcher and stamp its final marker state."""
    def _wait_and_update() -> None:
        try:
            rc = proc.wait()
        except Exception:  # noqa: BLE001
            return
        try:
            data = json.loads(marker_path.read_text())
            if isinstance(data, dict):
                data["status"] = "exited"
                data["exit_code"] = rc
                data["completed_at"] = time.time()
                tmp = marker_path.with_suffix(marker_path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
                tmp.replace(marker_path)
                debug_log.emit(
                    "armory", "task.launcher.exited",
                    state_root=Path(data.get("state_root") or marker_path.parents[1]),
                    session_id=data.get("session_id"),
                    task_id=data.get("task_run_id"),
                    pid=data.get("pid"),
                    exit_code=rc,
                    marker_path=str(marker_path),
                )
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(
        target=_wait_and_update,
        name=f"ara-task-reaper-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    ).start()


def start_hermes_guard_thread(state_root, interval_s=45, run=None, sleep=None,
                              should_stop=None, join=False):
    """Start a daemon thread that periodically reconciles the hermes gateway
    (force-kill rogue / relaunch contained / circuit-break). Side-effecting
    deps are injectable for testing."""
    import threading
    import time as _t
    if run is None:
        from .. import hermes_guard as hg
        run = lambda now: hg.run_reconcile(now, paths=None)
    if sleep is None:
        sleep = _t.sleep
    if should_stop is None:
        should_stop = lambda: False

    def _loop():
        while not should_stop():
            try:
                run(_t.time())
            except Exception:
                pass
            sleep(interval_s)

    th = threading.Thread(target=_loop, name="ara-hermes-guard", daemon=True)
    th.start()
    if join:
        th.join(timeout=5)
    return th


def build_session_export(state_root: Path, session_id: str) -> bytes | None:
    """Return a gzipped tarball of a session's artifacts, or ``None`` if
    the session directory doesn't exist. Layout inside the tarball:

        <session_id>/session.json
        <session_id>/events.jsonl
        <session_id>/policy_hits.jsonl
        <session_id>/transcript.jsonl
        <session_id>/contain.manifest
        <session_id>/approvals.jsonl
        <session_id>/contain/... (full subtree, files only)
        <session_id>/ATTESTATION.json  (audit chain last_hash + counts)
    """
    import io as _io
    import json as _json
    import tarfile as _tarfile
    import time as _time
    from ..audit import verify_chain
    sdir = state_root / "sessions" / session_id
    if not sdir.is_dir():
        return None

    # Audit chain attestation — last hash + verification status at
    # export time. A reader can re-run verify_chain() on the unpacked
    # policy_hits.jsonl and confirm they still get the same last_hash.
    hits_path = sdir / "policy_hits.jsonl"
    chain_ok: bool = True
    chain_lines: int = 0
    chain_err: str | None = None
    last_hash: str | None = None
    if hits_path.exists():
        chain_ok, chain_lines, chain_err = verify_chain(hits_path)
        try:
            import json as __json
            for line in hits_path.read_text().splitlines():
                if not line:
                    continue
                try:
                    rec = __json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                ph = rec.get("prev_hash")
                if isinstance(ph, str):
                    last_hash = ph
        except OSError:
            pass
    attestation = {
        "session_id": session_id,
        "exported_at": _time.time(),
        "audit_chain": {
            "present": hits_path.exists(),
            "ok": bool(chain_ok),
            "lines_verified": int(chain_lines),
            "error": chain_err,
            "last_prev_hash_seen": last_hash,
        },
    }

    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in (
            "session.json",
            "events.jsonl",
            "policy_hits.jsonl",
            "transcript.jsonl",
            "contain.manifest",
            "approvals.jsonl",
        ):
            f = sdir / name
            if f.is_file():
                tf.add(str(f), arcname=f"{session_id}/{name}")
        contain = sdir / "contain"
        if contain.is_dir():
            for p in contain.rglob("*"):
                if p.is_file():
                    tf.add(
                        str(p),
                        arcname=f"{session_id}/contain/{p.relative_to(contain)}",
                    )
        # Attestation blob inlined into the tar so a reader has
        # everything they need from the single file.
        att_bytes = _json.dumps(
            attestation, indent=2, sort_keys=True,
        ).encode()
        import tarfile as _tf2
        info = _tf2.TarInfo(name=f"{session_id}/ATTESTATION.json")
        info.size = len(att_bytes)
        info.mtime = int(_time.time())
        tf.addfile(info, _io.BytesIO(att_bytes))
    return buf.getvalue()


def build_session_export_signature(
    blob: bytes, secret: str,
) -> bytes:
    """Detached HMAC-SHA256 signature over the tarball bytes. The secret
    is the Armory admin token by default — anyone with the token can
    produce or verify the signature. Not a cryptographic guarantee of
    provenance, but rules out silent tampering by anyone without the
    token."""
    import hashlib as _hashlib
    import hmac as _hmac
    mac = _hmac.new(secret.encode(), blob, _hashlib.sha256)
    return (mac.hexdigest() + "\n").encode()


def append_absolute_section(
    state_root: Path, armor_name: str, title: str, body_text: str,
) -> tuple[bool, str]:
    """Append one ``## title`` section to an armor's active absolute
    file. Returns (ok, message)."""
    armor = armors_mod.load_armor(state_root, armor_name)
    if armor is None:
        return False, f"armor not found: {armor_name}"
    target = armor.absolute_path
    try:
        existing = target.read_text() if target.exists() else ""
        if existing and not existing.endswith("\n\n"):
            sep = "\n\n" if not existing.endswith("\n") else "\n"
        else:
            sep = ""
        addition = f"## {title}\n\n{body_text.strip()}\n"
        new_text = existing + sep + addition
        target.parent.mkdir(parents=True, exist_ok=True)
        import os as _os, tempfile
        tmp = tempfile.NamedTemporaryFile(
            mode="w", delete=False, dir=str(target.parent),
            prefix=target.name + ".", suffix=".tmp",
        )
        tmp.write(new_text); tmp.flush(); _os.fsync(tmp.fileno()); tmp.close()
        _os.replace(tmp.name, target)
    except OSError as exc:
        return False, f"append failed: {exc}"
    return True, "ok"


def _pid_is_live(meta_path: Path) -> bool:
    """Best-effort: True if the session's recorded pid is still running."""
    if not meta_path.exists():
        return False
    try:
        pid = json.loads(meta_path.read_text()).get("pid")
    except Exception:
        return False
    return process_state_mod.is_pid_live(pid)


def _delete_archived_session(
    state_root: Path, session_id: str,
) -> tuple[bool, str]:
    """Remove ``<state>/sessions/<id>/`` if the session is not running.
    Returns (ok, message)."""
    import shutil as _shutil
    sess_dir = state_root / "sessions" / session_id
    if not sess_dir.is_dir():
        return False, "Session not found"
    if _pid_is_live(sess_dir / "session.json"):
        return False, "Session is still running — terminate it first"
    try:
        _shutil.rmtree(sess_dir)
    except OSError as exc:
        return False, f"Delete failed: {exc}"
    return True, f"Deleted session {session_id[:12]}"


def _cleanup_archived_sessions(
    state_root: Path, *, max_age_days: float,
) -> tuple[int, int]:
    """Delete archived session dirs older than ``max_age_days``. Skips
    sessions whose recorded pid is still live. Returns
    (removed_count, skipped_live_count)."""
    import shutil as _shutil
    import time as _time
    sessions_root = state_root / "sessions"
    if not sessions_root.is_dir():
        return 0, 0
    cutoff = _time.time() - max_age_days * 86400.0
    removed = 0
    skipped = 0
    for entry in sessions_root.iterdir():
        if not entry.is_dir():
            continue
        if _pid_is_live(entry / "session.json"):
            skipped += 1
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            _shutil.rmtree(entry)
            removed += 1
        except OSError:
            pass
    return removed, skipped


@dataclass
class ArmoryConfig:
    state_root: Path
    walls_file: Path | None
    token: str
    viewer_token: str = ""
    bind: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    tls: bool = False
    behind_proxy: bool = False


class _Handler(http.server.BaseHTTPRequestHandler):
    config: ArmoryConfig  # set by factory

    def log_message(self, format: str, *args) -> None:  # quieter default log
        client = self.address_string()
        if self.config.behind_proxy:
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                # First hop in XFF is the original client.
                client = f"{fwd.split(',')[0].strip()} via {client}"
        sys.stderr.write(
            f"[armory] {client} - {format % args}\n"
        )

    # ---- utility ----
    def _auth_role(self) -> str | None:
        """Return ``"admin"`` if the supplied token matches the primary
        token, ``"viewer"`` if it matches the read-only derived token,
        ``None`` otherwise. Viewer tokens pass GET but are rejected
        from every POST by the handler."""
        parsed = urllib.parse.urlparse(self.path)
        qs = dict(urllib.parse.parse_qsl(parsed.query))
        supplied = qs.get("t") or self._cookie_token()
        if not supplied:
            return None
        if supplied == self.config.token:
            return "admin"
        if self.config.viewer_token and supplied == self.config.viewer_token:
            return "viewer"
        return None

    def _auth_ok(self) -> bool:
        return self._auth_role() is not None

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for item in raw.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                if k == "armory_token":
                    return v
        return None

    def _body(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw_bytes = self.rfile.read(length)
        self._cached_raw_body_bytes = raw_bytes
        if "multipart/form-data" in self.headers.get("Content-Type", ""):
            self._cached_raw_body = ""
            return {}
        raw = raw_bytes.decode(errors="replace")
        # Cache the raw body so subsequent helpers (e.g. _body_multi)
        # can re-parse without re-reading the (already-consumed) socket.
        self._cached_raw_body = raw
        return dict(urllib.parse.parse_qsl(raw))

    def _multipart_body(self) -> dict[str, object]:
        """Parse a multipart/form-data body already cached by _body()."""
        content_type = self.headers.get("Content-Type", "")
        raw = getattr(self, "_cached_raw_body_bytes", b"")
        if not raw or "multipart/form-data" not in content_type:
            return {}
        from email.parser import BytesParser
        from email.policy import default

        header = (
            f"Content-Type: {content_type}\r\n"
            "MIME-Version: 1.0\r\n\r\n"
        ).encode()
        msg = BytesParser(policy=default).parsebytes(header + raw)
        out: dict[str, object] = {}
        if not msg.is_multipart():
            return out
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is not None:
                out[name] = payload
                out[f"{name}_filename"] = filename
            else:
                out[name] = payload.decode(errors="replace")
        return out

    def _body_multi(self, name: str) -> list[str]:
        """Return all values for ``name`` from the form body (preserves
        duplicates — needed for checkbox groups). Must be called after
        ``_body()`` has consumed the request stream."""
        raw = getattr(self, "_cached_raw_body", "")
        return [v for k, v in urllib.parse.parse_qsl(raw) if k == name]

    def _send_html(self, html_body: str, code: int = 200) -> None:
        data = html_body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_plain(self, body: str, code: int = 200) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, *, content_type: str = "application/octet-stream",
                    filename: str | None = None, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path: str, append_token: bool = True) -> None:
        if append_token and "t=" not in path:
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}t={urllib.parse.quote(self.config.token)}"
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _unauthorized(self) -> None:
        # Used for POSTs that require auth — keep the plain 401 since
        # bouncing form submits through /login is bad UX. GETs go
        # through `_redirect_to_login` instead.
        self._send_html(
            '<!doctype html><html><body>'
            '<h1>Armory</h1><p>Missing or invalid token.</p>'
            '<p>POST endpoints reject unauthenticated requests with 401. '
            'Visit <a href="/login">/login</a> in a browser to sign in '
            'first.</p>'
            '</body></html>',
            code=401,
        )

    def _redirect_to_login(self) -> None:
        """For unauthenticated GETs, send a 302 to /login carrying the
        original URL so the user lands back where they tried to go."""
        next_url = self.path or "/"
        self.send_response(302)
        self.send_header(
            "Location",
            f"/login?next={urllib.parse.quote(next_url, safe='')}",
        )
        self.end_headers()

    def _set_token_cookie_and_redirect(
        self, token: str, target: str,
    ) -> None:
        """303 to ``target`` with a session cookie carrying ``token``.
        ``target`` is sanitised to a same-origin path; absolute URLs and
        protocol-relative URLs are rewritten to ``/``."""
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        cookie = (
            f"armory_token={token}; Path=/; SameSite=Strict; "
            "Max-Age=2592000"  # 30 days
        )
        if self.config.tls:
            cookie += "; Secure"
        self.send_response(303)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Location", target)
        self.end_headers()

    def _clear_cookie_and_redirect(self, target: str) -> None:
        self.send_response(303)
        self.send_header(
            "Set-Cookie",
            "armory_token=; Path=/; Max-Age=0; SameSite=Strict",
        )
        self.send_header("Location", target)
        self.end_headers()

    def _login_form_html(
        self, *, next_url: str = "/", error: str | None = None,
    ) -> str:
        from html import escape as _esc
        err_html = (
            '<div class="ara-callout" '
            'style="border-left:3px solid var(--error);margin-bottom:14px">'
            f'{_esc(error)}</div>' if error else ""
        )
        return (
            '<!doctype html><html><head>'
            '<meta charset="utf-8">'
            '<title>ARA · Sign in</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<link rel="stylesheet" href="/static/armory.css?v=20260613-redesign-11">'
            '</head><body>'
            '<header class="ara-top"><span class="brand">ARA</span></header>'
            '<main class="ara-main" '
            'style="max-width:380px;margin:80px auto;padding:0 16px">'
            '<h1 class="ara-h" style="margin-top:0">Sign in</h1>'
            '<p class="ara-sub">Enter the Armory token. The admin token '
            'grants full access; the viewer token (from '
            '<code>armory-viewer-token</code>) is read-only.</p>'
            + err_html +
            '<form method="post" action="/login" class="ara-form" '
            'style="flex-direction:column;align-items:stretch">'
            f'<input type="hidden" name="next" value="{_esc(next_url)}">'
            '<label>Token'
            '<input type="password" name="password" autofocus required '
            'autocomplete="current-password" '
            'style="width:100%;padding:8px 10px;'
            'background:var(--surface-container-lowest);'
            'color:var(--on-surface);border:1px solid '
            'color-mix(in srgb,var(--outline-variant) 25%,transparent);'
            'border-radius:var(--radius-sm);font-family:var(--font-mono)">'
            '</label>'
            '<button class="ara-btn ara-btn-primary" type="submit" '
            'style="margin-top:14px">Sign in</button>'
            '</form>'
            '<p class="ara-sub" style="margin-top:24px;font-size:11px">'
            'Tokens live under the state root: '
            '<code>armory-token</code> and '
            '<code>armory-viewer-token</code>.</p>'
            '</main></body></html>'
        )

    def _try_login(self, password: str) -> str | None:
        """Match ``password`` against the configured tokens; return
        which one it is (``"admin"`` or ``"viewer"``) or ``None``."""
        if password and password == self.config.token:
            return "admin"
        if (
            password and self.config.viewer_token
            and password == self.config.viewer_token
        ):
            return "viewer"
        return None

    # ---- dispatch ----
    def _serve_rhema_stream(self) -> None:
        """SSE endpoint for the /rhema page. Polls every session's
        ``transcript.jsonl`` size; whenever any of them grows we emit a
        ``data:`` event with a running count of new records since the
        stream opened. The page uses this to flash a "N new turns ·
        refresh" banner without continuously re-rendering pair UI.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        state_root = self.config.state_root
        sessions_dir = state_root / "sessions"
        positions: dict[Path, int] = {}
        new_count = 0
        stream_started = time.time()
        last_heartbeat = stream_started

        def flush(payload: bytes) -> bool:
            try:
                self.wfile.write(payload)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        if not flush(b": ara-armory rhema stream\n\n"):
            return

        while True:
            if sessions_dir.is_dir():
                try:
                    sessions = _recent_session_dirs(sessions_dir, 10)
                except OSError:
                    sessions = []
                for sess in sessions:
                    fpath = sess / "transcript.jsonl"
                    if not fpath.exists():
                        continue
                    try:
                        size = fpath.stat().st_size
                    except OSError:
                        continue
                    last = positions.get(fpath)
                    if last is None:
                        try:
                            ctime = sess.stat().st_ctime
                        except OSError:
                            ctime = 0
                        positions[fpath] = 0 if ctime >= stream_started else size
                        continue
                    if size <= last:
                        continue
                    try:
                        with fpath.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(last)
                            chunk = f.read()
                            positions[fpath] = f.tell()
                    except OSError:
                        continue
                    for line in chunk.splitlines():
                        if line.strip():
                            new_count += 1
                    payload = (
                        "data: "
                        + json.dumps({"new_since_open": new_count})
                        + "\n\n"
                    ).encode("utf-8")
                    if not flush(payload):
                        return

            now = time.time()
            if now - last_heartbeat >= 15:
                if not flush(b": heartbeat\n\n"):
                    return
                last_heartbeat = now
            time.sleep(0.5)

    def _serve_audit_stream(self) -> None:
        """Server-Sent Events endpoint. Tails events.jsonl + policy_hits.jsonl
        across recent sessions and emits each new line as SSE ``data:`` events
        containing the pre-rendered HTML row. Client prepends it into the
        audit table without a page refresh.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        state_root = self.config.state_root
        sessions_dir = state_root / "sessions"
        # Per-file read positions. Files we encountered before the stream
        # opened are considered "history" and get positioned at EOF (no
        # replay). Sessions created AFTER stream open stream from position
        # 0 so even short-lived worker runs don't get swallowed between
        # polls.
        positions: dict[Path, int] = {}
        stream_started = time.time()
        last_heartbeat = stream_started

        def flush(payload: bytes) -> bool:
            try:
                self.wfile.write(payload)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        # Initial "hello" so the client knows we're live.
        if not flush(b": ara-armory audit stream\n\n"):
            return

        while True:
            if sessions_dir.is_dir():
                try:
                    sessions = _recent_session_dirs(sessions_dir, 10)
                except OSError:
                    sessions = []
                for sess in sessions:
                    for fname in ("events.jsonl", "policy_hits.jsonl"):
                        fpath = sess / fname
                        if not fpath.exists():
                            continue
                        try:
                            size = fpath.stat().st_size
                        except OSError:
                            continue
                        last = positions.get(fpath)
                        if last is None:
                            # First encounter. If the session existed
                            # before the stream opened, skip its history;
                            # otherwise replay from the start so short
                            # runs aren't swallowed between polls.
                            try:
                                sess_created = sess.stat().st_ctime
                            except OSError:
                                sess_created = 0
                            last = 0 if sess_created >= stream_started else size
                            positions[fpath] = last
                            if last == size:
                                continue
                        if size <= last:
                            continue
                        try:
                            with fpath.open("r", encoding="utf-8", errors="replace") as f:
                                f.seek(last)
                                chunk = f.read()
                                positions[fpath] = f.tell()
                        except OSError:
                            continue
                        for line in chunk.splitlines():
                            if not line.strip():
                                continue
                            try:
                                rec = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            rec["_session"] = sess.name[:10]
                            rec["_source"] = fname
                            html_row = views.render_audit_row(rec)
                            payload = (
                                "data: "
                                + json.dumps({
                                    "html": html_row,
                                    "session": rec["_session"],
                                    "verdict": rec.get("verdict") or "",
                                })
                                + "\n\n"
                            ).encode("utf-8")
                            if not flush(payload):
                                return

            # Heartbeat every 15s so intermediaries don't idle the socket.
            now = time.time()
            if now - last_heartbeat >= 15:
                if not flush(b": heartbeat\n\n"):
                    return
                last_heartbeat = now

            time.sleep(0.5)

    def _serve_static(self, path: str) -> bool:
        # /static/<name> served from package dir. Token-auth'd like the
        # rest of the app so random probes don't leak filesystem layout.
        if not path.startswith("/static/"):
            return False
        name = path[len("/static/"):]
        if ".." in name.split("/"):
            self._send_html("<h1>Not found</h1>", code=404)
            return True
        import mimetypes
        static_dir = (Path(__file__).parent / "static").resolve()
        target = (static_dir / name).resolve()
        # Containment: target must stay under static_dir.
        try:
            target.relative_to(static_dir)
        except ValueError:
            self._send_html("<h1>Not found</h1>", code=404)
            return True
        if not target.is_file():
            self._send_html("<h1>Not found</h1>", code=404)
            return True
        ctype, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, max-age=0")
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._send_html("<h1>ok</h1>")
            return
        # /login is auth-free GET (renders the form); /static/* same so
        # the page CSS loads even when not yet signed in.
        if path == "/login":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            supplied = qs.get("t") or ""
            if self._try_login(supplied) is not None:
                self._set_token_cookie_and_redirect(
                    supplied, qs.get("next") or "/",
                )
                return
            self._send_html(self._login_form_html(
                next_url=qs.get("next") or "/",
            ))
            return
        if path.startswith("/static/"):
            if self._serve_static(path):
                return
        if not self._auth_ok():
            self._redirect_to_login()
            return
        if self._serve_static(path):
            return

        tok = self.config.token

        if path in ("/", "/status"):
            self._send_html(views.render_status(
                self.config.state_root, tok,
                viewer_token=(
                    self.config.viewer_token
                    if self._auth_role() == "admin" else ""
                ),
            ))
            return
        if path == "/courtyard":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            try:
                window_s = float(qs.get("window") or 3600.0)
            except ValueError:
                window_s = 3600.0
            self._send_html(views.render_courtyard(
                self.config.state_root, tok,
                tab=qs.get("tab", "sessions"),
                window_s=window_s,
                session_filter=qs.get("session") or None,
            ))
            return
        if path == "/debug":
            from . import debug_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(debug_views.render_debug_page(
                self.config.state_root, self.config.token,
                qs.get("session") or None))
            return
        if path == "/debug/tail":
            from . import debug_views
            import json as _json
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            offsets = {k: qs.get(k) for k, *_rest in debug_views._STREAMS}
            data = debug_views.debug_tail(
                self.config.state_root, qs.get("session", ""), offsets)
            self._send_plain(_json.dumps(data))
            return
        if path == "/sessions":
            # /sessions is the legacy index URL from the Fix-3 split; the
            # page was renamed back to Courtyard. Keep deep-links like
            # /sessions/<id> canonical (handled elsewhere); only the
            # index 302s.
            target = "/courtyard"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        # /baselines was merged into Armor Sets. The later-defined
        # redirect handles the operator-facing case; this old direct
        # renderer is kept in views.py for legacy callers but unwired.
        if path == "/approvals":
            self._redirect("/courtyard?tab=approvals")
            return
        if path == "/canaries":
            # Standalone canaries page retired; seeding lives in the
            # Bulwark guided builder (Canary mode), events surface on
            # /audit (verdict=flag).
            self._redirect("/audit?verdict=flag")
            return
        if path == "/bulwark":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(views.render_bulwark(
                self.config.state_root, tok,
                scope_filter=qs.get("scope", "all"),
                layer_filter=qs.get("layer", "all"),
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        if path == "/walls":
            self._redirect("/bulwark?layer=fs")
            return
        if path == "/rhema":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(views.render_rhema(
                self.config.state_root, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
                channel_filter=qs.get("channel"),
                status_filter=qs.get("status"),
                rule_filter=qs.get("rule"),
                sort_key=qs.get("sort"),
                tab=qs.get("tab", "live"),
                armor_override=qs.get("armor") or None,
                absolute_override=qs.get("absolute") or None,
                edit=(qs.get("edit") == "1"),
            ))
            return
        if path == "/absolute":
            # Folded into /rhema as the Envelope tab; preserve query
            # params (armor, absolute, edit, flash) so any bookmarked or
            # form-posted redirect keeps working transparently.
            qs = urllib.parse.parse_qs(parsed.query)
            fwd: list[str] = ["tab=envelope"]
            for k in ("armor", "absolute", "edit", "flash", "flash_level"):
                v = (qs.get(k) or [""])[0]
                if v:
                    fwd.append(f"{k}={urllib.parse.quote(v)}")
            self._redirect("/rhema?" + "&".join(fwd))
            return
        # Legacy overrides/blackholes pages — merged into /allowlist
        # with allow/deny tabs. Redirect to the new URL so bookmarks
        # and form actions keep working.
        if path == "/overrides":
            self._redirect(f"/allowlist?tab=allow", append_token=True)
            return
        if path == "/blackholes":
            self._redirect(f"/allowlist?tab=deny", append_token=True)
            return
        if path == "/allowlist":
            self._redirect("/bulwark")
            return
        if path in ("/overrides", "/blackholes"):
            self._redirect("/bulwark")
            return
        if path == "/danger":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(views.render_danger(
                self.config.state_root, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        if path == "/settings":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(views.render_settings(
                self.config.state_root, tok,
                viewer_token=(
                    self.config.viewer_token
                    if self._auth_role() == "admin" else ""
                ),
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        # Legacy /baselines — folded into Armor Sets page. Bookmark
        # redirects to /armors.
        if path == "/baselines":
            self._redirect("/armors", append_token=True)
            return
        if path == "/events":
            self._serve_audit_stream()
            return
        if path == "/rhema/events":
            self._serve_rhema_stream()
            return
        if path == "/rhema/turn":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            sid = qs.get("sid", "")
            try:
                line_idx = int(qs.get("line", "-1"))
            except ValueError:
                line_idx = -1
            try:
                offset = int(qs["off"]) if "off" in qs else None
            except ValueError:
                offset = None
            html = views.render_rhema_turn_detail(
                self.config.state_root, sid,
                line_idx if line_idx >= 0 else None,
                offset=offset,
            )
            if html is None:
                self._send_html(
                    '<div class="ara-empty" style="margin:14px 18px">'
                    'turn not found</div>',
                    code=404,
                )
            else:
                self._send_html(html)
            return
        if path == "/armors":
            self._send_html(views.render_armors(self.config.state_root, tok))
            return
        if path == "/armors/new":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(views.render_armor_new(tok, flash=qs.get("flash")))
            return
        if path == "/pixel-gallery":
            self._send_html(views.render_pixel_gallery(tok))
            return
        if path.startswith("/armors/") and path.endswith("/export"):
            name = path[len("/armors/"):-len("/export")]
            try:
                blob = armors_mod.export_armor(self.config.state_root, name)
            except FileNotFoundError:
                self._send_html("<h1>Armor not found</h1>", code=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="armor-{name}.tgz"',
            )
            self.end_headers()
            self.wfile.write(blob)
            return
        if path.startswith("/armors/"):
            name = path[len("/armors/"):]
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            page = views.render_armor_detail(
                self.config.state_root, name, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
                absolute_edit=(qs.get("absolute_edit") == "1"),
                walls_edit=(qs.get("walls_edit") == "1"),
                tab=qs.get("tab", "identity"),
            )
            if page is None:
                self._send_html("<h1>Armor not found</h1>", code=404)
            else:
                self._send_html(page)
            return

        # ---- Trims ------------------------------------------------------
        if path == "/trims":
            from . import trim_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(trim_views.render_trims_index(
                self.config.state_root, tok,
                armor_filter=qs.get("armor") or None,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        if path == "/trims/new":
            from . import trim_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(trim_views.render_trim_new(
                self.config.state_root, tok, flash=qs.get("flash"),
            ))
            return
        if path == "/trims/derive":
            from . import trim_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(trim_views.render_trim_derive(
                self.config.state_root, tok, flash=qs.get("flash"),
            ))
            return
        if path.startswith("/trims/"):
            tail = path[len("/trims/"):]
            if "/" in tail:
                from . import trim_views
                armor_seg, _, trim_id = tail.partition("/")
                armor_seg = urllib.parse.unquote(armor_seg)
                trim_id = urllib.parse.unquote(trim_id)
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                self._send_html(trim_views.render_trim_detail(
                    self.config.state_root, armor_seg, trim_id, tok,
                    flash=qs.get("flash"),
                    flash_level=qs.get("flash_level", "info"),
                ))
                return

        # ---- Temper -----------------------------------------------------
        if path == "/temper":
            from . import temper_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(temper_views.render_temper_index(
                self.config.state_root, tok,
                armor=qs.get("armor") or None,
                trim=qs.get("trim") or None,
                forge=qs.get("forge") or None,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        if path == "/temper/create":
            from . import temper_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(temper_views.render_temper_create(
                self.config.state_root, tok,
                armor=qs.get("armor") or None,
                trim=qs.get("trim") or None,
                forge=qs.get("forge") or None,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        if path.startswith("/temper/"):
            from . import temper_views
            tail = path[len("/temper/"):]
            if "/" in tail:
                armor_seg, _, proposal_id = tail.partition("/")
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                self._send_html(temper_views.render_temper_detail(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(proposal_id),
                    tok,
                    flash=qs.get("flash"),
                    flash_level=qs.get("flash_level", "info"),
                ))
                return

        # ---- Tools (classification registry) ----------------------------
        if path == "/tools":
            from . import tool_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(tool_views.render_tools_index(
                self.config.state_root, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
                filter_status=qs.get("status"),
            ))
            return
        if path.startswith("/tools/"):
            from . import tool_views
            key = urllib.parse.unquote(path[len("/tools/"):])
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(tool_views.render_tool_detail(
                self.config.state_root, key, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return

        # ---- Mats --------------------------------------------------------
        if path == "/mats":
            from . import mats_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            open_eid = (qs.get("eid") or "").strip()
            if open_eid:
                self._send_html(mats_views.render_mats_detail(
                    self.config.state_root, open_eid, tok,
                    flash=qs.get("flash"),
                    flash_level=qs.get("flash_level", "info"),
                ))
            else:
                self._send_html(mats_views.render_mats_index(
                    self.config.state_root, tok,
                    flash=qs.get("flash"),
                    flash_level=qs.get("flash_level", "info"),
                ))
            return
        if path == "/mats/inventory":
            from . import mats_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(mats_views.render_mats_inventory(
                self.config.state_root, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        if path.startswith("/mats/"):
            from . import mats_views
            eid = urllib.parse.unquote(path[len("/mats/"):])
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(mats_views.render_mats_detail(
                self.config.state_root, eid, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return

        # ---- TOD modal ---------------------------------------------------
        if path == "/tod":
            from . import tod_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(tod_views.render_tod_index(
                self.config.state_root, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return
        if path.startswith("/tod/"):
            from . import tod_views
            eid = urllib.parse.unquote(path[len("/tod/"):])
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(tod_views.render_tod_modal(
                self.config.state_root, eid, tok,
                flash=qs.get("flash"),
                flash_level=qs.get("flash_level", "info"),
            ))
            return

        if path == "/audit":
            qs = urllib.parse.parse_qs(parsed.query)
            tab = (qs.get("tab") or ["stream"])[0]
            fwd = ["tab=approvals" if tab == "approvals" else "tab=live"]
            for k in ("window", "session", "verdict", "flash", "flash_level"):
                v = (qs.get(k) or [""])[0]
                if v:
                    fwd.append(f"{k}={urllib.parse.quote(v)}")
            self._redirect("/courtyard?" + "&".join(fwd))
            return
        if path == "/graph":
            qs = urllib.parse.parse_qs(parsed.query)
            fwd = ["tab=graph"]
            for k in ("window", "session"):
                v = (qs.get(k) or [""])[0]
                if v:
                    fwd.append(f"{k}={urllib.parse.quote(v)}")
            self._redirect("/courtyard?" + "&".join(fwd))
            return
        if path.startswith("/rules/"):
            rid = urllib.parse.unquote(path[len("/rules/"):])
            page = views.render_rule_detail(
                self.config.state_root, rid, tok,
            )
            if page is None:
                self._send_html("<h1>Rule not found</h1>", code=404)
            else:
                self._send_html(page)
            return
        # Forge-v1 surfaces
        if path == "/engagement":
            from . import forge_v1_views
            self._send_html(
                forge_v1_views.render_engagement(self.config.state_root, tok)
            )
            return
        if path == "/weapons":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(
                forge_v1_views.render_weapons(
                    self.config.state_root, tok,
                    flash=qs.get("flash"),
                )
            )
            return
        if path.startswith("/weapons/"):
            wid = urllib.parse.unquote(
                path[len("/weapons/"):]
            ).strip("/")
            if wid:
                from . import forge_v1_views
                self._send_html(forge_v1_views.render_weapon_detail(
                    self.config.state_root, wid, tok,
                ))
                return
        if path == "/engagement/activations":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(forge_v1_views.render_activations(
                self.config.state_root, tok,
                reason_filter=qs.get("reason"),
                chunk_filter=qs.get("chunk"),
            ))
            return
        if path == "/engagement/events":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(forge_v1_views.render_events(
                self.config.state_root, tok,
                channel_filter=qs.get("channel"),
                status_filter=qs.get("status"),
                activation_filter=qs.get("activation"),
            ))
            return
        if path == "/engagement/evidence":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(forge_v1_views.render_evidence(
                self.config.state_root, tok,
                source_filter=qs.get("source"),
                activation_filter=qs.get("activation"),
            ))
            return
        if path == "/engagement/spoils":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(forge_v1_views.render_spoils(
                self.config.state_root, tok, flash=qs.get("flash"),
                scope=qs.get("scope", "active"),
                session=qs.get("session"),
            ))
            return
        if path == "/engagement/findings":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(forge_v1_views.render_findings(
                self.config.state_root, tok,
                status_filter=qs.get("status"),
                flash=qs.get("flash"),
            ))
            return
        if path.startswith("/engagement/evidence/"):
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            tail = urllib.parse.unquote(
                path[len("/engagement/evidence/"):]).strip("/")
            if tail.endswith("/raw"):
                eid = tail[:-len("/raw")]
                sd, binder, rec = forge_v1_views._evidence_binder_for(
                    self.config.state_root, eid, qs.get("session"))
                if rec is None:
                    self._send_plain("evidence not found", code=404)
                    return
                try:
                    raw = binder.get(eid) or b""
                except Exception:  # noqa: BLE001
                    raw = b""
                ct = str(getattr(rec, "content_type", "")
                         or "application/octet-stream")
                self._send_bytes(raw, content_type=ct,
                                 filename=eid.replace(":", "_") + ".bin")
                return
            if tail:
                self._send_html(forge_v1_views.render_evidence_detail(
                    self.config.state_root, tail, tok,
                    session_id=qs.get("session"), flash=qs.get("flash"),
                ))
                return
        if path.startswith("/engagement/field/"):
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            tail = urllib.parse.unquote(
                path[len("/engagement/field/"):]).strip("/")
            if tail:
                self._send_html(forge_v1_views.render_field_detail(
                    self.config.state_root, tail, tok,
                    session_id=qs.get("session"), flash=qs.get("flash"),
                ))
                return
        if path.startswith("/engagement/findings/"):
            fid = urllib.parse.unquote(
                path[len("/engagement/findings/"):]
            ).strip("/")
            if fid:
                from . import forge_v1_views
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                self._send_html(forge_v1_views.render_finding_detail(
                    self.config.state_root, fid, tok,
                    flash=qs.get("flash"),
                ))
                return
        if path == "/engagement/leads":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(forge_v1_views.render_leads(
                self.config.state_root, tok,
                status_filter=qs.get("status"),
                flash=qs.get("flash"),
            ))
            return
        if path.startswith("/engagement/leads/"):
            lid = urllib.parse.unquote(
                path[len("/engagement/leads/"):]
            ).strip("/")
            if lid:
                from . import forge_v1_views
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                self._send_html(forge_v1_views.render_lead_detail(
                    self.config.state_root, lid, tok,
                    flash=qs.get("flash"),
                ))
                return
        if path == "/engagement/anomalies":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(forge_v1_views.render_anomalies(
                self.config.state_root, tok,
                flash=qs.get("flash"),
            ))
            return
        if path.startswith("/engagement/anomalies/"):
            aid = urllib.parse.unquote(
                path[len("/engagement/anomalies/"):]
            ).strip("/")
            if aid:
                from . import forge_v1_views
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                self._send_html(forge_v1_views.render_anomaly_detail(
                    self.config.state_root, aid, tok,
                    flash=qs.get("flash"),
                ))
                return
        if path == "/forge":
            from . import forge_v1_views
            self._send_html(
                forge_v1_views.render_forge(self.config.state_root, tok)
            )
            return
        if path == "/forge/packs":
            from . import forge_v1_views
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            self._send_html(
                forge_v1_views.render_packs(
                    self.config.state_root, tok, flash=qs.get("flash", ""),
                )
            )
            return
        if path.startswith("/forge/packs/"):
            pname = urllib.parse.unquote(
                path[len("/forge/packs/"):]
            ).strip("/")
            if pname:
                from . import forge_v1_views
                self._send_html(forge_v1_views.render_pack_detail(
                    self.config.state_root, pname, tok,
                ))
                return
        if path.startswith("/forge/") and path.endswith("/proposal"):
            fid = urllib.parse.unquote(
                path[len("/forge/"):-len("/proposal")]
            ).strip("/")
            if fid:
                from . import forge_v1_views
                self._send_html(forge_v1_views.render_proposal_detail(
                    self.config.state_root, fid, tok,
                ))
                return
        if path.startswith("/forge/") and path.endswith("/audit"):
            fid = urllib.parse.unquote(
                path[len("/forge/"):-len("/audit")]
            ).strip("/")
            if fid:
                from . import forge_v1_views
                self._send_html(forge_v1_views.render_forge_audit(
                    self.config.state_root, fid, tok,
                ))
                return
        if path.startswith("/forge/"):
            fid = urllib.parse.unquote(path[len("/forge/"):]).strip("/")
            if fid:
                from . import forge_v1_views
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                self._send_html(forge_v1_views.render_forge_detail(
                    self.config.state_root, fid, tok,
                    flash=qs.get("flash"),
                ))
                return

        if path == "/docs":
            self._send_html(views.render_docs_index(tok))
            return
        if path.startswith("/docs/"):
            slug = urllib.parse.unquote(path[len("/docs/"):]).strip("/")
            page = views.render_docs_topic(tok, slug)
            if page is None:
                self._send_html("<h1>Doc topic not found</h1>", code=404)
            else:
                self._send_html(page)
            return
        if path == "/shadows":
            # Folded into /rhema as the Shadows tab. Preserve filters
            # + flash query params so the redirect keeps the view intact.
            qs = urllib.parse.parse_qs(parsed.query)
            fwd = ["tab=shadows"]
            for k in ("channel", "status", "rule", "sort", "flash", "flash_level"):
                v = (qs.get(k) or [""])[0]
                if v:
                    fwd.append(f"{k}={urllib.parse.quote(v)}")
            self._redirect("/rhema?" + "&".join(fwd))
            return
        # Detail deep-links live at /sessions/<id> (canonical). Legacy
        # /courtyard/<id> (pre-merge) + /gauntlet/<id> + /openclaw/<id>
        # all 302 to the canonical session detail URL.
        if path.startswith("/courtyard/"):
            sid = urllib.parse.unquote(path[len("/courtyard/"):]).strip("/")
            target = f"/sessions/{sid}"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if path == "/openclaw" or path.startswith("/openclaw/"):
            if path == "/openclaw":
                target = "/courtyard"
            else:
                target = path.replace("/openclaw", "/sessions", 1)
            if parsed.query:
                target = f"{target}?{parsed.query}"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if path == "/gauntlet" or path.startswith("/gauntlet/"):
            if path == "/gauntlet":
                target = "/courtyard"
            else:
                target = path.replace("/gauntlet", "/sessions", 1)
            if parsed.query:
                target = f"{target}?{parsed.query}"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        # /sessions/<id>/export.sig — detached HMAC-SHA256 signature of
        # the export.tgz bytes. Lets a verifier confirm the tarball
        # they received matches what the Armory produced without having
        # to re-fetch.
        if path.startswith("/sessions/") and path.endswith("/export.sig"):
            sid = path[len("/sessions/"):-len("/export.sig")]
            blob = build_session_export(self.config.state_root, sid)
            if blob is None:
                self._send_html("<h1>Session not found</h1>", code=404)
                return
            sig = build_session_export_signature(blob, self.config.token)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{sid}-export.sig"',
            )
            self.send_header("Content-Length", str(len(sig)))
            self.end_headers()
            self.wfile.write(sig)
            return

        # /sessions/<id>/export.tgz — bundle the session's JSONL streams
        # and containment manifest into a single gzipped tar. Useful for
        # offline post-mortem; the download is self-contained.
        if path.startswith("/sessions/") and path.endswith("/export.tgz"):
            sid = path[len("/sessions/"):-len("/export.tgz")]
            blob = build_session_export(self.config.state_root, sid)
            if blob is None:
                self._send_html("<h1>Session not found</h1>", code=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{sid}-export.tgz"',
            )
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return

        if path.startswith("/sessions/"):
            sid = path.split("/", 2)[2]
            sqs = dict(urllib.parse.parse_qsl(parsed.query))
            page = views.render_session(
                self.config.state_root, sid, tok,
                flash=sqs.get("flash"),
                flash_level=sqs.get("flash_level", "info"),
                tab=sqs.get("tab", "activity"),
            )
            if page is None:
                self._send_html("<h1>Not found</h1>", code=404)
            else:
                self._send_html(page)
            return
        self._send_html("<h1>Not found</h1>", code=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._body()

        # /login is auth-free POST — it IS the authentication step.
        if path == "/login":
            submitted = str(body.get("password") or "")
            target = str(body.get("next") or "/") or "/"
            role = self._try_login(submitted)
            if role is None:
                self._send_html(
                    self._login_form_html(
                        next_url=target, error="Wrong token. Try again.",
                    ),
                    code=401,
                )
                return
            self._set_token_cookie_and_redirect(submitted, target)
            return

        # /logout clears the cookie and bounces to /login.
        if path == "/logout":
            self._clear_cookie_and_redirect("/login")
            return

        role = self._auth_role()
        if role is None:
            self._unauthorized()
            return
        if role != "admin":
            self._send_plain(
                "forbidden: read-only token cannot mutate state",
                code=403,
            )
            return

        if path == "/engagement/start":
            ok, msg = _start_task_session(self.config.state_root, body)
            lvl = "ok" if ok else "bad"
            self._redirect(
                f"/engagement?flash={urllib.parse.quote(msg)}"
                f"&flash_level={lvl}"
            )
            return

        if path == "/engagement/polish":
            ok, msg = _start_polish_task(self.config.state_root, body)
            lvl = "ok" if ok else "bad"
            itype = str(body.get("item_type") or "").strip()
            iid = str(body.get("item_id") or "").strip()
            _sub = {"finding": "findings", "lead": "leads"}.get(itype)
            if _sub and iid:
                back = f"/engagement/{_sub}/{urllib.parse.quote(iid)}"
            else:
                back = "/engagement/spoils"
            self._redirect(
                f"{back}?flash={urllib.parse.quote(msg)}&flash_level={lvl}"
            )
            return

        if path == "/engagement/stop":
            sid = str(body.get("session_id") or "").strip()
            killed = _stop_task_session(self.config.state_root, sid or None)
            flash = "Task stopped" if killed else "Task session not running"
            lvl = "ok" if killed else "bad"
            self._redirect(
                f"/engagement?flash={urllib.parse.quote(flash)}"
                f"&flash_level={lvl}"
            )
            return

        # Spoils page: operator field edits + file attach
        if path.startswith("/engagement/spoils/"):
            from . import spoils_actions
            from urllib.parse import quote as _q
            tail = path[len("/engagement/spoils/"):]
            if tail.startswith("lead/") and tail.endswith("/edit"):
                lid = tail[len("lead/"):-len("/edit")]
                ok, msg = spoils_actions.edit_lead(
                    self.config.state_root, lid, body)
            elif tail.endswith("/edit"):
                fid = tail[:-len("/edit")]
                ok, msg = spoils_actions.edit_finding(
                    self.config.state_root, fid, body)
            elif tail.endswith("/attach"):
                fid = tail[:-len("/attach")]
                mb = self._multipart_body()
                data = mb.get("file") or b""
                if isinstance(data, str):
                    data = data.encode()
                ok, msg = spoils_actions.attach_file(
                    self.config.state_root, fid,
                    filename=str(mb.get("file_filename") or "attachment"),
                    data=data if isinstance(data, bytes) else b"",
                    summary=str(mb.get("summary") or ""))
            else:
                ok, msg = False, "unknown spoils action"
            self._redirect(
                f"/engagement/spoils?t={_q(self.config.token)}&flash={_q(msg)}")
            return

        # Forge-v1 finding sign-off / rescind / queue / return
        if path.startswith("/engagement/findings/") and any(
            path.endswith(s) for s in
            ("/sign_off", "/rescind", "/queue_for_review", "/return_to_draft")
        ):
            from . import forge_v1_views
            from urllib.parse import quote as _q
            tail = path[len("/engagement/findings/"):]
            if tail.endswith("/sign_off"):
                fid = tail[:-len("/sign_off")]
                ok, msg = forge_v1_views.handle_finding_sign_off(
                    self.config.state_root, fid, body.get("operator_id"),
                )
            elif tail.endswith("/rescind"):
                fid = tail[:-len("/rescind")]
                ok, msg = forge_v1_views.handle_finding_rescind(
                    self.config.state_root, fid,
                    str(body.get("reason") or "rescinded"),
                )
            elif tail.endswith("/queue_for_review"):
                fid = tail[:-len("/queue_for_review")]
                ok, msg = forge_v1_views.handle_finding_queue_for_review(
                    self.config.state_root, fid,
                    note=body.get("note"),
                )
            else:
                fid = tail[:-len("/return_to_draft")]
                ok, msg = forge_v1_views.handle_finding_return_to_draft(
                    self.config.state_root, fid,
                    note=body.get("note"),
                )
            self._redirect(
                f"/engagement/findings/{_q(fid)}"
                f"?t={_q(self.config.token)}&flash={_q(msg)}"
            )
            return

        # Lead actions (investigate / touch / promote / dismiss)
        if path.startswith("/engagement/leads/"):
            from . import forge_v1_views
            from urllib.parse import quote as _q
            tail = path[len("/engagement/leads/"):]
            for action in ("investigate", "touch", "promote", "dismiss"):
                suffix = "/" + action
                if tail.endswith(suffix):
                    lid = tail[:-len(suffix)]
                    ok, msg = forge_v1_views.handle_lead_action(
                        self.config.state_root, lid, action,
                        reason=str(body.get("reason") or "")
                        if action == "dismiss" else None,
                    )
                    self._redirect(
                        f"/engagement/leads"
                        f"?t={_q(self.config.token)}&flash={_q(msg)}"
                    )
                    return

        # Anomaly promote / dismiss
        if path.startswith("/engagement/anomalies/"):
            from . import forge_v1_views
            from urllib.parse import quote as _q
            tail = path[len("/engagement/anomalies/"):]
            if tail.endswith("/promote"):
                aid = tail[:-len("/promote")]
                ok, msg = forge_v1_views.handle_anomaly_promote(
                    self.config.state_root, aid, str(body.get("title") or ""),
                )
                self._redirect(
                    f"/engagement/anomalies"
                    f"?t={_q(self.config.token)}&flash={_q(msg)}"
                )
                return
            if tail.endswith("/dismiss"):
                aid = tail[:-len("/dismiss")]
                ok, msg = forge_v1_views.handle_anomaly_dismiss(
                    self.config.state_root, aid,
                    str(body.get("reason") or "dismissed via armory"),
                )
                self._redirect(
                    f"/engagement/anomalies"
                    f"?t={_q(self.config.token)}&flash={_q(msg)}"
                )
                return

        # Forge-v1 web actions
        if path == "/forge/create":
            from . import forge_actions
            from urllib.parse import quote as _q
            ok, msg, fid = forge_actions.handle_forge_create(
                self.config.state_root, body,
            )
            dest = f"/forge/{_q(fid)}" if ok and fid else "/forge"
            self._redirect(
                f"{dest}?t={_q(self.config.token)}&flash={_q(msg)}"
            )
            return

        if path.startswith("/forge/"):
            from . import forge_actions
            from urllib.parse import quote as _q
            tail = path[len("/forge/"):]
            action_map = {
                "/mats/remove": forge_actions.handle_forge_remove_mat,
                "/deposit/finalize": forge_actions.handle_forge_finalize_deposit,
                "/manifest/run": forge_actions.handle_forge_run_manifest,
                "/manifest/approve": forge_actions.handle_forge_approve_manifest,
                "/manifest/drop": forge_actions.handle_forge_drop_element,
                "/manifest/undrop": forge_actions.handle_forge_undrop_element,
                "/exhaust/run": forge_actions.handle_forge_run_exhaust,
                "/exhaust/fallback": forge_actions.handle_forge_pack_fallback,
                "/exhaust/auto-extract": forge_actions.handle_forge_auto_extract,
                "/exhaust/advance-review": forge_actions.handle_forge_advance_review,
                "/synthesize": forge_actions.handle_forge_synthesize,
                "/reconcile": forge_actions.handle_forge_reconcile,
                "/workflow/derive": forge_actions.handle_forge_workflow_derive,
                "/pack/curate": forge_actions.handle_forge_pack_curate,
                "/seal": forge_actions.handle_forge_seal,
                "/abort": forge_actions.handle_forge_abort,
            }
            # pack promote: /forge/packs/<name>/promote → redirect to pack list
            if tail.startswith("packs/") and tail.endswith("/promote"):
                pname = urllib.parse.unquote(
                    tail[len("packs/"):-len("/promote")]
                ).strip("/")
                ok, msg = forge_actions.handle_forge_pack_promote(
                    self.config.state_root, pname,
                )
                self._redirect(
                    f"/forge/packs?t={_q(self.config.token)}&flash={_q(msg)}"
                )
                return
            if tail.endswith("/mats/upload"):
                fid = urllib.parse.unquote(
                    tail[:-len("/mats/upload")]
                ).strip("/")
                ok, msg = forge_actions.handle_forge_upload_mat(
                    self.config.state_root, fid, self._multipart_body(),
                )
                self._redirect(
                    f"/forge/{_q(fid)}?t={_q(self.config.token)}"
                    f"&flash={_q(msg)}"
                )
                return
            for suffix, handler in action_map.items():
                if tail.endswith(suffix):
                    fid = urllib.parse.unquote(
                        tail[:-len(suffix)]
                    ).strip("/")
                    ok, msg = handler(self.config.state_root, fid, body)
                    self._redirect(
                        f"/forge/{_q(fid)}?t={_q(self.config.token)}"
                        f"&flash={_q(msg)}"
                    )
                    return

        # Forge-v1 reforge proposal approve
        if path.startswith("/forge/") and path.endswith("/proposal/approve"):
            from . import forge_v1_views
            from urllib.parse import quote as _q
            tail = path[len("/forge/"):-len("/proposal/approve")]
            fid = urllib.parse.unquote(tail).strip("/")
            ok, msg = forge_v1_views.handle_proposal_approve(
                self.config.state_root, fid,
            )
            self._redirect(
                f"/forge/{_q(fid)}?t={_q(self.config.token)}"
                f"&flash={_q(msg)}"
            )
            return

        # Forge-v1 weapon equip/unequip
        if path == "/weapons/equip":
            from . import forge_v1_views
            wid = str(body.get("weapon_id") or "")
            ok, msg = forge_v1_views.handle_equip(self.config.state_root, wid)
            self._redirect(
                f"/weapons?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}"
            )
            return
        if path == "/weapons/unequip":
            from . import forge_v1_views
            ok, msg = forge_v1_views.handle_unequip(self.config.state_root)
            self._redirect(
                f"/weapons?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}"
            )
            return
        if (
            path.startswith("/weapons/")
            and "/resources/" in path
            and path.endswith("/request-url")
        ):
            from .. import resources as resources_mod
            rest = path[len("/weapons/"):]
            weapon_id, _, tail = rest.partition("/resources/")
            weapon_id = urllib.parse.unquote(weapon_id).strip("/")
            resource_id = urllib.parse.unquote(
                tail[:-len("/request-url")]
            ).strip("/")
            wpath = self.config.state_root / "weapons" / weapon_id / "weapon.json"
            resource = None
            if wpath.is_file():
                try:
                    data = json.loads(wpath.read_text())
                    for item in data.get("resources") or []:
                        if (
                            isinstance(item, dict)
                            and str(item.get("id") or "") == resource_id
                        ):
                            resource = item
                            break
                except Exception:
                    resource = None
            if resource is None:
                self._redirect(
                    f"/weapons/{urllib.parse.quote(weapon_id)}"
                    f"?t={urllib.parse.quote(self.config.token)}"
                    "&flash=resource+not+found"
                )
                return
            resources_mod.write_resource_url_approval(
                self.config.state_root / "sessions" / "resource-resolution",
                resource,
                suggested_url=(body.get("suggested_url") or "").strip(),
                weapon_id=weapon_id,
            )
            self._redirect(
                f"/courtyard?tab=approvals&t={urllib.parse.quote(self.config.token)}"
                "&flash=resource+approval+queued"
            )
            return

        # /sessions/<id>/verify-audit — walk the policy_hits.jsonl hash chain.
        if path.startswith("/sessions/") and path.endswith("/verify-audit"):
            sid = path[len("/sessions/"):-len("/verify-audit")]
            hits = self.config.state_root / "sessions" / sid / "policy_hits.jsonl"
            ok, lineno, err = verify_chain(hits)
            msg = (f"Audit OK ({lineno} lines verified)" if ok
                   else f"Audit FAIL line {lineno}: {err}")
            lvl = "ok" if ok else "bad"
            self._redirect(
                f"/sessions/{urllib.parse.quote(sid)}"
                f"?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}&flash_level={lvl}"
            )
            return

        # /sessions/<id>/baseline — record a baseline from this session.
        if path.startswith("/sessions/") and path.endswith("/baseline"):
            sid = path[len("/sessions/"):-len("/baseline")]
            session_dir = self.config.state_root / "sessions" / sid
            name = (body.get("name") or "").strip()
            if not session_dir.is_dir():
                self._redirect(
                    f"/?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote('Session not found')}&flash_level=bad"
                )
                return
            if not name:
                self._redirect(
                    f"/sessions/{urllib.parse.quote(sid)}"
                    f"?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote('Baseline name required')}&flash_level=bad"
                )
                return
            try:
                b = baseline_mod.record_from_session(
                    self.config.state_root, name, session_dir,
                )
            except Exception as exc:
                self._redirect(
                    f"/sessions/{urllib.parse.quote(sid)}"
                    f"?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote(f'Baseline failed: {exc}')}"
                    "&flash_level=bad"
                )
                return
            msg = (f"Recorded baseline {b.name!r}: "
                   f"{len(b.paths)} paths, {len(b.commands)} commands, "
                   f"{len(b.hosts)} hosts")
            self._redirect(
                f"/sessions/{urllib.parse.quote(sid)}"
                f"?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}&flash_level=ok"
            )
            return

        # /baselines/record — record from latest session.
        if path == "/baselines/record":
            name = (body.get("name") or "").strip()
            sessions_dir = self.config.state_root / "sessions"
            entries = [p for p in sessions_dir.iterdir() if p.is_dir()] if sessions_dir.is_dir() else []
            if not entries:
                self._redirect(
                    f"/?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote('No sessions to baseline')}&flash_level=bad"
                )
                return
            latest = max(entries, key=lambda p: p.stat().st_mtime)
            try:
                b = baseline_mod.record_from_session(
                    self.config.state_root, name, latest,
                )
            except Exception as exc:
                self._redirect(
                    f"/?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote(f'Baseline failed: {exc}')}"
                    "&flash_level=bad"
                )
                return
            self._redirect(
                f"/?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(f'Recorded {b.name!r}')}&flash_level=ok"
            )
            return

        # /baselines/<name>/to-armor
        if path.startswith("/baselines/") and path.endswith("/to-armor"):
            bname = path[len("/baselines/"):-len("/to-armor")]
            armor = (body.get("armor") or "").strip()
            if not armor:
                self._redirect(
                    f"/?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote('Armor name required')}&flash_level=bad"
                )
                return
            try:
                summary = baseline_mod.to_armor(
                    self.config.state_root, bname, armor,
                )
            except Exception as exc:
                self._redirect(
                    f"/?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote(f'to-armor failed: {exc}')}"
                    "&flash_level=bad"
                )
                return
            self._redirect(
                f"/armors/{urllib.parse.quote(summary['armor'])}"
                f"?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(f'Created armor from baseline {bname!r}')}"
                "&flash_level=ok"
            )
            return

        # /baselines/<name>/delete
        if path.startswith("/baselines/") and path.endswith("/delete"):
            bname = path[len("/baselines/"):-len("/delete")]
            p = baseline_mod.path_for(self.config.state_root, bname)
            if p.exists():
                p.unlink()
                msg = f"Deleted baseline {bname!r}"
                lvl = "ok"
            else:
                msg = f"Baseline {bname!r} not found"
                lvl = "bad"
            self._redirect(
                f"/?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}&flash_level={lvl}"
            )
            return

        # /sessions/<id>/kill — SIGTERM the supervisor pid for this session.
        if path.startswith("/sessions/") and path.endswith("/kill"):
            sid = path[len("/sessions/"):-len("/kill")]
            killed = _kill_session(self.config.state_root, sid)
            flash = "Session terminated" if killed else "Session not running"
            lvl = "ok" if killed else "bad"
            self._redirect(
                f"/sessions/{urllib.parse.quote(sid)}"
                f"?flash={urllib.parse.quote(flash)}&flash_level={lvl}"
            )
            return

        # /sessions/<id>/delete — remove an archived session's directory.
        # Refuses if the pid is still live; operator must /kill first.
        if path.startswith("/sessions/") and path.endswith("/delete"):
            sid = path[len("/sessions/"):-len("/delete")]
            ok, msg = _delete_archived_session(self.config.state_root, sid)
            if ok:
                self._redirect(
                    f"/courtyard?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote(msg)}&flash_level=ok"
                )
            else:
                self._redirect(
                    f"/sessions/{urllib.parse.quote(sid)}"
                    f"?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote(msg)}&flash_level=bad"
                )
            return

        # /sessions/cleanup — bulk delete archived sessions older than N days.
        if path == "/sessions/cleanup":
            try:
                days = float(body.get("days") or "30")
            except (TypeError, ValueError):
                days = 30.0
            removed, skipped = _cleanup_archived_sessions(
                self.config.state_root, max_age_days=days,
            )
            msg = (
                f"Removed {removed} archived session(s) older than "
                f"{days:g} days"
                + (f"; skipped {skipped} still-running" if skipped else "")
            )
            self._redirect(
                f"/?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}&flash_level=ok"
            )
            return

        # /danger/bulwark — operator toggle: add/remove a flag's bulwark deny.
        if path == "/danger/bulwark":
            from .. import danger_registry as _dr
            op = str(body.get("op") or "")
            target = str(body.get("target") or "")
            add = str(body.get("add") or "") == "1"
            armor = armors_mod.active_armor_name(self.config.state_root)
            state = _dr.set_bulwark(
                self.config.state_root, op=op, target=target, add=add,
                armor_id=armor,
            )
            verb = "added to" if state else "removed from"
            msg = f"{target} {verb} armor bulwark"
            self._redirect(
                f"/danger?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}&flash_level=ok"
            )
            return

        # /settings/approvals — toggle timeout auto-grant + grace period.
        if path == "/settings/approvals":
            from .. import approvals as _approvals_mod
            settings = _approvals_mod.read_settings(self.config.state_root)
            settings["autogrant"] = bool(body.get("autogrant"))
            raw = str(body.get("autogrant_timeout_s") or "").strip()
            try:
                v = float(raw)
                if v > 0:
                    settings["autogrant_timeout_s"] = int(v)
            except (TypeError, ValueError):
                pass
            _approvals_mod.write_settings(self.config.state_root, settings)
            state = "enabled" if settings["autogrant"] else "disabled"
            grace = settings.get("autogrant_timeout_s", 15)
            msg = f"Approval auto-grant {state} (grace {grace}s)"
            self._redirect(
                f"/settings?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}&flash_level=ok"
            )
            return

        # /armors/import — accept a raw gzipped tarball body (no multipart).
        # Name and overwrite come from the query string so the body is pure.
        if path == "/armors/import":
            qs = dict(urllib.parse.parse_qsl(parsed.query))
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 50 * 1024 * 1024:
                self._redirect(
                    f"/armors?flash={urllib.parse.quote('Upload missing or too large')}"
                    "&flash_level=bad"
                )
                return
            blob = self.rfile.read(length)
            try:
                armor = armors_mod.import_armor(
                    self.config.state_root, blob,
                    rename_to=(qs.get("name") or None),
                    overwrite=(qs.get("overwrite") == "1"),
                )
            except (ValueError, FileExistsError, FileNotFoundError) as exc:
                self._redirect(
                    f"/armors?flash={urllib.parse.quote(f'Import failed: {exc}')}"
                    "&flash_level=bad"
                )
                return
            self._redirect(
                f"/armors/{urllib.parse.quote(armor.name)}"
                f"?flash={urllib.parse.quote('Imported')}&flash_level=ok"
            )
            return

        # /approvals/<id>/respond
        if path.startswith("/approvals/") and path.endswith("/respond"):
            approval_id = path[len("/approvals/"):-len("/respond")]
            action = body.get("action", "deny")
            resolved_url = (body.get("resolved_url") or "").strip() or None
            ttl_s: float | None = None
            ttl_raw = (body.get("ttl") or "").strip()
            if ttl_raw and action in ("allow-always", "deny-always"):
                try:
                    ttl_s = overrides_mod.parse_ttl(ttl_raw)
                except ValueError:
                    ttl_s = None
            for s in (self.config.state_root / "sessions").iterdir():
                rec = approvals_mod.read_pending(s, approval_id)
                if rec is not None:
                    resolved = approvals_mod.resolve(
                        s,
                        approval_id,
                        action,
                        ttl_s=ttl_s,
                        resolved_url=resolved_url,
                    )
                    if (
                        resolved is not None
                        and resolved.op == "resource.resolve_url"
                        and resolved.status == "allowed"
                    ):
                        from .. import resources as resources_mod
                        resources_mod.apply_resource_resolution_to_matching_weapons(
                            self.config.state_root,
                            resolved,
                        )
                    break
            self._redirect("/approvals")
            return

        if path == "/bulwark/add-canary":
            # Combined flow: seed a canary file AND auto-append a
            # reply-layer deny rule that fires if the canary value is
            # ever echoed back in an LLM response.
            from .. import canary as canary_mod
            kind = (body.get("kind") or "generic").strip()
            target = (body.get("path") or "").strip()
            note = (body.get("note") or "").strip() or None
            if not target:
                self._redirect("/bulwark?flash=path+required&flash_level=bad")
                return
            try:
                c = canary_mod.add(
                    self.config.state_root, Path(target),
                    kind=kind, note=note,
                )
            except FileExistsError as exc:
                self._redirect(
                    "/bulwark?flash="
                    + urllib.parse.quote(f"path already exists: {exc}")
                    + "&flash_level=bad"
                )
                return
            except Exception as exc:
                self._redirect(
                    "/bulwark?flash=" + urllib.parse.quote(str(exc))
                    + "&flash_level=bad"
                )
                return
            # Append the auto-watch rule. Escape regex metachars in the
            # value so the bulwark regex matcher treats it as a literal.
            import re as _re
            escaped = _re.escape(c.value)
            rule_line = (
                f"deny reply regex:{escaped}  # canary:{c.id}\n"
            )
            bw_path = self.config.state_root / "bulwark.md"
            existing = bw_path.read_text() if bw_path.exists() else ""
            header_re = _re.compile(r"^##\s+global\s*$", _re.MULTILINE)
            if header_re.search(existing):
                new_text = header_re.sub(
                    lambda m: m.group(0) + "\n" + rule_line.rstrip(),
                    existing, count=1,
                )
            else:
                sep = "" if not existing or existing.endswith("\n") else "\n"
                new_text = existing + sep + f"\n## global\n{rule_line}"
            bw_path.parent.mkdir(parents=True, exist_ok=True)
            bw_path.write_text(new_text)
            self._redirect(
                "/bulwark?flash="
                + urllib.parse.quote(
                    f"seeded {c.id} ({c.kind}) at {c.path} + reply rule added"
                )
                + "&flash_level=ok"
            )
            return

        # /canaries/add + /canaries/remove retired — the standalone
        # /canaries page is gone. Seeding happens through the Bulwark
        # guided builder (/bulwark/add-canary). Keep a 303 in case any
        # stale form action hits these.
        if path == "/canaries/add" or path == "/canaries/remove":
            self._redirect("/bulwark")
            return

        if path == "/bulwark/add-guided":
            kind = (body.get("scope_kind") or "global").strip()
            if kind == "global":
                scope = "global"
            elif kind == "armor":
                nm = (body.get("scope_armor") or "").strip()
                if not nm:
                    self._redirect("/bulwark?flash=pick+an+armor&flash_level=bad")
                    return
                scope = f"armor: {nm}"
            else:
                scope = (body.get("scope_custom") or "").strip()
                if not scope:
                    self._redirect("/bulwark?flash=custom+scope+required&flash_level=bad")
                    return

            action = (body.get("action") or "deny").strip()
            layer = (body.get("layer") or "").strip()
            match = (body.get("match") or "").strip()
            if not layer or not match:
                self._redirect("/bulwark?flash=layer+and+match+required&flash_level=bad")
                return
            if body.get("is_regex"):
                match = "regex:" + match

            mods = []
            ttl = (body.get("mod_ttl") or "").strip()
            if ttl:
                mods.append(f"ttl {ttl}")
            count = (body.get("mod_count") or "").strip()
            if count:
                mods.append(f"x{count}")
            then = (body.get("mod_then") or "").strip()
            if then:
                mods.append(f"then:{then}")
            if body.get("mod_highlight"):
                mods.append("highlight")

            mod_str = (" " + " ".join(mods)) if mods else ""
            note = (body.get("note") or "").strip()
            rule = f"{action}{mod_str} {layer} {match}"
            if note:
                rule += f"  # {note}"

            # Reuse the same file-write + validation flow as /bulwark/add
            # by re-dispatching the body.
            body["scope"] = scope
            body["rule"] = rule
            path = "/bulwark/add"  # fall through

        if path == "/bulwark/add":
            scope = (body.get("scope") or "").strip()
            rule = (body.get("rule") or "").strip()
            if not scope or not rule:
                self._redirect("/bulwark?flash=scope+and+rule+required&flash_level=bad")
                return
            bw_path = self.config.state_root / "bulwark.md"
            existing = bw_path.read_text() if bw_path.exists() else ""
            # Append the rule under the requested scope header. If the
            # header already exists, add to it; otherwise create a new
            # section at end of file.
            import re as _re
            header_re = _re.compile(
                r"^##\s+" + _re.escape(scope) + r"\s*$", _re.MULTILINE
            )
            if header_re.search(existing):
                # Insert the rule right after the matching header.
                new_text = header_re.sub(
                    lambda m: m.group(0) + "\n" + rule, existing, count=1,
                )
            else:
                sep = "" if existing.endswith("\n") or not existing else "\n"
                new_text = existing + sep + f"\n## {scope}\n{rule}\n"
            # Validate before saving so we don't corrupt the file.
            try:
                from .. import bulwark as _bw
                test = _bw.parse(new_text)
                if test.parse_errors:
                    self._redirect(
                        "/bulwark?flash=" + urllib.parse.quote(
                            "parse error: " + test.parse_errors[0]
                        ) + "&flash_level=bad"
                    )
                    return
            except Exception as exc:
                self._redirect(
                    "/bulwark?flash=" + urllib.parse.quote(str(exc)) + "&flash_level=bad"
                )
                return
            bw_path.parent.mkdir(parents=True, exist_ok=True)
            bw_path.write_text(new_text)
            self._redirect("/bulwark?flash=rule+added&flash_level=ok")
            return

        if path in (
            "/overrides/add", "/overrides/remove",
            "/blackholes/add", "/blackholes/remove",
        ):
            self._redirect(
                "/bulwark?flash=" + urllib.parse.quote(
                    "Overrides and blackholes are now unified in Bulwark. "
                    "Add rules here instead."
                ) + "&flash_level=info"
            )
            return

        if path.startswith("/armors/") and path.endswith("/equip"):
            name = path[len("/armors/"):-len("/equip")]
            armors_mod.equip_armor(self.config.state_root, name)
            self._redirect("/armors")
            return

        if path == "/armors/new":
            name = (body.get("name") or "").strip()
            description = body.get("description") or ""
            sigil = body.get("sigil") or "shield"
            if not name:
                self._redirect(
                    f"/armors/new?flash={urllib.parse.quote('Name is required')}"
                )
                return
            try:
                armors_mod.create_armor(
                    self.config.state_root, name,
                    description=description, sigil=sigil,
                )
            except (ValueError, FileExistsError) as exc:
                self._redirect(
                    f"/armors/new?flash={urllib.parse.quote(str(exc))}"
                )
                return
            self._redirect(f"/armors/{urllib.parse.quote(name)}")
            return

        # ---- Trims POST handlers ----------------------------------------
        if path == "/trims/new":
            from . import trim_views
            target = trim_views.handle_trim_new(
                self.config.state_root, body, self.config.token,
            )
            self._redirect(target)
            return
        if path == "/trims/derive":
            from . import trim_views
            target = trim_views.handle_trim_derive(
                self.config.state_root, body, self.config.token,
            )
            self._redirect(target)
            return
        if path.startswith("/trims/") and path.endswith("/apply"):
            from . import trim_views
            tail = path[len("/trims/"):-len("/apply")]
            if "/" in tail:
                armor_seg, _, trim_id = tail.partition("/")
                self._redirect(trim_views.handle_trim_apply(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(trim_id), self.config.token,
                ))
                return
        if path.startswith("/trims/") and path.endswith("/release"):
            from . import trim_views
            tail = path[len("/trims/"):-len("/release")]
            if "/" in tail:
                armor_seg, _, trim_id = tail.partition("/")
                self._redirect(trim_views.handle_trim_release(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(trim_id), self.config.token,
                ))
                return
        if path.startswith("/trims/") and path.endswith("/delete"):
            from . import trim_views
            tail = path[len("/trims/"):-len("/delete")]
            if "/" in tail:
                armor_seg, _, trim_id = tail.partition("/")
                self._redirect(trim_views.handle_trim_delete(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(trim_id), self.config.token,
                ))
                return

        # ---- Temper POST handlers ---------------------------------------
        if path == "/temper/baseline/install":
            from . import temper_views
            self._redirect(temper_views.handle_temper_install_baseline(
                self.config.state_root, self.config.token,
            ))
            return
        if path == "/temper/tailor/start":
            from . import temper_views
            self._redirect(temper_views.handle_temper_tailor_start(
                self.config.state_root, body, self.config.token,
            ))
            return
        if path == "/temper/tailor/finish":
            from . import temper_views
            self._redirect(temper_views.handle_temper_tailor_finish(
                self.config.state_root, body, self.config.token,
            ))
            return
        if path == "/temper/tailor/abort":
            from . import temper_views
            self._redirect(temper_views.handle_temper_tailor_abort(
                self.config.state_root, self.config.token,
            ))
            return
        if path == "/temper/create":
            from . import temper_views
            target = temper_views.handle_temper_create(
                self.config.state_root, body, self.config.token,
            )
            self._redirect(target)
            return
        if path.startswith("/tempers/") and (
            path.endswith("/equip") or path.endswith("/unequip")
        ):
            from . import temper_views
            verb = "unequip" if path.endswith("/unequip") else "equip"
            temper_id = urllib.parse.unquote(
                path[len("/tempers/"):-len("/" + verb)])
            handler = (
                temper_views.handle_temper_unequip if verb == "unequip"
                else temper_views.handle_temper_equip
            )
            self._redirect(handler(
                self.config.state_root, temper_id, self.config.token))
            return
        if path.startswith("/temper/") and path.endswith("/ingest-selected"):
            from . import temper_views
            tail = path[len("/temper/"):-len("/ingest-selected")]
            if "/" in tail:
                armor_seg, _, proposal_id = tail.partition("/")
                self._redirect(temper_views.handle_temper_ingest_selected(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(proposal_id),
                    body,
                    self.config.token,
                ))
                return
        if path.startswith("/temper/") and path.endswith("/seal"):
            from . import temper_views
            tail = path[len("/temper/"):-len("/seal")]
            if "/" in tail:
                armor_seg, _, proposal_id = tail.partition("/")
                self._redirect(temper_views.handle_temper_seal(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(proposal_id),
                    self.config.token,
                ))
                return
        if path.startswith("/temper/") and path.endswith("/reject"):
            from . import temper_views
            tail = path[len("/temper/"):-len("/reject")]
            if "/" in tail:
                armor_seg, _, proposal_id = tail.partition("/")
                self._redirect(temper_views.handle_temper_reject(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(proposal_id),
                    self.config.token,
                ))
                return
        if path.startswith("/temper/") and path.endswith("/apply"):
            from . import temper_views
            tail = path[len("/temper/"):-len("/apply")]
            if "/" in tail:
                armor_seg, _, proposal_id = tail.partition("/")
                self._redirect(temper_views.handle_temper_apply(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(proposal_id),
                    self.config.token,
                    body,
                ))
                return
        if path.startswith("/temper/") and path.endswith("/activate"):
            from . import temper_views
            tail = path[len("/temper/"):-len("/activate")]
            if "/" in tail:
                armor_seg, _, proposal_id = tail.partition("/")
                self._redirect(temper_views.handle_temper_activate(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(proposal_id),
                    self.config.token,
                ))
                return
        if path.startswith("/temper/") and path.endswith("/release"):
            from . import temper_views
            tail = path[len("/temper/"):-len("/release")]
            if "/" in tail:
                armor_seg, _, proposal_id = tail.partition("/")
                self._redirect(temper_views.handle_temper_release(
                    self.config.state_root,
                    urllib.parse.unquote(armor_seg),
                    urllib.parse.unquote(proposal_id),
                    self.config.token,
                ))
                return

        # ---- Tools (classification registry) POST -----------------------
        if path.startswith("/tools/") and path.endswith("/approve"):
            from . import tool_views
            key = urllib.parse.unquote(path[len("/tools/"):-len("/approve")])
            self._redirect(tool_views.handle_tool_approve(
                self.config.state_root, key, self.config.token,
            ))
            return
        if path.startswith("/tools/") and path.endswith("/override"):
            from . import tool_views
            key = urllib.parse.unquote(path[len("/tools/"):-len("/override")])
            self._redirect(tool_views.handle_tool_override(
                self.config.state_root, key, body, self.config.token,
            ))
            return
        if path.startswith("/tools/") and path.endswith("/reclassify"):
            from . import tool_views
            key = urllib.parse.unquote(path[len("/tools/"):-len("/reclassify")])
            self._redirect(tool_views.handle_tool_reclassify(
                self.config.state_root, key, self.config.token,
            ))
            return
        if path.startswith("/tools/") and path.endswith("/delete"):
            from . import tool_views
            key = urllib.parse.unquote(path[len("/tools/"):-len("/delete")])
            self._redirect(tool_views.handle_tool_delete(
                self.config.state_root, key, self.config.token,
            ))
            return

        # ---- Mats POST ---------------------------------------------------
        if path.startswith("/mats/") and path.endswith("/upload"):
            from . import mats_views
            eid = urllib.parse.unquote(path[len("/mats/"):-len("/upload")])
            self._redirect(mats_views.handle_mats_upload(
                self.config.state_root, eid, self._multipart_body(),
                self.config.token,
            ))
            return
        if path.startswith("/mats/") and path.endswith("/paste"):
            from . import mats_views
            eid = urllib.parse.unquote(path[len("/mats/"):-len("/paste")])
            self._redirect(mats_views.handle_mats_paste(
                self.config.state_root, eid, body, self.config.token,
            ))
            return
        if path.startswith("/mats/") and path.endswith("/rescan"):
            from . import mats_views
            eid = urllib.parse.unquote(path[len("/mats/"):-len("/rescan")])
            self._redirect(mats_views.handle_mats_rescan(
                self.config.state_root, eid, self.config.token,
            ))
            return
        if (
            path.startswith("/mats/")
            and (path.endswith("/include") or path.endswith("/exclude"))
        ):
            from . import mats_views
            selected = path.endswith("/include")
            suffix = "/include" if selected else "/exclude"
            tail = path[len("/mats/"):-len(suffix)]
            if "/" in tail:
                eid_seg, _, relpath = tail.partition("/")
                self._redirect(mats_views.handle_mats_selection(
                    self.config.state_root,
                    urllib.parse.unquote(eid_seg),
                    urllib.parse.unquote(relpath),
                    self.config.token,
                    selected=selected,
                ))
                return
        if path.startswith("/mats/") and path.endswith("/delete"):
            from . import mats_views
            tail = path[len("/mats/"):-len("/delete")]
            if "/" in tail:
                eid_seg, _, relpath = tail.partition("/")
                self._redirect(mats_views.handle_mats_delete(
                    self.config.state_root,
                    urllib.parse.unquote(eid_seg),
                    urllib.parse.unquote(relpath),
                    self.config.token,
                ))
                return

        # ---- TOD modal POST ---------------------------------------------
        if path.startswith("/tod/") and path.endswith("/grant"):
            from . import tod_views
            eid = urllib.parse.unquote(path[len("/tod/"):-len("/grant")])
            self._redirect(tod_views.handle_tod_grant(
                self.config.state_root, eid, body, self.config.token,
            ))
            return

        if path.startswith("/armors/") and path.endswith("/clone"):
            name = path[len("/armors/"):-len("/clone")]
            dst = (body.get("dst") or "").strip()
            if not dst:
                self._redirect(
                    f"/armors/{urllib.parse.quote(name)}"
                    f"?flash={urllib.parse.quote('Clone destination required')}"
                    "&flash_level=bad"
                )
                return
            try:
                armors_mod.clone_armor(self.config.state_root, name, dst)
            except (ValueError, FileNotFoundError, FileExistsError) as exc:
                self._redirect(
                    f"/armors/{urllib.parse.quote(name)}"
                    f"?flash={urllib.parse.quote(str(exc))}&flash_level=bad"
                )
                return
            self._redirect(f"/armors/{urllib.parse.quote(dst)}")
            return

        if path.startswith("/armors/") and path.endswith("/delete"):
            name = path[len("/armors/"):-len("/delete")]
            ok = armors_mod.delete_armor(self.config.state_root, name)
            if not ok:
                self._redirect(
                    f"/armors/{urllib.parse.quote(name)}"
                    f"?flash={urllib.parse.quote('Delete failed')}&flash_level=bad"
                )
                return
            self._redirect(
                f"/armors?flash={urllib.parse.quote(f'Deleted {name}')}&flash_level=ok"
            )
            return

        for suffix in ("/overrides/add", "/blackholes/add",
                       "/overrides/remove", "/blackholes/remove"):
            if path.startswith("/armors/") and path.endswith(suffix):
                name = path[len("/armors/"):-len(suffix)]
                armor = armors_mod.load_armor(self.config.state_root, name)
                if armor is None:
                    self._redirect(
                        f"/armors?flash={urllib.parse.quote('Armor not found')}"
                        "&flash_level=bad"
                    )
                    return
                store_path = armor.overrides_path
                back = f"/armors/{urllib.parse.quote(name)}"
                if suffix.endswith("/add"):
                    kind = body.get("kind", "path")
                    pattern = (body.get("pattern") or "").strip()
                    reason = body.get("reason") or None
                    if pattern:
                        if "overrides" in suffix:
                            overrides_mod.add_override_at(
                                store_path, kind=kind, pattern=pattern,
                                reason=reason,
                            )
                        else:
                            overrides_mod.add_blackhole_at(
                                store_path, kind=kind, pattern=pattern,
                                reason=reason,
                            )
                        self._redirect(
                            f"{back}?flash={urllib.parse.quote('Entry added')}"
                            "&flash_level=ok"
                        )
                    else:
                        self._redirect(
                            f"{back}?flash={urllib.parse.quote('Pattern required')}"
                            "&flash_level=bad"
                        )
                    return
                # /remove
                entry_id = (body.get("id") or "").strip()
                if entry_id:
                    overrides_mod.remove_at(store_path, entry_id)
                self._redirect(back)
                return

        if path.startswith("/armors/") and path.endswith("/channels"):
            name = path[len("/armors/"):-len("/channels")]
            channels = self._body_multi("channels")
            ok = armors_mod.set_channels(
                self.config.state_root, name, channels,
            )
            if not ok:
                self._redirect(
                    f"/armors/{urllib.parse.quote(name)}"
                    f"?flash={urllib.parse.quote('Armor not found')}&flash_level=bad"
                )
                return
            label = ", ".join(channels) if channels else "global"
            self._redirect(
                f"/armors/{urllib.parse.quote(name)}"
                f"?flash={urllib.parse.quote(f'Channels: {label}')}&flash_level=ok"
            )
            return

        if path.startswith("/armors/") and path.endswith("/meta"):
            name = path[len("/armors/"):-len("/meta")]
            tags_raw = body.get("tags") or ""
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            shadow_raw = body.get("shadow_mode")
            updated = armors_mod.update_armor_meta(
                self.config.state_root, name,
                description=body.get("description"),
                sigil=body.get("sigil") or None,
                tags=tags,
                shadow_mode=bool(shadow_raw) and shadow_raw != "0",
            )
            if updated is None:
                self._redirect(
                    f"/armors/{urllib.parse.quote(name)}"
                    f"?flash={urllib.parse.quote('Not found')}&flash_level=bad"
                )
                return
            self._redirect(
                f"/armors/{urllib.parse.quote(name)}"
                f"?flash={urllib.parse.quote('Metadata saved')}&flash_level=ok"
            )
            return

        if path == "/rhema/add" or (
            path.startswith("/rhema/") and path.endswith("/remove")
        ):
            self._redirect(
                "/bulwark?flash=" + urllib.parse.quote(
                    "Rhema rules are now managed in Bulwark (ask/reply/oc "
                    "layers). Use the guided builder below."
                ) + "&flash_level=info"
            )
            return

        # /shadows/promote — flip a shadow variant into an enforcing
        # rewrite. Body param: rule_id. If rule_id starts with "abs-live-"
        # the armor's absolute_enforce flag is flipped; otherwise the
        # operator rhema rule's action goes shadow_rewrite → rewrite.
        if path == "/shadows/promote":
            rid = str(body.get("rule_id") or "").strip()
            if not rid:
                self._redirect(
                    f"/shadows?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote('rule_id required')}&flash_level=bad"
                )
                return
            if rid.startswith("abs-live-"):
                armor_name = rid[len("abs-live-"):]
                ok = armors_mod.set_absolute_enforce(
                    self.config.state_root, armor_name, True,
                )
                msg = (
                    f"ABSOLUTE.md for armor {armor_name!r} now ENFORCING on "
                    "live requests" if ok else
                    f"Armor {armor_name!r} not found"
                )
                self._redirect(
                    f"/shadows?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote(msg)}"
                    f"&flash_level={'ok' if ok else 'bad'}"
                )
                return
            from .. import rhema as rhema_mod
            rules = rhema_mod.load(self.config.state_root)
            found = False
            for r in rules:
                if r.id == rid and r.action == "shadow_rewrite":
                    r.action = "rewrite"
                    found = True
                    break
            if found:
                rhema_mod.save(self.config.state_root, rules)
                msg = f"Rule {rid} promoted to enforcing rewrite"
            else:
                msg = f"Rule {rid} not found or not a shadow"
            self._redirect(
                f"/shadows?t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote(msg)}"
                f"&flash_level={'ok' if found else 'bad'}"
            )
            return

        # /absolute/append-section — append one section to the target
        # armor's active absolute file. Called via fetch() from the
        # section editor's per-row "Copy to…" control. Returns plain
        # text so the client can report success without a page reload.
        if path == "/absolute/append-section":
            target_armor = str(body.get("armor") or "").strip()
            section_title = str(body.get("title") or "").strip()
            section_body = str(body.get("body") or "")
            if not target_armor:
                self._send_plain("armor required", code=400)
                return
            ok, msg = append_absolute_section(
                self.config.state_root, target_armor,
                section_title, section_body,
            )
            if ok:
                self._send_plain(msg)
            else:
                code = 404 if "not found" in msg else 500
                self._send_plain(msg, code=code)
            return

        if path == "/absolute/save":
            active = armors_mod.active_armor_name(self.config.state_root)
            armor = armors_mod.load_armor(self.config.state_root, active) if active else None
            if armor is None:
                self._redirect(
                    f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote('No armor equipped')}&flash_level=bad"
                )
                return
            target = armor.absolute_path
            contents = body.get("contents", "")
            import os as _os, tempfile
            try:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", delete=False, dir=str(target.parent),
                    prefix=target.name + ".", suffix=".tmp",
                )
                tmp.write(contents); tmp.flush(); _os.fsync(tmp.fileno()); tmp.close()
                _os.replace(tmp.name, target)
            except OSError as exc:
                self._redirect(
                    f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                    f"&edit=1&flash={urllib.parse.quote(f'Save failed: {exc}')}"
                    "&flash_level=bad"
                )
                return
            self._redirect(
                f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                f"&flash={urllib.parse.quote('ABSOLUTE saved')}&flash_level=ok"
            )
            return

        # Per-absolute hot-swap routes: save / activate / new / rename / delete
        # Format: /armors/<armor>/absolutes/<name>/save|activate|rename|delete
        #         /armors/<armor>/absolutes/new
        if path.startswith("/armors/") and "/absolutes/" in path:
            import os as _os, tempfile
            rest = path[len("/armors/"):]
            parts = rest.split("/")
            # parts: [armor, "absolutes", <name>, <action>] OR [armor, "absolutes", "new"]
            if len(parts) >= 3 and parts[1] == "absolutes":
                armor_name = parts[0]
                armor = armors_mod.load_armor(self.config.state_root, armor_name)
                if armor is None:
                    self._send_html("<h1>Armor not found</h1>", code=404)
                    return
                if parts[2] == "new":
                    name = (body.get("name") or "").strip()
                    if not name:
                        self._redirect(
                            f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                            f"&armor={urllib.parse.quote(armor_name)}&edit=1"
                            f"&flash={urllib.parse.quote('Name required')}"
                            "&flash_level=bad"
                        )
                        return
                    armors_mod.create_absolute(armor, name, "")
                    self._redirect(
                        f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                        f"&armor={urllib.parse.quote(armor_name)}"
                        f"&absolute={urllib.parse.quote(name)}&edit=1"
                        f"&flash={urllib.parse.quote('Created ' + name)}"
                        "&flash_level=ok"
                    )
                    return
                if len(parts) >= 4:
                    abs_name = parts[2]
                    action = parts[3]
                    if action == "save":
                        target = armors_mod.path_for_absolute(armor, abs_name)
                        contents = body.get("contents", "")
                        try:
                            tmp = tempfile.NamedTemporaryFile(
                                mode="w", delete=False,
                                dir=str(target.parent),
                                prefix=target.name + ".",
                                suffix=".tmp",
                            )
                            tmp.write(contents); tmp.flush()
                            _os.fsync(tmp.fileno()); tmp.close()
                            target.parent.mkdir(parents=True, exist_ok=True)
                            _os.replace(tmp.name, target)
                        except OSError as exc:
                            self._redirect(
                                f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                                f"&armor={urllib.parse.quote(armor_name)}"
                                f"&absolute={urllib.parse.quote(abs_name)}&edit=1"
                                f"&flash={urllib.parse.quote('Save failed: ' + str(exc))}"
                                "&flash_level=bad"
                            )
                            return
                        self._redirect(
                            f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                            f"&armor={urllib.parse.quote(armor_name)}"
                            f"&absolute={urllib.parse.quote(abs_name)}"
                            f"&flash={urllib.parse.quote('Saved ' + abs_name)}"
                            "&flash_level=ok"
                        )
                        return
                    if action == "activate":
                        ok = armors_mod.set_active_absolute(armor, abs_name)
                        self._redirect(
                            f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                            f"&armor={urllib.parse.quote(armor_name)}"
                            f"&absolute={urllib.parse.quote(abs_name)}"
                            f"&flash={urllib.parse.quote(('Activated ' if ok else 'Activate failed: ') + abs_name)}"
                            f"&flash_level={'ok' if ok else 'bad'}"
                        )
                        return
                    if action == "rename":
                        new_name = (body.get("new_name") or "").strip()
                        ok = armors_mod.rename_absolute(armor, abs_name, new_name)
                        self._redirect(
                            f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                            f"&armor={urllib.parse.quote(armor_name)}"
                            f"&absolute={urllib.parse.quote(new_name if ok else abs_name)}"
                            f"&edit=1"
                            f"&flash={urllib.parse.quote(('Renamed to ' + new_name) if ok else 'Rename failed')}"
                            f"&flash_level={'ok' if ok else 'bad'}"
                        )
                        return
                    if action == "delete":
                        ok = armors_mod.delete_absolute(armor, abs_name)
                        self._redirect(
                            f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                            f"&armor={urllib.parse.quote(armor_name)}"
                            f"&flash={urllib.parse.quote(('Deleted ' + abs_name) if ok else 'Delete failed (keep at least one absolute)')}"
                            f"&flash_level={'ok' if ok else 'bad'}"
                        )
                        return
                    if action == "duplicate":
                        new_name = (body.get("new_name") or "").strip()
                        ok = armors_mod.duplicate_absolute(
                            armor, abs_name, new_name,
                        )
                        self._redirect(
                            f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                            f"&armor={urllib.parse.quote(armor_name)}"
                            f"&absolute={urllib.parse.quote(new_name if ok else abs_name)}"
                            f"&flash={urllib.parse.quote(('Duplicated to ' + new_name) if ok else 'Duplicate failed')}"
                            f"&flash_level={'ok' if ok else 'bad'}"
                        )
                        return

        if path == "/absolute/sync-shadow":
            # Deprecated as of Fix 1. ABSOLUTE.md is read live by the MITM
            # every turn; there is no separate "sync" step and no managed
            # Rhema rule to upsert. Legacy bookmarks / POSTs land here and
            # get redirected back with a flash explaining the change.
            # ``contents`` from the form is dropped silently — Save still
            # writes the file (via /absolute/save or the per-armor route).
            from .. import rhema as _rhema
            try:
                _rhema.purge_managed(self.config.state_root, "abs-sync")
            except Exception:  # noqa: BLE001
                pass
            msg = (
                "Sync-as-shadow_rewrite is gone — ABSOLUTE.md is read live "
                "by the MITM each turn. See /shadows for variants."
            )
            self._redirect(
                f"/rhema?tab=envelope&t={urllib.parse.quote(self.config.token)}"
                f"&edit=1&flash={urllib.parse.quote(msg)}&flash_level=ok"
            )
            return

        if path == "/walls/save":
            target_raw = body.get("target", "")
            contents = body.get("contents", "")
            # Hard guard: only write to paths we'd have rendered ourselves.
            allowed, label = views._resolve_walls_source(
                self.config.state_root, self.config.walls_file,
            )
            if not allowed or str(allowed) != target_raw:
                msg = (
                    f"Refused: would only save to {allowed}"
                    if allowed else "Refused: no WALLS target configured"
                )
                self._redirect(
                    f"/walls?flash={urllib.parse.quote(msg)}&flash_level=bad"
                )
                return
            # Atomic write + fsync to preserve supervisor hot-reload semantics.
            import os as _os, tempfile
            try:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", delete=False,
                    dir=str(allowed.parent),
                    prefix=allowed.name + ".",
                    suffix=".tmp",
                )
                tmp.write(contents)
                tmp.flush()
                _os.fsync(tmp.fileno())
                tmp.close()
                _os.replace(tmp.name, allowed)
            except OSError as exc:
                self._redirect(
                    f"/walls?edit=1&flash=Save+failed:+{urllib.parse.quote(str(exc))}"
                    "&flash_level=bad"
                )
                return
            self._redirect(
                "/walls?flash=Saved.+Supervisor+hot-reload+will+pick+it+up"
                "&flash_level=ok"
            )
            return

        # /armors/<name>/walls/save  and  /armors/<name>/absolute/save
        for kind, suffix in (("walls", "/walls/save"), ("absolute", "/absolute/save")):
            if path.startswith("/armors/") and path.endswith(suffix):
                armor_name = path[len("/armors/"):-len(suffix)]
                armor = armors_mod.load_armor(self.config.state_root, armor_name)
                if armor is None:
                    self._send_html("<h1>Armor not found</h1>", code=404)
                    return
                target = armor.walls_path if kind == "walls" else armor.absolute_path
                contents = body.get("contents", "")
                import os as _os, tempfile
                try:
                    tmp = tempfile.NamedTemporaryFile(
                        mode="w", delete=False,
                        dir=str(target.parent),
                        prefix=target.name + ".",
                        suffix=".tmp",
                    )
                    tmp.write(contents)
                    tmp.flush()
                    _os.fsync(tmp.fileno())
                    tmp.close()
                    _os.replace(tmp.name, target)
                except OSError as exc:
                    back = (
                        f"/armors/{urllib.parse.quote(armor_name)}"
                        f"?t={urllib.parse.quote(self.config.token)}"
                        f"&{kind}_edit=1"
                        f"&flash={urllib.parse.quote(f'Save failed: {exc}')}"
                        "&flash_level=bad"
                    )
                    self._redirect(back)
                    return
                msg = f"{kind.upper()} saved"
                self._redirect(
                    f"/armors/{urllib.parse.quote(armor_name)}"
                    f"?t={urllib.parse.quote(self.config.token)}"
                    f"&flash={urllib.parse.quote(msg)}&flash_level=ok"
                )
                return

        self._send_html("<h1>Not found</h1>", code=404)


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_armory(
    state_root: Path,
    walls_file: Path | None = None,
    bind: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    behind_proxy: bool = False,
) -> None:
    token = ensure_token(state_root)
    viewer_token = ensure_viewer_token(state_root)
    tls_enabled = bool(tls_cert and tls_key)

    # MCP-Scan sidecar: if ``ARA_MCP_SCAN_PROXY_PORT`` is set and
    # mcp-scan is installed, spin it up alongside the armory. Findings
    # land in events.jsonl as ``mcp_scan_finding`` events and surface
    # on /audit. Opt-in — ARA doesn't auto-configure the agent's MCP
    # client (too environment-specific).
    _mcp_handle = None
    try:
        from .. import mcp_scan as _mcp_scan
        _mcp_port_raw = os.environ.get("ARA_MCP_SCAN_PROXY_PORT")
        if _mcp_port_raw and _mcp_scan.is_available():
            try:
                _mcp_port = int(_mcp_port_raw)
            except ValueError:
                _mcp_port = 0
            if _mcp_port > 0:
                _events_path = state_root / "events.jsonl"

                def _sink(ev: dict) -> None:
                    ev.setdefault("ts", __import__("time").time())
                    with open(_events_path, "a", encoding="utf-8") as f:
                        f.write(__import__("json").dumps(ev) + "\n")

                _mcp_handle = _mcp_scan.launch_proxy_sidecar(
                    _mcp_port, state_root, event_sink=_sink,
                )
                if _mcp_handle is not None:
                    sys.stderr.write(
                        f"[armory] mcp-scan proxy sidecar listening on "
                        f"port {_mcp_port} (log: {_mcp_handle.log_path})\n"
                    )
    except Exception as _exc:  # noqa: BLE001
        sys.stderr.write(f"[armory] mcp-scan sidecar skipped: {_exc}\n")

    # Auto-migrate legacy policy files to bulwark.md on first boot after the
    # Bulwark rollout. No-op if bulwark.md already exists or no legacy files
    # are present. Safe and idempotent.
    try:
        from .. import bulwark_migrate
        _rep = bulwark_migrate.migrate(state_root)
        if _rep.wrote_bulwark:
            sys.stderr.write(
                f"[armory] migrated legacy policy files → "
                f"{_rep.bulwark_path} ({_rep.rule_count} rules)\n"
            )
            for _src, _bak in _rep.renamed:
                sys.stderr.write(f"[armory]   renamed {_src} → {_bak}\n")
        for _w in _rep.warnings:
            sys.stderr.write(f"[armory]   migration warning: {_w}\n")
    except Exception as _exc:  # don't fail boot on migration hiccup
        sys.stderr.write(f"[armory] bulwark auto-migrate skipped: {_exc}\n")

    # Skill change detection: index ~/.openclaw/skills + openclaw's
    # dist/extensions on each armory boot. New skills or modifications
    # surface as ``skill_changed`` events on /audit (verdict=flag).
    try:
        from .. import skill_watch
        _snap, _changes = skill_watch.scan(state_root)
        if _changes:
            _events_path = state_root / "events.jsonl"
            import json as _json
            with open(_events_path, "a", encoding="utf-8") as _f:
                for _ev in skill_watch.changes_to_events(_changes):
                    _f.write(_json.dumps(_ev) + "\n")
            sys.stderr.write(
                f"[armory] skill-watch detected {len(_changes)} change(s) — "
                f"see /audit?verdict=flag\n"
            )
        else:
            sys.stderr.write(
                f"[armory] skill-watch: {len(_snap.entries)} entries "
                f"indexed, no changes\n"
            )
    except Exception as _exc:  # noqa: BLE001
        sys.stderr.write(f"[armory] skill-watch skipped: {_exc}\n")

    # Seed the default canary set (operator.admin-token shape at
    # ~/.openclaw-ara-canaries/paired-stub.json). No-op if any canary
    # already exists — respects operator curation.
    try:
        from .. import canary as _canary
        _seeded = _canary.seed_defaults(state_root)
        for _c in _seeded:
            sys.stderr.write(
                f"[armory] seeded default canary {_c.id} ({_c.kind}) "
                f"at {_c.path}\n"
            )
    except Exception as _exc:  # noqa: BLE001
        sys.stderr.write(f"[armory] default canary seed skipped: {_exc}\n")

    # Start the exec-approvals enforcing proxy. It listens at
    # <state_root>/exec-approvals-tap.sock, parses openclaw's JSONL
    # frames, consults Bulwark exec rules, and forwards allow-path
    # requests to openclaw's real ~/.clawdbot/exec-approvals.sock.
    # Contained workers are auto-pointed at the tap socket by
    # ara.containment (see _wire_exec_approvals_redirect). Non-
    # contained workers aren't covered — we warn about that below.
    try:
        from .. import exec_approvals_proxy as _eap
        _eap_handle = _eap.start(state_root)
        if _eap_handle is not None:
            sys.stderr.write(
                f"[armory] exec-approvals proxy listening at "
                f"{_eap_handle.listen_path} (forwards to {_eap.DEFAULT_REAL_SOCKET})\n"
            )
            # Warn if non-contained workers might be running — they
            # bypass the proxy entirely. Detection is heuristic
            # (we don't track live worker PIDs centrally here).
            if _eap.looks_unsupervised(state_root):
                sys.stderr.write(
                    "[armory] HINT: launch contained workers with "
                    "`ara openclaw --contain` to route exec-approvals "
                    "through ARA. Non-contained workers bypass the proxy.\n"
                )
    except Exception as _exc:  # noqa: BLE001
        sys.stderr.write(f"[armory] exec-approvals proxy skipped: {_exc}\n")

    # Gateway WebSocket tap — ALWAYS starts. Contained workers are
    # auto-pointed at it by ara.containment (openclaw.json gateway.port
    # rewrite). The tap parses WebSocket + ACP frames and emits
    # gateway_acp events per parsed frame.
    try:
        from .. import gateway_ws_tap as _ws_tap
        from .. import gateway_check as _gw_check
        _upstream_port = _gw_check.discover_gateway_port()
        _ws_handle = _ws_tap.start(
            state_root,
            listen_port=0,  # ephemeral
            upstream_host="127.0.0.1",
            upstream_port=_upstream_port,
        )
        if _ws_handle is not None:
            # Persist the resolved listen port so containment can
            # rewrite the worker's openclaw.json gateway.port.
            (state_root / "gateway-ws-tap-port").write_text(
                str(_ws_handle.listen_port)
            )
            sys.stderr.write(
                f"[armory] gateway ws tap listening on 127.0.0.1:"
                f"{_ws_handle.listen_port} → forwards to "
                f"127.0.0.1:{_upstream_port}\n"
            )
    except Exception as _exc:  # noqa: BLE001
        sys.stderr.write(f"[armory] gateway ws tap skipped: {_exc}\n")

    # Hermes containment guard — periodic reconcile: detect any uncontained
    # ("rogue") hermes, force-kill it, relaunch contained; circuit-break on
    # repeats (see ara.hermes_guard). Disable with ARA_HERMES_GUARD_DISABLE=1.
    try:
        if os.environ.get("ARA_HERMES_GUARD_DISABLE") != "1":
            start_hermes_guard_thread(state_root)
            sys.stderr.write("[armory] hermes containment guard started\n")
    except Exception as _exc:  # noqa: BLE001
        sys.stderr.write(f"[armory] hermes guard skipped: {_exc}\n")

    config = ArmoryConfig(
        state_root=state_root, walls_file=walls_file, token=token,
        viewer_token=viewer_token,
        bind=bind, port=port,
        tls=tls_enabled, behind_proxy=behind_proxy,
    )

    handler = _Handler
    handler.config = config  # type: ignore[assignment]

    server = _ThreadingServer((bind, port), handler)
    if tls_enabled:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(tls_cert), keyfile=str(tls_key))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    scheme = "https" if tls_enabled else "http"
    qtok = urllib.parse.quote(token)
    sys.stderr.write(
        f"[armory] serving {scheme}://{bind}:{port}\n"
        f"[armory] token file: {state_root / 'armory-token'}\n"
        f"[armory] viewer (read-only) token file: "
        f"{state_root / 'armory-viewer-token'}\n"
    )
    if behind_proxy:
        sys.stderr.write(
            "[armory] trusting X-Forwarded-Proto/Host/For headers "
            "(--behind-proxy).\n"
        )
    if bind in {"0.0.0.0", "::", ""}:
        sys.stderr.write(
            "[armory] WARNING: bound to all interfaces — the token is the "
            "only auth layer. Ensure network trust matches.\n"
        )
        # Best-effort: report LAN-reachable addresses so the operator has a
        # URL they can actually paste elsewhere.
        try:
            import socket as _socket
            host = _socket.gethostname()
            addrs = sorted({
                info[4][0]
                for info in _socket.getaddrinfo(host, None)
                if info[0] in (_socket.AF_INET, _socket.AF_INET6)
                and not info[4][0].startswith(("127.", "::1"))
            })
            for addr in addrs:
                display = f"[{addr}]" if ":" in addr else addr
                sys.stderr.write(
                    f"[armory] url: {scheme}://{display}:{port}/?t={qtok}\n"
                )
            if not addrs:
                sys.stderr.write(
                    f"[armory] url: {scheme}://{host}:{port}/?t={qtok}\n"
                )
        except Exception:
            sys.stderr.write(
                f"[armory] url: {scheme}://<host>:{port}/?t={qtok}\n"
            )
    else:
        sys.stderr.write(
            f"[armory] url: {scheme}://{bind}:{port}/?t={qtok}\n"
        )
    # ---- verbose-by-default exit diagnostics ----
    # The armory has historically died silently — operator restarts via
    # ara-ctl.sh and the log only shows request lines, never WHY the
    # process exited. The handlers below log:
    #   * SIGTERM / SIGINT / SIGHUP receipt with timestamp
    #   * any uncaught exception during serve_forever (full traceback)
    #   * "ready" marker after server is bound and dispatching
    #   * "stopped" marker on clean exit
    # Set ARA_ARMORY_QUIET=1 to suppress the lifecycle markers
    # (not the exception diagnostics — those always log).
    import signal as _signal
    import time as _time
    import traceback as _traceback
    _quiet = os.environ.get("ARA_ARMORY_QUIET") == "1"
    _started_at = _time.time()
    _exit_reason = {"why": "unknown", "details": ""}

    def _on_signal(signum, _frame):
        try:
            name = _signal.Signals(signum).name
        except (ValueError, AttributeError):
            name = f"signal-{signum}"
        sys.stderr.write(
            f"[armory] received {name} — initiating shutdown "
            f"(uptime={_time.time() - _started_at:.1f}s pid={os.getpid()})\n"
        )
        sys.stderr.flush()
        _exit_reason["why"] = name
        _exit_reason["details"] = "signal handler"
        try:
            server.shutdown()
        except Exception:
            pass

    for _sig in (_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP):
        try:
            _signal.signal(_sig, _on_signal)
        except (OSError, ValueError):
            # SIGHUP not available on Windows; signal in non-main-thread.
            pass

    if not _quiet:
        sys.stderr.write(
            f"[armory] ready pid={os.getpid()} bind={bind} port={port} "
            f"state_root={state_root}\n"
        )
        sys.stderr.flush()

    try:
        server.serve_forever()
        # Reached only if shutdown() was called (signal handler).
        if _exit_reason["why"] == "unknown":
            _exit_reason["why"] = "serve_forever returned"
    except KeyboardInterrupt:
        _exit_reason["why"] = "KeyboardInterrupt"
        sys.stderr.write("[armory] KeyboardInterrupt — shutting down\n")
    except SystemExit as _se:
        _exit_reason["why"] = "SystemExit"
        _exit_reason["details"] = f"code={_se.code}"
        sys.stderr.write(f"[armory] SystemExit code={_se.code}\n")
        raise
    except BaseException as _exc:  # noqa: BLE001
        _exit_reason["why"] = type(_exc).__name__
        _exit_reason["details"] = str(_exc)
        sys.stderr.write(
            f"[armory] FATAL exception in serve_forever — "
            f"{type(_exc).__name__}: {_exc}\n"
        )
        sys.stderr.write(_traceback.format_exc())
        sys.stderr.flush()
        raise
    finally:
        try:
            server.server_close()
        except Exception as _exc:  # noqa: BLE001
            sys.stderr.write(f"[armory] server_close error: {_exc}\n")
        if not _quiet:
            uptime = _time.time() - _started_at
            sys.stderr.write(
                f"[armory] stopped pid={os.getpid()} uptime={uptime:.1f}s "
                f"reason={_exit_reason['why']} "
                f"details={_exit_reason['details']!r}\n"
            )
            sys.stderr.flush()
