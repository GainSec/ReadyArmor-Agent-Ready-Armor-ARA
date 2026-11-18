"""`ara armor` subcommand — manage Armor Sets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import armors as armors_mod
from .session import default_state_root


def _resolve_state_root(override: Path | None) -> Path:
    return override or default_state_root()


def cmd_armor_list(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    rows = armors_mod.list_armors(state_root)
    active = armors_mod.active_armor_name(state_root)
    if not rows:
        print(f"(no armor sets yet — try `ara armor create <name>`)")
        if active:
            print(f"[active pointer set to {active!r} but no such armor]")
        return 0
    print(f"{'':3}{'NAME':22} {'TIER':4} {'WLL':>4} {'CMD':>4} {'APR':>4} {'OVR':>4} {'BLK':>4}  DESCRIPTION")
    for a in rows:
        marker = "★" if a.equipped else " "
        print(
            f"{marker:3}{a.name[:22]:22} "
            f"{'I II III IV V'.split()[a.stats.tier-1]:>4} "
            f"{a.stats.walls:>4} {a.stats.commands:>4} {a.stats.approvals:>4} "
            f"{a.stats.overrides:>4} {a.stats.blackholes:>4}  "
            f"{a.meta.description[:40]}"
        )
    if active and not any(a.name == active for a in rows):
        print(f"warning: active={active!r} but that armor directory is missing",
              file=sys.stderr)
    return 0


def cmd_armor_show(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    a = armors_mod.load_armor(state_root, args.name)
    if a is None:
        print(f"ara armor show: no armor named {args.name!r}", file=sys.stderr)
        return 2
    payload = {
        "name": a.name,
        "root": str(a.root),
        "equipped": a.equipped,
        "meta": a.meta.to_dict(),
        "stats": {
            "walls": a.stats.walls,
            "commands": a.stats.commands,
            "approvals": a.stats.approvals,
            "overrides": a.stats.overrides,
            "blackholes": a.stats.blackholes,
            "total": a.stats.total,
            "tier": a.stats.tier,
        },
        "files": {
            "walls": str(a.walls_path),
            "absolute": str(a.absolute_path),
            "overrides": str(a.overrides_path),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_armor_create(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        a = armors_mod.create_armor(
            state_root, args.name,
            from_walls=args.from_walls,
            description=args.description or "",
            sigil=args.sigil or "shield",
            tags=args.tag or [],
        )
    except (ValueError, FileExistsError) as exc:
        print(f"ara armor create: {exc}", file=sys.stderr)
        return 2
    print(f"created armor {a.name} at {a.root}")
    return 0


def cmd_armor_clone(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        a = armors_mod.clone_armor(state_root, args.src, args.dst)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(f"ara armor clone: {exc}", file=sys.stderr)
        return 2
    print(f"cloned {args.src} -> {a.name}")
    return 0


def cmd_armor_equip(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    a = armors_mod.equip_armor(state_root, args.name)
    if a is None:
        print(f"ara armor equip: no armor named {args.name!r}", file=sys.stderr)
        return 2
    print(f"equipped {a.name} (walls={a.stats.walls}, commands={a.stats.commands}, approvals={a.stats.approvals})")
    return 0


def cmd_armor_unequip(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    armors_mod.set_active_armor(state_root, None)
    print("no armor equipped")
    return 0


def cmd_armor_remove(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    if not armors_mod.delete_armor(state_root, args.name):
        print(f"ara armor remove: no armor named {args.name!r}", file=sys.stderr)
        return 2
    print(f"removed {args.name}")
    return 0


def install_armor_subcommand(sub: argparse._SubParsersAction) -> None:
    armor = sub.add_parser("armor", help="manage Armor Sets (named rule loadouts)")
    asub = armor.add_subparsers(dest="verb", required=True)

    def add_state_root_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--state-root", type=Path, default=None)

    p = asub.add_parser("list", help="list armor sets")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_list)

    p = asub.add_parser("show", help="print armor set details")
    p.add_argument("name")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_show)

    p = asub.add_parser("create", help="create a new armor set")
    p.add_argument("name")
    p.add_argument("--from-walls", type=Path, default=None,
                   help="seed WALLS.md from this file")
    p.add_argument("--description", default=None)
    p.add_argument("--sigil", default=None, help="icon hint: shield, castle, flask, hammer, lock")
    p.add_argument("--tag", action="append", default=[])
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_create)

    p = asub.add_parser("clone", help="duplicate an armor set under a new name")
    p.add_argument("src")
    p.add_argument("dst")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_clone)

    p = asub.add_parser("equip", help="mark an armor set as active (used by next ara launch)")
    p.add_argument("name")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_equip)

    p = asub.add_parser("unequip", help="clear the active armor")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_unequip)

    p = asub.add_parser("remove", help="delete an armor set")
    p.add_argument("name")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_remove)

    p = asub.add_parser("export", help="write an armor set to a .tgz tarball")
    p.add_argument("name")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="output file (default: ./armor-<name>.tgz; - for stdout)")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_export)

    p = asub.add_parser("import", help="install an armor set from a .tgz tarball")
    p.add_argument("file", type=Path, help="tarball path, or - for stdin")
    p.add_argument("--as", dest="rename_to", default=None,
                   help="install under this name instead of the archive's top-level name")
    p.add_argument("--overwrite", action="store_true",
                   help="replace an existing armor of the same name")
    add_state_root_arg(p)
    p.set_defaults(_func=cmd_armor_import)


def cmd_armor_export(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    try:
        blob = armors_mod.export_armor(state_root, args.name)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.output is None:
        out = Path(f"armor-{args.name}.tgz")
    else:
        out = args.output
    if str(out) == "-":
        sys.stdout.buffer.write(blob)
    else:
        out.write_bytes(blob)
        print(f"wrote {out} ({len(blob)} bytes)")
    return 0


def cmd_armor_import(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    if str(args.file) == "-":
        blob = sys.stdin.buffer.read()
    else:
        blob = Path(args.file).read_bytes()
    try:
        armor = armors_mod.import_armor(
            state_root, blob,
            rename_to=args.rename_to,
            overwrite=args.overwrite,
        )
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    print(f"imported as {armor.name}")
    return 0
