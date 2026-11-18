"""`ara trim` subcommand — manage armor trims (engagement-scoped overlays).

See ``docs/specs/armor-trim-v0.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import trims as trims_mod
from .session import default_state_root


def _resolve_state_root(override: Path | None) -> Path:
    return override or default_state_root()


def _add_state_root_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--state-root", type=Path, default=None)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_trim_list(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    if args.armor:
        rows = trims_mod.list_trims(state_root, args.armor)
    else:
        rows = trims_mod.list_all_trims(state_root)
    if not rows:
        print("(no trims)")
        return 0
    print(f"{'':3}{'ARMOR':16} {'TRIM_ID':30} {'TARGET':40} EXPIRES")
    for t in rows:
        active = trims_mod.active_trim_id(state_root, t.armor)
        marker = "★" if active == t.trim_id else " "
        print(
            f"{marker:3}{t.armor[:16]:16} "
            f"{t.trim_id[:30]:30} "
            f"{t.target[:40]:40} "
            f"{t.expires_at or '-'}"
        )
    return 0


def cmd_trim_show(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    meta = trims_mod.load_trim(state_root, args.armor, args.trim_id)
    if meta is None:
        print(
            f"ara trim show: no trim {args.trim_id!r} under armor {args.armor!r}",
            file=sys.stderr,
        )
        return 2
    payload = {
        "meta": meta.to_dict(),
        "active": trims_mod.active_trim_id(state_root, args.armor) == args.trim_id,
        "files": {
            "meta": str(trims_mod.trim_meta_path(state_root, args.armor, args.trim_id)),
            "bulwark_section": str(
                trims_mod.trim_bulwark_section_path(state_root, args.armor, args.trim_id)
            ),
            "absolute": str(
                trims_mod.trim_absolute_path(state_root, args.armor, args.trim_id)
            ),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_trim_new(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        meta = trims_mod.new_trim(
            state_root, args.armor, args.trim_id,
            target=args.target or "",
            operator_id=args.operator or "",
            weapon_ref=args.weapon_ref,
            engagement_id=args.engagement_id,
        )
    except trims_mod.InvalidTrimId as exc:
        print(f"ara trim new: invalid trim id: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, FileExistsError) as exc:
        print(f"ara trim new: {exc}", file=sys.stderr)
        return 2
    print(
        f"created trim {meta.trim_id} under armor {meta.armor} "
        f"at {trims_mod.trim_dir(state_root, meta.armor, meta.trim_id)}"
    )
    return 0


def cmd_trim_derive(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    if str(args.engagement) == "-":
        blob = sys.stdin.read()
    else:
        blob = Path(args.engagement).read_text()
    try:
        engagement = json.loads(blob)
    except json.JSONDecodeError as exc:
        print(f"ara trim derive: engagement file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(engagement, dict):
        print("ara trim derive: engagement must be a JSON object", file=sys.stderr)
        return 2
    try:
        meta = trims_mod.derive_trim(
            state_root, args.armor,
            weapon_id=args.weapon_ref,
            engagement=engagement,
            trim_id=args.trim_id,
            operator_id=args.operator or "",
        )
    except trims_mod.InvalidTrimId as exc:
        print(f"ara trim derive: invalid trim id: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"ara trim derive: {exc}", file=sys.stderr)
        return 2
    print(
        f"derived trim {meta.trim_id} under armor {meta.armor} "
        f"(target={meta.target!r}, expires={meta.expires_at or '-'})"
    )
    if args.apply:
        return _apply(state_root, meta.armor, meta.trim_id)
    return 0


def cmd_trim_apply(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    return _apply(state_root, args.armor, args.trim_id)


def _apply(state_root: Path, armor: str, trim_id: str) -> int:
    try:
        res = trims_mod.apply_trim(state_root, armor, trim_id)
    except trims_mod.TrimNotFound as exc:
        print(f"ara trim apply: {exc}", file=sys.stderr)
        return 2
    except trims_mod.TrimCurrentlyApplied as exc:
        print(f"ara trim apply: {exc}", file=sys.stderr)
        return 2
    print(
        f"applied trim {res['trim_id']} to armor {res['armor']} "
        f"(bulwark={res['bulwark_overlaid']}, absolute={res['absolute_overlaid']}, "
        f"expires={res['expires_at'] or '-'})"
    )
    return 0


def cmd_trim_release(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    res = trims_mod.release_trim(state_root, args.armor, args.trim_id)
    if not res["released"]:
        print(f"(no trim active on armor {args.armor!r})")
        return 0
    print(
        f"released trim {res['trim_id']} from armor {res['armor']} "
        f"(bulwark_changed={res['bulwark_changed']}, "
        f"absolute_changed={res['absolute_changed']})"
    )
    return 0


def cmd_trim_delete(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        gone = trims_mod.delete_trim(state_root, args.armor, args.trim_id)
    except trims_mod.TrimCurrentlyApplied as exc:
        print(f"ara trim delete: {exc}", file=sys.stderr)
        return 2
    if not gone:
        print(
            f"ara trim delete: no trim {args.trim_id!r} under armor {args.armor!r}",
            file=sys.stderr,
        )
        return 2
    print(f"deleted trim {args.trim_id} from armor {args.armor}")
    return 0


def cmd_trim_status(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    out: dict = {}
    if args.armor:
        out[args.armor] = trims_mod.active_trim_id(state_root, args.armor)
    else:
        rows = trims_mod.list_all_trims(state_root)
        seen: set[str] = set()
        for t in rows:
            if t.armor in seen:
                continue
            seen.add(t.armor)
            out[t.armor] = trims_mod.active_trim_id(state_root, t.armor)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def install_trim_subcommand(sub: argparse._SubParsersAction) -> None:
    trim = sub.add_parser(
        "trim",
        help="manage armor trims (engagement-scoped overlays)",
    )
    tsub = trim.add_subparsers(dest="verb", required=True)

    p = tsub.add_parser("list", help="list trims (across all armors, or filtered)")
    p.add_argument("--armor", default=None,
                   help="show only trims under this armor")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_list)

    p = tsub.add_parser("show", help="print a trim's metadata + file paths")
    p.add_argument("armor")
    p.add_argument("trim_id")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_show)

    p = tsub.add_parser("new", help="create an empty trim under an armor")
    p.add_argument("armor")
    p.add_argument("trim_id")
    p.add_argument("--target", default=None, help="primary target (URL or host)")
    p.add_argument("--operator", default=None, help="operator id")
    p.add_argument("--weapon-ref", default=None,
                   help="weapon id this trim is paired with")
    p.add_argument("--engagement-id", default=None)
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_new)

    p = tsub.add_parser(
        "derive",
        help="derive a trim from an engagement.json (no LLM)",
    )
    p.add_argument("armor")
    p.add_argument("engagement", type=Path,
                   help="path to engagement.json (or '-' for stdin)")
    p.add_argument("--trim-id", default=None,
                   help="override trim id (default: engagement_id)")
    p.add_argument("--weapon-ref", default=None)
    p.add_argument("--operator", default=None)
    p.add_argument("--apply", action="store_true",
                   help="apply the derived trim immediately")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_derive)

    p = tsub.add_parser(
        "apply",
        help="merge a trim's overlays into bulwark.md + active absolute",
    )
    p.add_argument("armor")
    p.add_argument("trim_id")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_apply)

    p = tsub.add_parser(
        "release",
        help="strip a trim's overlays (defaults to whichever is active)",
    )
    p.add_argument("armor")
    p.add_argument("trim_id", nargs="?", default=None)
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_release)

    p = tsub.add_parser("delete", help="delete a trim (must not be applied)")
    p.add_argument("armor")
    p.add_argument("trim_id")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_delete)

    p = tsub.add_parser("status", help="show active trim per armor")
    p.add_argument("--armor", default=None)
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_trim_status)
