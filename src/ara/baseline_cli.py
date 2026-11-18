"""`ara baseline` — record, diff, list observed resources across sessions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import baseline as baseline_mod
from .session import default_state_root


def _resolve_state_root(override: Path | None) -> Path:
    return override or default_state_root()


def _resolve_session(state_root: Path, ref: str) -> Path | None:
    """Accept 'latest', a prefix, or a full id."""
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return None
    entries = [p for p in sessions_dir.iterdir() if p.is_dir()]
    if not entries:
        return None
    if ref == "latest":
        return max(entries, key=lambda p: p.stat().st_mtime)
    for p in entries:
        if p.name == ref or p.name.startswith(ref):
            return p
    return None


def cmd_baseline_record(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    s = _resolve_session(state_root, args.from_session)
    if s is None:
        print(f"ara baseline: no session matched {args.from_session!r}", file=sys.stderr)
        return 1
    b = baseline_mod.record_from_session(state_root, args.name, s)
    print(
        f"recorded baseline {b.name!r} from session {s.name[:12]}: "
        f"{len(b.paths)} paths, {len(b.commands)} commands, {len(b.hosts)} hosts"
    )
    return 0


def cmd_baseline_list(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    names = baseline_mod.list_names(state_root)
    if not names:
        print("(no baselines)")
        return 0
    print(f"{'NAME':20} {'PATHS':>6} {'CMDS':>6} {'HOSTS':>6}  SOURCE_SESSION")
    for n in names:
        b = baseline_mod.load(state_root, n)
        print(
            f"{n[:20]:20} {len(b.paths):>6} {len(b.commands):>6} {len(b.hosts):>6}  "
            f"{b.source_session[:16]}"
        )
    return 0


def cmd_baseline_show(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        b = baseline_mod.load(state_root, args.name)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"# baseline: {b.name}")
    print(f"# source session: {b.source_session}")
    print(f"\n# paths ({len(b.paths)})")
    for p in b.paths: print(p)
    print(f"\n# commands ({len(b.commands)})")
    for c in b.commands: print(c)
    print(f"\n# hosts ({len(b.hosts)})")
    for h in b.hosts: print(h)
    return 0


def cmd_baseline_diff(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        b = baseline_mod.load(state_root, args.name)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    s = _resolve_session(state_root, args.from_session)
    if s is None:
        print(f"ara baseline: no session matched {args.from_session!r}", file=sys.stderr)
        return 1
    d = baseline_mod.diff_against_session(b, s)
    if d.total == 0:
        print(f"session {s.name[:12]}: no deviations from baseline {b.name!r}")
        return 0
    print(f"session {s.name[:12]} vs baseline {b.name!r}: "
          f"{d.total} new resources")
    if d.new_paths:
        print(f"\n# new paths ({len(d.new_paths)})")
        for p in d.new_paths: print(p)
    if d.new_commands:
        print(f"\n# new commands ({len(d.new_commands)})")
        for c in d.new_commands: print(c)
    if d.new_hosts:
        print(f"\n# new hosts ({len(d.new_hosts)})")
        for h in d.new_hosts: print(h)
    return 0


def cmd_baseline_to_armor(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        summary = baseline_mod.to_armor(
            state_root, args.name, args.armor,
            overwrite=args.overwrite,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr); return 1
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        print("  (pass --overwrite to replace)", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr); return 1
    print(
        f"created armor {summary['armor']!r} from baseline {args.name!r}: "
        f"{summary['overrides_count']} overrides "
        f"({summary['paths']} paths, {summary['commands']} commands, "
        f"{summary['hosts']} hosts)"
    )
    print(f"  root: {summary['armor_root']}")
    print(f"  equip with: ara armor equip {summary['armor']} "
          f"--state-root {state_root}")
    return 0


def cmd_baseline_remove(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    p = baseline_mod.path_for(state_root, args.name)
    if not p.exists():
        print(f"no baseline named {args.name!r}", file=sys.stderr)
        return 1
    p.unlink()
    print(f"removed {args.name}")
    return 0


def install_baseline_subcommand(sub: argparse._SubParsersAction) -> None:
    baseline = sub.add_parser(
        "baseline", help="capture/compare known-good resource sets across sessions",
    )
    bsub = baseline.add_subparsers(dest="verb", required=True)

    def _add_state_root(p):
        p.add_argument("--state-root", type=Path, default=None)

    p = bsub.add_parser("record", help="snapshot observed resources from a session")
    p.add_argument("name")
    p.add_argument("--from-session", default="latest",
                   help="session id (prefix) or 'latest'")
    _add_state_root(p)
    p.set_defaults(_func=cmd_baseline_record)

    p = bsub.add_parser("list", help="list all baselines")
    _add_state_root(p)
    p.set_defaults(_func=cmd_baseline_list)

    p = bsub.add_parser("show", help="dump a baseline's contents")
    p.add_argument("name")
    _add_state_root(p)
    p.set_defaults(_func=cmd_baseline_show)

    p = bsub.add_parser("diff", help="show new resources vs a baseline")
    p.add_argument("name")
    p.add_argument("--from-session", default="latest")
    _add_state_root(p)
    p.set_defaults(_func=cmd_baseline_diff)

    p = bsub.add_parser(
        "to-armor",
        help="materialize a baseline as a new armor set (prompts-on-everything "
             "with baseline resources pre-allowed)",
    )
    p.add_argument("name", help="baseline to export")
    p.add_argument("--armor", required=True, help="name for the new armor")
    p.add_argument("--overwrite", action="store_true",
                   help="replace an existing armor with the same name")
    _add_state_root(p)
    p.set_defaults(_func=cmd_baseline_to_armor)

    p = bsub.add_parser("remove", help="delete a baseline")
    p.add_argument("name")
    _add_state_root(p)
    p.set_defaults(_func=cmd_baseline_remove)
