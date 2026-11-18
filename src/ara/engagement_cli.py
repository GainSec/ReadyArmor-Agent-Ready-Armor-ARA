"""``ara engagement status`` — read-only operator visibility surface.

Aggregates state from the running supervisor's session directory:

- equipped weapon (from ``<state_root>/equipped_weapon``)
- active session id + dir
- active chunk + activation history
  (from ``chunk_history.jsonl`` + ``activations.jsonl``)
- channel-call counts grouped by channel name
  (from ``events.jsonl`` ``channel.<name>`` audit events)
- finding / lead / anomaly counts (latest-wins per id)

No supervisor RPC needed — everything lives on disk under ``state_root``.
The supervisor's existing audit chain is the single source of truth.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from . import weapons as weapons_mod


def _default_state_root() -> Path:
    env = os.environ.get("ARA_STATE_ROOT")
    return Path(env) if env else Path(".ara")


def install_engagement_subcommand(sub: argparse._SubParsersAction) -> None:
    eng = sub.add_parser(
        "engagement",
        help="forge-v1 engagement status (read-only operator view)",
    )
    eng.add_argument(
        "--state-root", type=Path, default=None,
        help="ARA state dir (defaults to $ARA_STATE_ROOT or ./.ara/)",
    )
    esub = eng.add_subparsers(dest="engagement_cmd", required=True)
    p = esub.add_parser("status", help="show current engagement state")
    p.add_argument(
        "--session", default=None,
        help="session id (defaults to the most recent one in the state root)",
    )
    p.add_argument(
        "--recent", type=int, default=10,
        help="how many recent activations / chunk swaps to surface "
             "(default 10)",
    )
    p.set_defaults(_handler=_cmd_status)

    p = esub.add_parser(
        "preflight",
        help="validate engagement coherence before pointing the agent at it",
    )
    p.add_argument(
        "engagement_id",
        help="engagement_id to validate (matches trim engagement_id)",
    )
    p.add_argument(
        "--armor", default=None,
        help="armor to check the trim against (default: active armor)",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="fail the check on warnings, not just hard errors",
    )
    p.set_defaults(_handler=_cmd_preflight)

    eng.set_defaults(_handler=lambda _args: eng.print_help() or 0)


def run(args: argparse.Namespace) -> int:
    h = getattr(args, "_handler", None)
    if h is None:
        return 1
    return h(args)


def _state(args: argparse.Namespace) -> Path:
    return Path(args.state_root) if args.state_root else _default_state_root()


def _resolve_session_dir(state_root: Path, session_id: str | None) -> Path | None:
    sessions = state_root / "sessions"
    if not sessions.is_dir():
        return None
    if session_id:
        cand = sessions / session_id
        return cand if cand.is_dir() else None
    # Most recent by session activity. Directory mtime alone is not stable
    # enough across filesystems; transcript/events/chunk writes should keep
    # the active session current.
    candidates = [c for c in sessions.iterdir() if c.is_dir()]
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
    if limit is not None and limit > 0:
        return out[-limit:]
    return out


def _summarize_chunk_history(session_dir: Path, recent: int) -> dict:
    rows = _read_jsonl(session_dir / "chunk_history.jsonl")
    if not rows:
        return {"current_chunk": None, "transitions": []}
    # Find last mount/swap/jump/rewind to derive current chunk
    current = None
    for r in reversed(rows):
        kind = r.get("kind")
        if kind in ("initialize", "swap", "jump", "rewind"):
            current = r.get("to_chunk")
            break
    transitions = [
        {
            "ts": r.get("ts"),
            "kind": r.get("kind"),
            "from": r.get("from_chunk"),
            "to": r.get("to_chunk"),
            "tag": r.get("tag"),
            "caller": r.get("caller"),
            "activation_id": r.get("activation_id"),
        }
        for r in rows[-recent:]
    ]
    return {"current_chunk": current, "transitions": transitions}


def _summarize_activations(session_dir: Path, recent: int) -> dict:
    rows = _read_jsonl(session_dir / "activations.jsonl")
    return {
        "count": len(rows),
        "recent": [
            {"ts": r.get("ts"), "activation_id": r.get("activation_id"),
             "reason": r.get("reason"), "chunk_id": r.get("chunk_id")}
            for r in rows[-recent:]
        ],
        "current": (
            (session_dir / "activation.txt").read_text().strip()
            if (session_dir / "activation.txt").is_file() else None
        ),
    }


def _summarize_channel_calls(session_dir: Path) -> dict:
    rows = _read_jsonl(session_dir / "events.jsonl")
    by_channel: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    for r in rows:
        kind = r.get("kind") or ""
        if not kind.startswith("channel."):
            continue
        body = r.get("body") or {}
        ch_name = body.get("channel") or kind[len("channel."):]
        status = body.get("status") or "unknown"
        by_channel[ch_name] += 1
        by_status[f"{ch_name}:{status}"] += 1
    return {
        "total": int(sum(by_channel.values())),
        "by_channel": dict(by_channel),
        "rejections_by_channel": {
            k.split(":", 1)[0]: v for k, v in by_status.items()
            if k.endswith(":rejected")
        },
    }


def _latest_per_id(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        rid = r.get("id")
        if rid:
            out[rid] = r
    return out


def _summarize_findings(session_dir: Path) -> dict:
    rows = _read_jsonl(session_dir / "findings.jsonl")
    latest = _latest_per_id(rows)
    by_status: Counter[str] = Counter(
        (f.get("status") or "unknown") for f in latest.values()
    )
    by_severity: Counter[str] = Counter(
        (f.get("severity_name") or "unknown") for f in latest.values()
    )
    return {
        "total": len(latest),
        "by_status": dict(by_status),
        "by_severity": dict(by_severity),
    }


def _summarize_leads(session_dir: Path) -> dict:
    rows = _read_jsonl(session_dir / "leads.jsonl")
    latest = _latest_per_id(rows)
    by_status: Counter[str] = Counter(
        (ld.get("status") or "unknown") for ld in latest.values()
    )
    by_priority: Counter[str] = Counter(
        (ld.get("priority_operator_override")
         or ld.get("priority_computed") or "unknown")
        for ld in latest.values()
    )
    return {
        "total": len(latest),
        "by_status": dict(by_status),
        "by_priority": dict(by_priority),
    }


def _summarize_anomalies(session_dir: Path) -> dict:
    rows = _read_jsonl(session_dir / "anomalies.jsonl")
    latest = _latest_per_id(rows)
    by_status: Counter[str] = Counter(
        (a.get("status") or "unknown") for a in latest.values()
    )
    pending = sum(1 for a in latest.values() if a.get("status") == "pending")
    return {
        "total": len(latest),
        "pending": pending,
        "by_status": dict(by_status),
    }


def _summarize_facts(session_dir: Path) -> dict:
    rows = _read_jsonl(session_dir / "facts.jsonl")
    return {"total": len(rows)}


def _summarize_evidence(session_dir: Path) -> dict:
    """Count evidence rows from the binder's SQLite index."""
    db = session_dir / "evidence" / "index.db"
    if not db.is_file():
        return {"total": 0}
    import sqlite3
    try:
        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT count(*) FROM evidence").fetchone()[0]
        finally:
            conn.close()
        return {"total": int(n)}
    except sqlite3.DatabaseError:
        return {"total": 0}


# ---------- preflight check ----------

# Severity levels — exit code 1 only when there's at least one FAIL.
# WARN gets surfaced but doesn't block (unless --strict).
_PASS, _WARN, _FAIL = "PASS", "WARN", "FAIL"


def _check_engagement_dir(state: Path, eid: str) -> list[tuple[str, str, str]]:
    from . import engagement_mats as _em
    out: list[tuple[str, str, str]] = []
    edir = _em.engagement_dir(state, eid)
    if not edir.is_dir():
        return [(_FAIL, "engagement dir",
                 f"missing: {edir} — derive a trim or run "
                 f"engagement_mats.ensure_engagement first")]
    out.append((_PASS, "engagement dir", str(edir)))
    if (edir / "intake.json").is_file():
        out.append((_PASS, "intake.json", "present"))
    else:
        out.append((_WARN, "intake.json",
                    "missing — derive_trim writes this; preflight "
                    "still valid if the operator hand-curated meta"))
    return out


def _check_trim(
    state: Path, eid: str, armor_hint: str | None,
) -> tuple[list[tuple[str, str, str]], object]:
    from . import armors as _armors
    from . import trims as _trims

    out: list[tuple[str, str, str]] = []
    armor = armor_hint or _armors.active_armor_name(state)
    if not armor:
        out.append((_FAIL, "active armor",
                    "no armor active and --armor not given"))
        return out, None
    out.append((_PASS, "active armor", armor))

    if _armors.load_armor(state, armor) is None:
        out.append((_FAIL, "armor exists",
                    f"armor {armor!r} not found at {state}"))
        return out, None
    out.append((_PASS, "armor exists", armor))

    # Find the trim under this armor with matching engagement_id
    matching = [
        t for t in _trims.list_trims(state, armor)
        if t.engagement_id == eid
    ]
    if not matching:
        out.append((_FAIL, "trim derived",
                    f"no trim under armor {armor!r} has "
                    f"engagement_id={eid!r} — run "
                    f"`ara trim derive {armor} <engagement.json>`"))
        return out, None
    if len(matching) > 1:
        out.append((_WARN, "trim derived",
                    f"multiple trims claim engagement_id={eid!r}; "
                    f"using first: {matching[0].trim_id}"))
    meta = matching[0]
    out.append((_PASS, "trim derived", meta.trim_id))

    active_tid = _trims.active_trim_id(state, armor)
    if active_tid != meta.trim_id:
        out.append((_FAIL, "trim applied",
                    f"trim {meta.trim_id!r} exists but is not applied "
                    f"(active_trim_id={active_tid!r}) — run "
                    f"`ara trim apply {armor} {meta.trim_id}`"))
        return out, meta
    out.append((_PASS, "trim applied", meta.trim_id))
    return out, meta


def _check_window(meta) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    from datetime import datetime, timezone
    try:
        end = meta.test_window_end
        if not end:
            out.append((_WARN, "test window",
                        "no end set — engagement runs indefinitely"))
            return out
        end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc,
        )
    except (AttributeError, ValueError):
        out.append((_WARN, "test window", "test_window_end unparseable"))
        return out
    now = datetime.now(timezone.utc)
    if end_dt < now:
        out.append((_FAIL, "test window",
                    f"window EXPIRED at {end} — derive a fresh trim"))
    else:
        delta = end_dt - now
        out.append((_PASS, "test window",
                    f"open until {end} ({delta.days}d {delta.seconds // 3600}h left)"))
    return out


def _check_tod(meta) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    tod = list(getattr(meta, "tod_allowed", []) or [])
    if not tod:
        out.append((_PASS, "TOD windows",
                    "unrestricted (no tod_allowed configured)"))
        return out
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    for i, w in enumerate(tod):
        days = w.get("days") or []
        bad_days = [d for d in days if d not in valid_days]
        if bad_days:
            out.append((_WARN, f"TOD #{i}",
                        f"unknown day(s) {bad_days}"))
        if not (w.get("start") and w.get("end")):
            out.append((_WARN, f"TOD #{i}",
                        "missing start/end"))
    if all(s == _PASS for s, _, _ in out) or not out:
        out.append((_PASS, "TOD windows", f"{len(tod)} configured"))
    return out


def _check_rate_limits(meta) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    rl = dict(getattr(meta, "rate_limits", {}) or {})
    if not rl:
        out.append((_PASS, "rate limits",
                    "unrestricted (no rate_limits configured)"))
        return out
    gl = rl.get("global_rps")
    if gl is not None and gl < 0:
        out.append((_FAIL, "rate limits", f"negative global_rps={gl!r}"))
    overflow = rl.get("overflow", "queue")
    if overflow not in ("queue", "deny"):
        out.append((_WARN, "rate limits",
                    f"unknown overflow={overflow!r} (expected queue|deny)"))
    out.append((_PASS, "rate limits",
                f"global={gl}, per_host={len(rl.get('per_host') or {})}, "
                f"overflow={overflow}"))
    return out


def _check_tokens(state: Path, meta) -> list[tuple[str, str, str]]:
    """Verify declared_tokens match the env/posture/activity combo per spec §13."""
    out: list[tuple[str, str, str]] = []
    declared = set(getattr(meta, "declared_tokens", []) or [])
    env = getattr(meta, "env", "nonprod")
    posture = getattr(meta, "posture", "blackbox")
    active = bool(getattr(meta, "active_testing", False))

    expected: set[str] = set()
    if env == "prod":
        expected.add("ENV-PROD")
        if active:
            expected.add("ACTIVE-PROD-APPROVAL")
    elif env == "nonprod":
        expected.add("ENV-NONPROD")
    if active:
        expected.add("ACTIVE-TESTING-APPROVAL")
    if posture == "graybox":
        expected.add("POSTURE-GRAYBOX")
    elif posture == "whitebox":
        expected.add("POSTURE-WHITEBOX")

    missing = expected - declared
    if missing:
        out.append((_FAIL, "declared tokens",
                    f"missing for env={env}+active={active}+posture={posture}: "
                    f"{sorted(missing)}"))
    else:
        out.append((_PASS, "declared tokens",
                    f"all expected present ({sorted(expected) or 'none required'})"))

    # Q9 — env=prod with manual DESTRUCTIVE-APPROVAL is contradictory
    if env == "prod" and "DESTRUCTIVE-APPROVAL" in declared:
        out.append((_FAIL, "Q9 contradiction",
                    "DESTRUCTIVE-APPROVAL declared on env=prod — "
                    "use per-action PROD-DESTRUCTIVE-APPROVAL instead"))
    return out


def _check_mats(state: Path, eid: str, meta) -> list[tuple[str, str, str]]:
    from . import engagement_mats as _em
    out: list[tuple[str, str, str]] = []
    posture = getattr(meta, "posture", "blackbox")
    if posture == "blackbox":
        out.append((_PASS, "mats", "blackbox — none expected"))
        return out
    populated = _em.populated_classes(state, eid)
    if not populated:
        out.append((_WARN, "mats",
                    f"posture={posture} but no mat classes populated — "
                    f"upload via wizard or scp+rescan"))
    else:
        out.append((_PASS, "mats",
                    f"posture={posture}, populated: {populated}"))
    return out


def _check_weapon(state: Path, meta) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    weapon_id = getattr(meta, "weapon_ref", None)
    if not weapon_id:
        out.append((_PASS, "weapon", "no weapon_ref linked (optional)"))
        return out
    wdir = state / "weapons" / weapon_id
    if not wdir.is_dir():
        out.append((_FAIL, "weapon",
                    f"weapon_ref={weapon_id!r} not found at {wdir}"))
        return out
    if not (wdir / "seal.json").is_file():
        out.append((_FAIL, "weapon",
                    f"weapon {weapon_id!r} not sealed (missing seal.json)"))
        return out
    if not (wdir / "weapon.json").is_file():
        out.append((_FAIL, "weapon",
                    f"weapon {weapon_id!r} missing weapon.json"))
        return out
    out.append((_PASS, "weapon", f"{weapon_id} sealed at {wdir}"))
    return out


def _cmd_preflight(args: argparse.Namespace) -> int:
    state = _state(args)
    eid = args.engagement_id

    print(f"=== Preflight: engagement {eid} @ {state} ===\n")

    checks: list[tuple[str, str, str]] = []
    checks.extend(_check_engagement_dir(state, eid))
    trim_checks, meta = _check_trim(state, eid, args.armor)
    checks.extend(trim_checks)
    if meta is not None:
        checks.extend(_check_window(meta))
        checks.extend(_check_tod(meta))
        checks.extend(_check_rate_limits(meta))
        checks.extend(_check_tokens(state, meta))
        checks.extend(_check_mats(state, eid, meta))
        checks.extend(_check_weapon(state, meta))

    width = max(len(label) for _, label, _ in checks)
    for severity, label, detail in checks:
        marker = {_PASS: "✓", _WARN: "!", _FAIL: "✗"}[severity]
        print(f"  [{marker}] {severity:<4} {label:<{width}}  {detail}")

    n_fail = sum(1 for s, _, _ in checks if s == _FAIL)
    n_warn = sum(1 for s, _, _ in checks if s == _WARN)
    n_pass = sum(1 for s, _, _ in checks if s == _PASS)
    print()
    print(f"  total: {n_pass} pass, {n_warn} warn, {n_fail} fail")
    if n_fail:
        return 1
    if n_warn and args.strict:
        return 1
    return 0


# ---------- handler ----------

def _cmd_status(args: argparse.Namespace) -> int:
    state = _state(args)
    eq = weapons_mod.equipped_weapon_id(state)
    session_dir = _resolve_session_dir(state, args.session)
    out: dict = {
        "state_root": str(state),
        "equipped_weapon": eq,
        "session_dir": str(session_dir) if session_dir else None,
        "session_id": session_dir.name if session_dir else None,
    }
    if session_dir is not None:
        out["chunk"] = _summarize_chunk_history(session_dir, args.recent)
        out["activations"] = _summarize_activations(session_dir, args.recent)
        out["channels"] = _summarize_channel_calls(session_dir)
        out["findings"] = _summarize_findings(session_dir)
        out["leads"] = _summarize_leads(session_dir)
        out["anomalies"] = _summarize_anomalies(session_dir)
        out["facts"] = _summarize_facts(session_dir)
        out["evidence"] = _summarize_evidence(session_dir)
    print(json.dumps(out, indent=2, default=str))
    return 0
