"""`ara explain` and `ara status` — inspection commands for operators."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import armors as armors_mod
from . import overrides as overrides_mod
from .audit import verify_chain
from .launch import DEFAULT_WALLS
from .policy import evaluate
from .session import default_state_root
from .walls import (
    Walls, parse_file,
    _glob_to_regex, find_approval_match, find_match, find_command_match,
    find_net_match,
)


def _resolve_state_root(override: Path | None) -> Path:
    return override or default_state_root()


def _fmt_ago(ts: float) -> str:
    dt = max(0, int(time.time() - ts))
    if dt < 60:
        return f"{dt}s"
    if dt < 3600:
        return f"{dt // 60}m"
    return f"{dt // 3600}h"


def _resolve_walls_source(args, state_root: Path) -> tuple[Path | None, str]:
    """Prefer --walls, else the equipped armor's WALLS, else the bundled default."""
    if args.walls:
        return args.walls, "--walls flag"
    active = armors_mod.active_walls_file(state_root)
    if active is not None and active.exists():
        name = armors_mod.active_armor_name(state_root) or "?"
        return active, f"equipped armor ({name})"
    if DEFAULT_WALLS.exists():
        return DEFAULT_WALLS, "bundled default"
    return None, "(none)"


def _effective_override_store(state_root: Path) -> overrides_mod.Store:
    """Global + equipped-armor store, same precedence the supervisor uses."""
    store = overrides_mod.load(state_root)
    active = armors_mod.active_armor_name(state_root)
    if active:
        armor = armors_mod.load_armor(state_root, active)
        if armor is not None:
            armor_store = overrides_mod.load_at(armor.overrides_path)
            store = store.merge(armor_store)
    return store


def _verdict_color(v: str) -> str:
    if not sys.stdout.isatty():
        return v.upper()
    colors = {
        "allow": "\x1b[32m",    # green
        "deny": "\x1b[31m",     # red
        "approval": "\x1b[33m", # yellow
    }
    c = colors.get(v, "")
    return f"{c}{v.upper()}\x1b[0m" if c else v.upper()


def _match_matrix(walls: Walls, query: dict) -> dict:
    """Return every rule that *could* match, regardless of precedence.
    Useful for the operator to see competing / overlapping rules."""
    op = query.get("op", "")
    out = {"deny_path": [], "deny_command": [], "deny_net": [],
           "approval_path": [], "approval_command": [], "approval_net": []}
    if op.startswith("fs."):
        path = query.get("path", "")
        for r in walls.path_rules:
            if _glob_to_regex(r.pattern).match(path):
                out["deny_path"].append(r)
        for r in walls.approval_rules:
            if r.kind == "path" and _glob_to_regex(r.pattern).match(path):
                out["approval_path"].append(r)
    elif op == "exec.spawn":
        command = query.get("command", "") or ""
        args_ = query.get("args") or []
        hit = find_command_match(walls, command, args_)
        if hit: out["deny_command"].append(hit)
        for r in walls.approval_rules:
            if r.kind == "command":
                import os as _os
                basename = _os.path.basename(command) if command else ""
                for cand in (command, basename):
                    if cand and _glob_to_regex(r.pattern).match(cand):
                        out["approval_command"].append(r)
                        break
    elif op == "net.connect":
        host = query.get("host", "")
        port = query.get("port")
        hit = find_net_match(walls, host, port)
        if hit: out["deny_net"].append(hit)
        ar = find_approval_match(walls, op, host=host, port=port)
        if ar and ar.kind == "net":
            out["approval_net"].append(ar)
    return out


def cmd_explain(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    walls_path, source = _resolve_walls_source(args, state_root)
    if walls_path is None or not walls_path.exists():
        print("ara explain: no WALLS.md found (use --walls)", file=sys.stderr)
        return 2

    try:
        walls: Walls = parse_file(walls_path)
    except Exception as exc:
        print(f"ara explain: WALLS parse failed: {exc}", file=sys.stderr)
        return 2

    store = _effective_override_store(state_root)
    shadow = armors_mod.active_shadow_mode(state_root)

    query: dict = {"op": args.op}
    if args.op.startswith("fs."):
        query["path"] = args.target
        subject_line = f"target:   {args.target}"
    elif args.op == "exec.spawn":
        query["command"] = args.target
        query["args"] = list(args.args or [])
        if args.cwd:
            query["cwd"] = args.cwd
        argv_str = " ".join([args.target, *query["args"]]) if query["args"] else args.target
        subject_line = f"command:  {argv_str}"
        if args.cwd:
            subject_line += f"\ncwd:      {args.cwd}"
    elif args.op == "net.connect":
        if ":" in args.target and not args.target.startswith("/"):
            host, _, p = args.target.rpartition(":")
            query["host"] = host
            try:
                query["port"] = int(p)
            except ValueError:
                query["port"] = p
        else:
            query["host"] = args.target
            query["port"] = args.port or 80
        subject_line = f"target:   {query['host']}:{query['port']}"
    else:
        print(f"ara explain: unknown op {args.op!r}", file=sys.stderr)
        return 2

    decision = evaluate(walls, query, override_store=store)
    effective = decision
    shadow_note = ""
    if shadow and decision.verdict == "deny":
        shadow_note = "  (shadow-mode armor would flip this to ALLOW + audit)"

    print(f"op:       {args.op}")
    print(subject_line)
    print(f"walls:    {walls_path}  [{source}]")
    print()

    # --- Walk through decision layers ---
    print("=== Evaluation ===")

    # 1. Blackholes
    bh_hits = []
    for e_ in store.blackholes:
        if _matches_override(e_, query):
            bh_hits.append(e_)
    print(f"  1. blackholes ({len(store.blackholes)} entries): "
          f"{len(bh_hits)} match")
    for e_ in bh_hits:
        print(f"       BLACKHOLE {e_.id} kind={e_.kind} pattern={e_.pattern!r}"
              + (f" reason={e_.reason!r}" if e_.reason else ""))

    # 2. Overrides
    ov_hits = []
    for e_ in store.overrides:
        if _matches_override(e_, query):
            ov_hits.append(e_)
    print(f"  2. overrides  ({len(store.overrides)} entries): "
          f"{len(ov_hits)} match")
    for e_ in ov_hits:
        print(f"       OVERRIDE {e_.id} kind={e_.kind} pattern={e_.pattern!r}"
              + (f" reason={e_.reason!r}" if e_.reason else ""))

    # 3. Deny walls (matrix — show all matches, even ones that wouldn't fire
    #    due to short-circuit).
    matrix = _match_matrix(walls, query)
    deny_total = len(matrix["deny_path"]) + len(matrix["deny_command"]) + len(matrix["deny_net"])
    print(f"  3. deny walls: {deny_total} match")
    for section in ("deny_path", "deny_command", "deny_net"):
        for r in matrix[section]:
            print(f"       {r.rule_id:<22} pattern={r.pattern!r}")

    # 4. Approval walls
    appr_total = len(matrix["approval_path"]) + len(matrix["approval_command"]) + len(matrix["approval_net"])
    print(f"  4. approval walls: {appr_total} match")
    for section in ("approval_path", "approval_command", "approval_net"):
        for r in matrix[section]:
            print(f"       {r.rule_id:<22} pattern={r.pattern!r}")

    print()
    print("=== Decision ===")
    print(f"  verdict:  {_verdict_color(effective.verdict)}{shadow_note}")
    if effective.reason:
        print(f"  reason:   {effective.reason}")
    if effective.rule_id:
        print(f"  rule:     {effective.rule_id}")
    if effective.touched:
        print(f"  touched:  {effective.touched}")
    if effective.approval_pattern:
        print(f"  approval pattern: {effective.approval_pattern}")

    # Walls summary
    print()
    print("=== Context ===")
    print(f"  walls:       {walls_path}")
    print(f"  deny_path:   {len(walls.path_rules)}")
    print(f"  deny_command:{len(walls.command_rules)}")
    print(f"  deny_net:    {len(walls.net_rules)}")
    print(f"  approval:    {len(walls.approval_rules)}")
    print(f"  overrides:   {len(store.overrides)}  blackholes: {len(store.blackholes)}")
    if shadow:
        print("  shadow_mode: ON (equipped armor is dry-run)")
    return 0


def _matches_override(entry: overrides_mod.Entry, query: dict) -> bool:
    """Same logic as policy._matches_entry but inlined so we don't pull
    the private helper into CLI territory."""
    import os as _os
    op = query.get("op", "")
    if entry.kind == "path":
        candidate = query.get("path")
        if not candidate and op == "net.connect":
            host = query.get("host", "")
            port = query.get("port")
            candidate = f"{host}:{port}" if port is not None else host
        if not candidate:
            return False
        return bool(_glob_to_regex(entry.pattern).match(candidate))
    if entry.kind == "command":
        command = query.get("command")
        if not command:
            return False
        basename = _os.path.basename(command)
        for cand in (command, basename):
            if cand and _glob_to_regex(entry.pattern).match(cand):
                return True
    return False


def cmd_status(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        print("(no sessions)")
        return 0

    sessions = sorted(
        [p for p in sessions_dir.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not sessions:
        print("(no sessions)")
        return 0

    print(f"{'SESSION':14} {'AGE':6} {'GUARDS':7} {'HITS':6} {'DENIES':7} {'PENDING':8}")
    for s in sessions[: args.limit]:
        meta_path = s / "session.json"
        started = (
            json.loads(meta_path.read_text()).get("started_at", s.stat().st_mtime)
            if meta_path.exists()
            else s.stat().st_mtime
        )
        events_path = s / "events.jsonl"
        hits_path = s / "policy_hits.jsonl"
        pending_dir = s / "approvals" / "pending"

        guards = 0
        if events_path.exists():
            for line in events_path.read_text().splitlines():
                try:
                    if json.loads(line).get("kind") == "hello":
                        guards += 1
                except Exception:
                    continue

        hits = 0
        denies = 0
        if hits_path.exists():
            for line in hits_path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    hits += 1
                    if r.get("verdict") == "deny":
                        denies += 1
                except Exception:
                    continue

        pending = (
            len(list(pending_dir.glob("*.json"))) if pending_dir.is_dir() else 0
        )

        print(
            f"{s.name[:12]:14} {_fmt_ago(started):6} "
            f"{guards:<7} {hits:<6} {denies:<7} {pending:<8}"
        )

    # Global store summary
    store = overrides_mod.load(state_root)
    print()
    print(f"overrides:  {len(store.overrides)}   blackholes: {len(store.blackholes)}")
    return 0


def install_inspect_subcommands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("explain",
                       help="dry-run a query against WALLS + overrides (no execution)")
    p.add_argument("target",
                   help="path (fs.*), command (exec.spawn), or host[:port] (net.connect)")
    p.add_argument("--op", default="fs.read",
                   help="fs.read | fs.readdir | fs.open | exec.spawn | net.connect "
                        "(default: fs.read)")
    p.add_argument("-a", "--args", nargs="*", default=None,
                   help="argv for exec.spawn (after the command)")
    p.add_argument("--cwd", type=str, default=None,
                   help="working directory for exec.spawn")
    p.add_argument("--port", type=int, default=None,
                   help="port for net.connect (if target lacks :port)")
    p.add_argument("--walls", type=Path, default=None,
                   help="WALLS.md to evaluate against (default: equipped armor, "
                        "else bundled default)")
    p.add_argument("--state-root", type=Path, default=None)
    p.set_defaults(_func=cmd_explain)

    p = sub.add_parser("status", help="show session + audit summary")
    p.add_argument("--state-root", type=Path, default=None)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(_func=cmd_status)

    p = sub.add_parser("verify-audit",
                       help="verify the hash chain on policy_hits.jsonl for a session")
    p.add_argument("session_id",
                   help="session id (prefix ok), or 'all' to check every session")
    p.add_argument("--state-root", type=Path, default=None)
    p.set_defaults(_func=cmd_verify_audit)


def cmd_verify_audit(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        print("(no sessions)")
        return 0
    targets = []
    if args.session_id == "all":
        targets = sorted(p for p in sessions_dir.iterdir() if p.is_dir())
    else:
        prefix = args.session_id
        targets = [
            p for p in sessions_dir.iterdir()
            if p.is_dir() and p.name.startswith(prefix)
        ]
    if not targets:
        print(f"no session matched {args.session_id!r}", file=sys.stderr)
        return 1
    bad = 0
    for s in targets:
        path = s / "policy_hits.jsonl"
        ok, lineno, err = verify_chain(path)
        if ok:
            print(f"OK   {s.name[:12]}  ({lineno} lines)")
        else:
            bad += 1
            print(f"FAIL {s.name[:12]}  line {lineno}: {err}", file=sys.stderr)
    return 1 if bad else 0
