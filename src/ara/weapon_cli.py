"""``ara weapon`` CLI subcommands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import weapons as weapons_mod


def _default_state_root() -> Path:
    env = os.environ.get("ARA_STATE_ROOT")
    return Path(env) if env else Path(".ara")


def install_weapon_subcommand(sub: argparse._SubParsersAction) -> None:
    weapon = sub.add_parser(
        "weapon",
        help="manage equipped weapon for the active session",
    )
    weapon.add_argument(
        "--state-root", type=Path, default=None,
        help="ARA state dir (defaults to $ARA_STATE_ROOT or ./.ara/)",
    )
    wsub = weapon.add_subparsers(dest="weapon_cmd", required=True)

    p = wsub.add_parser("equip", help="equip a sealed weapon")
    p.add_argument("weapon_id")
    p.set_defaults(_handler=_cmd_equip)

    p = wsub.add_parser("unequip", help="unequip the current weapon")
    p.set_defaults(_handler=_cmd_unequip)

    p = wsub.add_parser("current", help="show the equipped weapon")
    p.set_defaults(_handler=_cmd_current)

    p = wsub.add_parser("list", help="list sealed weapons")
    p.set_defaults(_handler=_cmd_list)

    p = wsub.add_parser(
        "calibrate",
        help="run calibration queries against a sealed weapon's index "
             "and write a report to <weapon_dir>/calibration_report.json",
    )
    p.add_argument("weapon_id")
    p.add_argument(
        "--queries", type=Path, default=None,
        help="path to a calibration queries JSON file. Defaults to "
             "<weapon_dir>/calibration_queries.json.",
    )
    p.add_argument(
        "--min-precision", type=float, default=0.6,
        help="global minimum mean precision@5 (default 0.6).",
    )
    p.set_defaults(_handler=_cmd_calibrate)

    weapon.set_defaults(_handler=lambda _args: weapon.print_help() or 0)


def run(args: argparse.Namespace) -> int:
    h = getattr(args, "_handler", None)
    if h is None:
        return 1
    return h(args)


def _state(args: argparse.Namespace) -> Path:
    return Path(args.state_root) if args.state_root else _default_state_root()


def _cmd_equip(args: argparse.Namespace) -> int:
    state = _state(args)
    if not weapons_mod.weapon_exists(state, args.weapon_id):
        print(json.dumps({
            "error": f"weapon {args.weapon_id!r} not sealed (no seal.json under "
                     f"{weapons_mod.weapon_dir(state, args.weapon_id)})",
        }))
        return 2
    # ``equip_policy`` is overlay-append: it strips any prior weapon
    # overlay from the operator's files and writes the new one in,
    # so no manual unequip-of-prior-weapon dance is needed.
    weapons_mod.set_equipped_weapon(state, args.weapon_id)
    summary = weapons_mod.equip_policy(state, args.weapon_id)
    print(json.dumps({
        "equipped_weapon": args.weapon_id,
        "policy_overlaid": summary["overlaid"],
        "policy_kept": summary["kept"],
    }, indent=2))
    return 0


def _cmd_unequip(args: argparse.Namespace) -> int:
    state = _state(args)
    summary = weapons_mod.unequip_policy(state)
    weapons_mod.set_equipped_weapon(state, None)
    print(json.dumps({
        "equipped_weapon": None,
        "policy_stripped": summary["stripped"],
        "policy_untouched": summary["untouched"],
    }, indent=2))
    return 0


def _cmd_current(args: argparse.Namespace) -> int:
    print(json.dumps({
        "equipped_weapon": weapons_mod.equipped_weapon_id(_state(args)),
    }))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    state = _state(args)
    print(json.dumps({
        "weapons": weapons_mod.list_sealed_weapons(state),
        "equipped": weapons_mod.equipped_weapon_id(state),
    }, indent=2))
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    state = _state(args)
    wid = args.weapon_id
    if not weapons_mod.weapon_exists(state, wid):
        print(json.dumps({
            "error": f"weapon {wid!r} not sealed",
        }))
        return 2
    wdir = weapons_mod.weapon_dir(state, wid)
    queries_path = args.queries or (wdir / "calibration_queries.json")
    if not queries_path.exists():
        print(json.dumps({
            "error": f"no calibration queries at {queries_path}",
        }))
        return 2
    fts_path = wdir / "search" / "fts.db"
    if not fts_path.is_file():
        print(json.dumps({
            "error": f"weapon has no search index at {fts_path} — "
                     f"calibrate requires a sealed weapon with FTS data",
        }))
        return 2

    from .forge.index import calibration as cal_mod
    from .forge.index.dispatcher import open_dispatcher

    queries = cal_mod.load_queries(queries_path)
    if not queries:
        print(json.dumps({
            "error": f"no queries loaded from {queries_path}",
        }))
        return 2
    dispatcher = open_dispatcher(wdir)
    seal_id = ""
    seal_path = wdir / "seal.json"
    if seal_path.is_file():
        try:
            seal_id = json.loads(seal_path.read_text()).get(
                "weapon_hash", "",
            )
        except Exception:
            seal_id = ""
    report = cal_mod.run_calibration(
        dispatcher, queries, weapon_id=wid, seal_id=seal_id,
        global_min_p_at_5=args.min_precision,
    )
    out_path = wdir / "calibration_report.json"
    cal_mod.write_report(out_path, report)
    print(json.dumps({
        "report_path": str(out_path),
        "queries_total": report.queries_total,
        "queries_passed": report.queries_passed,
        "queries_failed": report.queries_failed,
        "mean_precision_at_5": report.mean_precision_at_5,
        "mean_recall_at_10": report.mean_recall_at_10,
        "seal_blocked": report.seal_blocked,
        "blocking_reasons": report.blocking_reasons,
    }, indent=2))
    return 0 if not report.seal_blocked else 3
