"""Operator subcommands: approval, overrides, blackholes.

All these operate on durable state under ``.ara/`` and can be invoked from
any terminal while a session is live (or after).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import approvals as approvals_mod
from . import overrides as overrides_mod
from .session import default_state_root


def _resolve_state_root(override: Path | None) -> Path:
    return override or default_state_root()


def _list_sessions(state_root: Path) -> list[Path]:
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    return [p for p in sessions_dir.iterdir() if p.is_dir()]


def _find_session_by_id_or_approval(state_root: Path, key: str) -> Path | None:
    """Given a session id OR an approval id, return the session root."""
    sessions = _list_sessions(state_root)
    # Match on session id (dir name) first.
    for s in sessions:
        if s.name == key or s.name.startswith(key):
            return s
    # Match on approval id across any session.
    for s in sessions:
        for sub in ("pending", "resolved"):
            if (s / "approvals" / sub / f"{key}.json").exists():
                return s
    return None


def _fmt_ago(ts: float) -> str:
    dt = max(0, int(time.time() - ts))
    if dt < 60:
        return f"{dt}s"
    if dt < 3600:
        return f"{dt // 60}m"
    return f"{dt // 3600}h"


# ------------- approval -------------

def cmd_approval_list(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    rows = []
    for s in _list_sessions(state_root):
        for rec in approvals_mod.list_pending(s):
            rows.append((s.name, rec))
    if not rows:
        print("(no pending approvals)")
        return 0
    print(f"{'APPROVAL ID':12} {'SESSION':12} {'AGE':6} {'OP':14} TARGET")
    for sid, rec in rows:
        target = rec.path or rec.command or ""
        print(
            f"{rec.id:12} {sid[:10]:12} {_fmt_ago(rec.created_at):6} "
            f"{rec.op:14} {target}"
        )
    return 0


def cmd_approval_show(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    session = _find_session_by_id_or_approval(state_root, args.id)
    if session is None:
        print(f"no approval or session matching '{args.id}'", file=sys.stderr)
        return 2
    rec = approvals_mod.read_pending(session, args.id)
    if rec is None:
        # Might be resolved.
        rfile = approvals_mod.resolved_dir(session) / f"{args.id}.json"
        if rfile.exists():
            print(rfile.read_text())
            return 0
        print(f"approval {args.id} not found", file=sys.stderr)
        return 2
    print(json.dumps(rec.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_approval_respond(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    session = _find_session_by_id_or_approval(state_root, args.id)
    if session is None:
        print(f"no pending approval matching '{args.id}'", file=sys.stderr)
        return 2
    action = args.action
    if action not in {"allow", "deny", "allow-session", "deny-session",
                      "allow-always", "deny-always"}:
        print(f"unknown action: {action}", file=sys.stderr)
        return 2
    ttl_s = None
    if args.ttl:
        try:
            ttl_s = overrides_mod.parse_ttl(args.ttl)
        except ValueError as exc:
            print(f"invalid --ttl: {exc}", file=sys.stderr)
            return 2
    record = approvals_mod.resolve(
        session, args.id, action, reason=args.reason, ttl_s=ttl_s,
    )
    if record is None:
        print(f"approval {args.id} not pending (already resolved or missing)",
              file=sys.stderr)
        return 2
    target = record.path or record.command or ""
    print(f"resolved {args.id} {action} ({record.status}) target={target}")
    return 0


# ------------- overrides / blackholes shared ------------

def _which_list(name: str, store: overrides_mod.Store) -> list[overrides_mod.Entry]:
    return store.overrides if name == "overrides" else store.blackholes


def _list_entries(name: str, args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    store = overrides_mod.load(state_root)
    entries = _which_list(name, store)
    if not entries:
        print(f"(no {name})")
        return 0
    print(f"{'ID':12} {'KIND':8} {'SCOPE':14} {'AGE':6} PATTERN")
    for e in entries:
        print(
            f"{e.id:12} {e.kind:8} {e.scope:14} {_fmt_ago(e.added_at):6} {e.pattern}"
        )
    return 0


def _show_entry(name: str, args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    entry = overrides_mod.find(state_root, args.id)
    if not entry:
        print(f"no {name} entry matching '{args.id}'", file=sys.stderr)
        return 2
    print(json.dumps(entry.to_dict(), indent=2, sort_keys=True))
    return 0


def _modify_entry(name: str, args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    if args.action == "add":
        if not args.pattern:
            print("ara {0} modify add: --pattern is required".format(name),
                  file=sys.stderr)
            return 2
        expires_at = None
        if args.ttl:
            try:
                expires_at = time.time() + overrides_mod.parse_ttl(args.ttl)
            except ValueError as exc:
                print(f"invalid --ttl: {exc}", file=sys.stderr)
                return 2
        if name == "overrides":
            entry = overrides_mod.add_override(
                state_root, kind=args.kind, pattern=args.pattern,
                reason=args.reason, scope=args.scope,
                expires_at=expires_at,
            )
        else:
            entry = overrides_mod.add_blackhole(
                state_root, kind=args.kind, pattern=args.pattern,
                reason=args.reason, scope=args.scope,
                expires_at=expires_at,
            )
        ttl_suffix = f" ttl={args.ttl}" if args.ttl else ""
        print(f"added {name[:-1]} {entry.id} kind={entry.kind} "
              f"pattern={entry.pattern}{ttl_suffix}")
        return 0
    if args.action == "remove":
        if not args.id:
            print(f"ara {name} modify remove: --id is required", file=sys.stderr)
            return 2
        removed = overrides_mod.remove(state_root, args.id)
        if not removed:
            print(f"no {name} entry with id {args.id}", file=sys.stderr)
            return 2
        print(f"removed {name[:-1]} {args.id}")
        return 0
    print(f"ara {name} modify: unknown action '{args.action}'", file=sys.stderr)
    return 2


_LEGACY_WARNED: set[str] = set()


def _deprecated(cmd: str) -> None:
    """Print a one-time deprecation banner to stderr before running the
    legacy handler. The legacy store was migrated to bulwark.md on the
    first armory startup — these commands now operate on the .bak
    files and have no runtime effect."""
    if cmd in _LEGACY_WARNED:
        return
    _LEGACY_WARNED.add(cmd)
    print(
        f"[deprecated] `ara {cmd}` is obsolete — rules live in bulwark.md "
        f"now. Use: ara bulwark list / add / remove. See "
        f"docs/BULWARK-SPEC.md for the DSL.",
        file=sys.stderr,
    )


def cmd_overrides_list(args: argparse.Namespace) -> int:
    _deprecated("overrides")
    return _list_entries("overrides", args)


def cmd_overrides_show(args: argparse.Namespace) -> int:
    _deprecated("overrides")
    return _show_entry("overrides", args)


def cmd_overrides_modify(args: argparse.Namespace) -> int:
    _deprecated("overrides")
    return _modify_entry("overrides", args)


def cmd_blackholes_list(args: argparse.Namespace) -> int:
    _deprecated("blackholes")
    return _list_entries("blackholes", args)


def cmd_blackholes_show(args: argparse.Namespace) -> int:
    _deprecated("blackholes")
    return _show_entry("blackholes", args)


def cmd_blackholes_modify(args: argparse.Namespace) -> int:
    _deprecated("blackholes")
    return _modify_entry("blackholes", args)


# ---------- bulwark ----------

def _default_state_root(args: argparse.Namespace) -> Path:
    return args.state_root if args.state_root else Path(".ara")


def cmd_bulwark_migrate(args: argparse.Namespace) -> int:
    from . import bulwark_migrate
    report = bulwark_migrate.migrate(
        _default_state_root(args),
        dry_run=args.dry_run,
        force=args.force,
    )
    if report.skipped:
        print(f"skipped: {report.skip_reason}")
        return 0
    if args.dry_run:
        print("# dry-run output (not written)")
        print(report.bulwark_text)
        if report.warnings:
            print("\n# warnings:")
            for w in report.warnings:
                print(f"  - {w}")
        return 0
    print(f"wrote {report.bulwark_path} ({report.rule_count} non-empty lines)")
    for src, bak in report.renamed:
        print(f"  renamed {src} → {bak}")
    for w in report.warnings:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


def cmd_bulwark_verify(args: argparse.Namespace) -> int:
    from . import bulwark
    path = _default_state_root(args) / "bulwark.md"
    if not path.exists():
        print(f"{path} does not exist", file=sys.stderr)
        return 1
    b = bulwark.parse_file(path)
    if b.parse_errors:
        for err in b.parse_errors:
            print(err, file=sys.stderr)
        return 1
    print(f"{path}: OK ({len(b.rules)} rules)")
    return 0


def cmd_session_list(args: argparse.Namespace) -> int:
    from .session import find_resumable_sessions
    import time as _t
    sessions = find_resumable_sessions(_default_state_root(args))
    for s in sessions:
        age = int(_t.time() - s.started_at)
        ago = f"{age//3600}h" if age >= 3600 else f"{age//60}m" if age >= 60 else f"{age}s"
        print(f"{s.id[:12]}  started={ago}ago  pid={s.pid}  root={s.root}")
    return 0


def cmd_session_resume(args: argparse.Namespace) -> int:
    """Print a resume command for the given session id.

    Actually re-launching the worker with the correct resume flag is
    the caller's job (ara openclaw / ara claude / ara launch with
    --resume-session); this subcommand just confirms the session is
    resumable and prints the canonical command.
    """
    from .session import find_resumable_sessions
    sessions = find_resumable_sessions(_default_state_root(args))
    matches = [s for s in sessions if s.id.startswith(args.id)]
    if not matches:
        print(f"session {args.id!r} not found", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous prefix {args.id!r} matches "
              f"{[s.id[:12] for s in matches]}", file=sys.stderr)
        return 1
    s = matches[0]
    print(f"session {s.id} is resumable.")
    print(f"  ara openclaw --resume-session {s.id} -- run  # or similar")
    print(f"  (or pass resume_id={s.id} to Supervisor.start())")
    return 0


def cmd_canary_add(args: argparse.Namespace) -> int:
    from . import canary
    try:
        c = canary.add(
            _default_state_root(args), Path(args.path),
            kind=args.kind, note=args.note,
        )
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{c.id}  {c.kind:<8}  {c.value}  -> {c.path}")
    return 0


def cmd_canary_list(args: argparse.Namespace) -> int:
    from . import canary
    store = canary.load(_default_state_root(args))
    for c in store.canaries:
        print(
            f"{c.id}  {c.kind:<8}  hits={c.hit_count:<3}  "
            f"value={c.value}  path={c.path}"
        )
    return 0


def cmd_canary_remove(args: argparse.Namespace) -> int:
    from . import canary
    ok = canary.remove(_default_state_root(args), args.id,
                       delete_file=args.delete_file)
    if not ok:
        print(f"canary {args.id} not found", file=sys.stderr)
        return 1
    return 0


def cmd_canary_verify(args: argparse.Namespace) -> int:
    from . import canary
    rep = canary.verify_all(_default_state_root(args))
    print(
        f"total={rep['total']}  ok={len(rep['ok'])}  "
        f"tampered={len(rep['tampered'])}  missing={len(rep['missing'])}"
    )
    for cid in rep["tampered"]:
        print(f"  tampered: {cid}", file=sys.stderr)
    for cid in rep["missing"]:
        print(f"  missing:  {cid}", file=sys.stderr)
    return 0 if not rep["tampered"] and not rep["missing"] else 1


def cmd_evidence_record(args: argparse.Namespace) -> int:
    from . import evidence
    try:
        rec = evidence.record_artifact(
            args.session_dir,
            tool_use_id=args.tool_use_id,
            tool_name=args.tool,
            action=args.action,
            artifact_path=args.path,
            hint=args.hint,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{rec.capture_id}  {rec.artifact_sha256[:12]}  {rec.size_bytes}b  {rec.artifact_path}")
    return 0


def cmd_evidence_list(args: argparse.Namespace) -> int:
    from . import evidence
    for r in evidence.load(args.session_dir):
        status = "ok" if evidence.verify(r) else "!!"
        print(f"{status}  {r.capture_id}  {r.tool_name:<14}  "
              f"{r.artifact_sha256[:12]}  {r.size_bytes}b  {r.artifact_path}")
    return 0


def cmd_evidence_verify(args: argparse.Namespace) -> int:
    from . import evidence
    rep = evidence.verify_session(args.session_dir)
    print(
        f"total={rep['total']}  ok={len(rep['ok'])}  "
        f"tampered={len(rep['tampered'])}  missing={len(rep['missing'])}"
    )
    for cid in rep["tampered"]:
        print(f"  tampered: {cid}", file=sys.stderr)
    for cid in rep["missing"]:
        print(f"  missing:  {cid}", file=sys.stderr)
    return 0 if not rep["tampered"] and not rep["missing"] else 1


def cmd_bulwark_list(args: argparse.Namespace) -> int:
    from . import bulwark
    path = _default_state_root(args) / "bulwark.md"
    if not path.exists():
        print(f"{path} does not exist", file=sys.stderr)
        return 1
    b = bulwark.parse_file(path)
    layer_filter = args.layer
    for r in b.rules:
        if layer_filter and not (r.layer == layer_filter or r.layer.startswith(f"{layer_filter}:")):
            continue
        print(f"[{r.rule_id}] ## {r.scope.raw}  {r.raw.strip()}")
    return 0


# ------------- wiring ------------

def add_state_root_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--state-root", type=Path, default=None,
                   help="ARA state dir (defaults to ./.ara)")


def install_admin_subcommands(sub: argparse._SubParsersAction) -> None:
    # approval
    approval = sub.add_parser("approval", help="manage pending approvals")
    asub = approval.add_subparsers(dest="verb", required=True)

    p = asub.add_parser("list", help="list pending approvals")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_approval_list)

    p = asub.add_parser("show", help="show an approval (pending or resolved)")
    p.add_argument("id")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_approval_show)

    p = asub.add_parser("respond", help="respond to a pending approval")
    p.add_argument("id")
    p.add_argument("action",
                   help="allow|deny|allow-session|deny-session|allow-always|deny-always")
    p.add_argument("--reason", default=None)
    p.add_argument("--ttl", default=None,
                   help="expiry for allow-always/deny-always grants "
                        "(e.g. 5m, 2h, 1d). Omit for permanent.")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_approval_respond)

    # overrides
    overrides = sub.add_parser("overrides", help="manage always-allow entries")
    osub = overrides.add_subparsers(dest="verb", required=True)

    p = osub.add_parser("list")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_overrides_list)

    p = osub.add_parser("show")
    p.add_argument("id")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_overrides_show)

    p = osub.add_parser("modify")
    p.add_argument("action", choices=["add", "remove"])
    p.add_argument("--id", default=None)
    p.add_argument("--kind", choices=["path", "command"], default="path")
    p.add_argument("--pattern", default=None)
    p.add_argument("--reason", default=None)
    p.add_argument("--scope", default="always")
    p.add_argument("--ttl", default=None,
                   help="expire after this duration (e.g. 5m, 2h, 1d)")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_overrides_modify)

    # blackholes
    blackholes = sub.add_parser("blackholes", help="manage always-deny entries")
    bsub = blackholes.add_subparsers(dest="verb", required=True)

    p = bsub.add_parser("list")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_blackholes_list)

    p = bsub.add_parser("show")
    p.add_argument("id")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_blackholes_show)

    p = bsub.add_parser("modify")
    p.add_argument("action", choices=["add", "remove"])
    p.add_argument("--id", default=None)
    p.add_argument("--kind", choices=["path", "command"], default="path")
    p.add_argument("--pattern", default=None)
    p.add_argument("--reason", default=None)
    p.add_argument("--scope", default="always")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_blackholes_modify)

    # bulwark (unified policy)
    bulwark = sub.add_parser("bulwark", help="unified policy (walls + allowlist + rhema)")
    bsub = bulwark.add_subparsers(dest="verb", required=True)

    p = bsub.add_parser("migrate", help="synthesize bulwark.md from legacy files")
    p.add_argument("--dry-run", action="store_true",
                   help="print synthesized output without writing")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing bulwark.md")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_bulwark_migrate)

    p = bsub.add_parser("verify", help="parse bulwark.md and report errors")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_bulwark_verify)

    p = bsub.add_parser("list", help="list rules (optionally filtered by layer)")
    p.add_argument("--layer", default=None,
                   help="filter to net | fs | nav | exec | perm | url | env | ask | reply | oc")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_bulwark_list)

    # session (resumption)
    session = sub.add_parser("session", help="list / resume sessions")
    ssub = session.add_subparsers(dest="verb", required=True)

    p = ssub.add_parser("list", help="list resumable sessions newest-first")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_session_list)

    p = ssub.add_parser("resume",
                        help="confirm session is resumable and print the canonical command")
    p.add_argument("id", help="session id or short prefix")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_session_resume)

    # canary (honeytokens)
    canary = sub.add_parser("canary", help="seed and watch honeytoken credentials")
    csub = canary.add_subparsers(dest="verb", required=True)

    p = csub.add_parser("add", help="seed a canary at a path and register it")
    p.add_argument("path", help="absolute path to seed (must not exist)")
    p.add_argument("--kind", default="generic",
                   choices=["aws", "stripe", "github", "openai", "generic"])
    p.add_argument("--note", default=None)
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_canary_add)

    p = csub.add_parser("list", help="list registered canaries + hit counts")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_canary_list)

    p = csub.add_parser("remove", help="deregister a canary")
    p.add_argument("id")
    p.add_argument("--delete-file", action="store_true",
                   help="also unlink the seed file")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_canary_remove)

    p = csub.add_parser("verify",
                        help="confirm every seed file still contains its value")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_canary_verify)

    # evidence (provenance index)
    evidence = sub.add_parser("evidence", help="artifact provenance index for a session")
    esub = evidence.add_subparsers(dest="verb", required=True)

    p = esub.add_parser("record", help="hash a file + append to the session's evidence index")
    p.add_argument("session_dir", type=Path, help="<state_root>/sessions/<sid>")
    p.add_argument("path", help="absolute path to the artifact")
    p.add_argument("--tool", required=True, help="tool name (e.g. oc:write)")
    p.add_argument("--action", default="capture",
                   help="action (write | read | fetch | exec | capture)")
    p.add_argument("--tool-use-id", default="manual",
                   help="opaque ID linking this capture to a tool call")
    p.add_argument("--hint", default=None, help="free-form context")
    p.set_defaults(_func=cmd_evidence_record)

    p = esub.add_parser("list", help="list all records for a session")
    p.add_argument("session_dir", type=Path)
    p.set_defaults(_func=cmd_evidence_list)

    p = esub.add_parser("verify", help="re-hash every record and report tampering")
    p.add_argument("session_dir", type=Path)
    p.set_defaults(_func=cmd_evidence_verify)
