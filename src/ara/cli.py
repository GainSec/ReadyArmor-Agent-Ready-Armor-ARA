from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .admin_cli import install_admin_subcommands
from .armor_cli import install_armor_subcommand
from .baseline_cli import install_baseline_subcommand
from .inspect_cli import install_inspect_subcommands
from .launch import DEFAULT_WALLS, launch_cli
from .workers import claude_code as claude_code_worker
from .workers import openclaw as openclaw_worker


def _default_walls_arg() -> "Path | None":
    return DEFAULT_WALLS if DEFAULT_WALLS.exists() else None


def _resolve_state_root_for_equip(args) -> Path:
    """Pick the state root the launcher will use for equip/unequip.

    Mirrors how launch_cli + supervisor resolve it: explicit
    ``--state-root`` wins; else $ARA_STATE_ROOT; else ``./.ara``.
    """
    if getattr(args, "state_root", None):
        return Path(args.state_root)
    env = os.environ.get("ARA_STATE_ROOT")
    if env:
        return Path(env)
    return Path(".ara")


def _with_equipped_weapon(args, *, label: str, run):
    """If ``args.weapon`` is set, equip before ``run()`` and unequip
    in a finally block. Returns the launcher's exit code unchanged.

    No-op when ``args.weapon`` is None (callers fall through to plain
    ``run()``).
    """
    weapon_id = getattr(args, "weapon", None)
    if not weapon_id:
        return run()
    from . import weapons as weapons_mod
    state = _resolve_state_root_for_equip(args)
    if not weapons_mod.weapon_exists(state, weapon_id):
        print(
            f"[ara {label}] weapon {weapon_id!r} not sealed under "
            f"{weapons_mod.weapon_dir(state, weapon_id)}",
            file=sys.stderr,
        )
        return 2
    prior_equipped = weapons_mod.equipped_weapon_id(state)
    weapons_mod.set_equipped_weapon(state, weapon_id)
    summary = weapons_mod.equip_policy(state, weapon_id)
    print(
        f"[ara {label}] equipped weapon={weapon_id} "
        f"policy_overlaid={summary['overlaid']} kept={summary['kept']}",
        file=sys.stderr,
    )
    try:
        return run()
    finally:
        weapons_mod.unequip_policy(state)
        # Restore prior equipped marker (if any) so we don't leave the
        # operator's state pointing at a weapon they didn't ask for.
        weapons_mod.set_equipped_weapon(state, prior_equipped)
        print(
            f"[ara {label}] unequipped weapon={weapon_id}"
            + (f"; restored prior={prior_equipped}" if prior_equipped else ""),
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ara", description="Agent Ready Armor")
    parser.add_argument("--version", action="version", version=f"ara {__version__}")
    sub = parser.add_subparsers(dest="command")

    # Generic escape-hatch launcher.
    launch = sub.add_parser(
        "launch",
        help="spawn an arbitrary worker under ARA supervision (generic)",
    )
    launch.add_argument(
        "--walls",
        type=Path,
        default=None,
        help="explicit WALLS.md (overrides active armor, if any)",
    )
    launch.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="ARA state dir (defaults to ./.ara/)",
    )
    launch.add_argument(
        "--contain", action="store_true",
        help="session-scoped HOME/TMPDIR/XDG isolation + LD_PRELOAD shim",
    )
    launch.add_argument(
        "--contain-profile", default="default",
        help="profile name used to scope contained HOME/state",
    )
    launch.add_argument(
        "cmd", nargs=argparse.REMAINDER, help="command to run under ARA"
    )

    # Worker-specific subcommand: OpenClaw.
    oc = sub.add_parser(
        "openclaw",
        help="launch OpenClaw under ARA (ARA owns the profile)",
    )
    oc.add_argument(
        "--profile",
        default="default",
        help="ARA profile name; controls OpenClaw state dir under ~/.openclaw-ara-<profile>/",
    )
    oc.add_argument(
        "--walls",
        type=Path,
        default=_default_walls_arg(),
        help="path to WALLS.md (defaults to repo-bundled file)",
    )
    oc.add_argument(
        "--openclaw-bin",
        default=os.environ.get("ARA_OPENCLAW_BIN"),
        help="override path to openclaw binary (env: ARA_OPENCLAW_BIN)",
    )
    oc.add_argument(
        "--contain",
        action="store_true",
        help="session-scoped HOME/TMPDIR/XDG/CODEX isolation; bootstraps "
             "~/.codex/auth.json into the sandbox so OAuth survives",
    )
    oc.add_argument(
        "--instrument",
        action="store_true",
        help="verbose mode: NODE_DEBUG, DEBUG, ARA_HOOK_DEBUG, source maps",
    )
    oc.add_argument(
        "--no-tee",
        action="store_true",
        help="do NOT tee stdout/stderr into session-local log files "
             "(default is to tee when --contain is on)",
    )
    oc.add_argument(
        "--state-root", type=Path, default=None,
        help="ARA state dir for the supervisor + MITM (defaults to ./.ara/). "
             "Set to a shared root if you want the Armory dashboard to "
             "see this session's transcripts and policy hits.",
    )
    oc.add_argument(
        "--weapon", default=None, metavar="WEAPON_ID",
        help="forge-v1 weapon to equip before launch and unequip after exit. "
             "Mounts the engagement (channels + chunk runtime + auto-firer) "
             "and overlays the weapon's policy stack on top of standing rules.",
    )
    oc.add_argument(
        "args", nargs=argparse.REMAINDER, help="openclaw subcommand and flags"
    )

    # Worker-specific subcommand: Claude Code.
    cc = sub.add_parser(
        "claude",
        help="launch Claude Code under ARA (ARA owns the profile)",
    )
    cc.add_argument(
        "--profile", default="default",
        help="ARA profile name; controls state dir under ~/.claude-ara-<profile>/",
    )
    cc.add_argument(
        "--walls", type=Path, default=_default_walls_arg(),
        help="path to WALLS.md (defaults to repo-bundled file)",
    )
    cc.add_argument(
        "--claude-bin", default=os.environ.get("ARA_CLAUDE_BIN"),
        help="override path to claude binary (env: ARA_CLAUDE_BIN)",
    )
    cc.add_argument(
        "--contain", action="store_true",
        help="session-scoped HOME/TMPDIR/XDG isolation",
    )
    cc.add_argument(
        "--no-tee", action="store_true",
        help="do NOT tee stdout/stderr into session-local log files",
    )
    cc.add_argument(
        "--state-root", type=Path, default=None,
        help="ARA state dir (defaults to ./.ara/)",
    )
    cc.add_argument(
        "--weapon", default=None, metavar="WEAPON_ID",
        help="forge-v1 weapon to equip before launch and unequip after exit.",
    )
    cc.add_argument(
        "args", nargs=argparse.REMAINDER, help="claude subcommand and flags",
    )

    sub.add_parser(
        "forge-mcp",
        help=(
            "stdio MCP server bridging forge-v1 channels to MCP clients "
            "(Hermes Agent, Claude Desktop, etc). Must run inside an "
            "`ara launch --contain` session — reads ARA_SOCKET / "
            "ARA_SESSION_TOKEN from the contained env."
        ),
    )
    sub.add_parser(
        "temper-mcp",
        help=(
            "stdio MCP server for Tailor/Needle Temper candidate proposals. "
            "Writes pending candidates into the active Tailor session under "
            "the ARA state root; does not modify live armor policy."
        ),
    )
    sub.add_parser(
        "task-repair-commit",
        help=argparse.SUPPRESS,
    )
    sub.add_parser(
        "task-repair-chunk-transition",
        help=argparse.SUPPRESS,
    )
    sub.add_parser(
        "task-force-chunk-advance",
        help=argparse.SUPPRESS,
    )
    sub.add_parser(
        "task-verify-findings",
        help=argparse.SUPPRESS,
    )
    debug_log = sub.add_parser(
        "debug-log",
        help="view ARA debug JSONL events",
    )
    debug_log.add_argument("--state-root", type=Path, default=None)
    debug_log.add_argument("--log", type=Path, default=None)
    debug_log.add_argument("--component")
    debug_log.add_argument("--event")
    debug_log.add_argument("--task")
    debug_log.add_argument("--session")
    debug_log.add_argument("--grep")
    debug_log.add_argument("--level")
    debug_log.add_argument("--limit", type=int, default=200)
    debug_log.add_argument("--follow", action="store_true")
    debug_log.add_argument("--no-curses", action="store_true")

    armory = sub.add_parser("armory", help="run the Armory web UI")
    armory.add_argument("--state-root", type=Path, default=None)
    armory.add_argument("--walls", type=Path, default=DEFAULT_WALLS)
    armory.add_argument("--bind", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1; use 0.0.0.0 for all)")
    armory.add_argument("-A", "--all-interfaces", action="store_true",
                        help="shorthand for --bind 0.0.0.0")
    armory.add_argument("--port", type=int, default=4770)
    armory.add_argument("--tls-cert", type=Path, default=None,
                        help="PEM certificate to enable HTTPS (requires --tls-key)")
    armory.add_argument("--tls-key", type=Path, default=None,
                        help="PEM private key for --tls-cert")
    armory.add_argument("--behind-proxy", action="store_true",
                        help="trust X-Forwarded-Proto/Host/For headers "
                             "(only enable when running behind a reverse proxy)")

    install_admin_subcommands(sub)
    install_inspect_subcommands(sub)
    install_armor_subcommand(sub)
    install_baseline_subcommand(sub)

    from .forge.cli import install_forge_subcommand
    install_forge_subcommand(sub)

    from .weapon_cli import install_weapon_subcommand
    install_weapon_subcommand(sub)

    from .engagement_cli import install_engagement_subcommand
    install_engagement_subcommand(sub)

    from .trim_cli import install_trim_subcommand
    install_trim_subcommand(sub)

    from .tool_cli import install_tool_subcommand
    install_tool_subcommand(sub)

    from .task_extend import install_task_extend_subcommand
    install_task_extend_subcommand(sub)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "forge-mcp":
        from .clients.forge_mcp_server import run as _forge_mcp_run
        return _forge_mcp_run()

    if args.command == "temper-mcp":
        from .clients.temper_mcp_server import run as _temper_mcp_run
        return _temper_mcp_run()

    if args.command == "task-repair-commit":
        from .task_repair import repair_from_env as _repair_from_env
        return _repair_from_env()

    if args.command == "task-repair-chunk-transition":
        from .task_repair import repair_chunk_transition_from_env as _repair_from_env
        return _repair_from_env()

    if args.command == "task-force-chunk-advance":
        from .task_repair import force_chunk_advance_from_env as _force_advance
        return _force_advance()

    if args.command == "task-verify-findings":
        from .task_repair import verify_findings_from_env as _verify_findings
        return _verify_findings()

    if args.command == "debug-log":
        from .debug_viewer import main as _debug_viewer_main
        viewer_args: list[str] = []
        for flag, value in (
            ("--state-root", args.state_root),
            ("--log", args.log),
            ("--component", args.component),
            ("--event", args.event),
            ("--task", args.task),
            ("--session", args.session),
            ("--grep", args.grep),
            ("--level", args.level),
            ("--limit", args.limit),
        ):
            if value is not None:
                viewer_args.extend([flag, str(value)])
        if args.follow:
            viewer_args.append("--follow")
        if args.no_curses:
            viewer_args.append("--no-curses")
        return _debug_viewer_main(viewer_args)

    if args.command == "launch":
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print(
                "ara launch: need a command (try `ara launch -- node -e '...'`)",
                file=sys.stderr,
            )
            return 2
        return launch_cli(
            cmd,
            walls_file=args.walls,
            state_root=args.state_root,
            contain=args.contain,
            contain_profile=args.contain_profile,
        )

    # Admin subcommands set args._func on their leaf parsers.
    fn = getattr(args, "_func", None)
    if fn is not None:
        return fn(args)

    if args.command == "armory":
        from .armory import run_armory
        from .session import default_state_root
        state_root = args.state_root or default_state_root()
        walls = args.walls if args.walls and args.walls.exists() else None
        bind = "0.0.0.0" if args.all_interfaces else args.bind
        if bool(args.tls_cert) != bool(args.tls_key):
            print("ara armory: --tls-cert and --tls-key must be given together",
                  file=sys.stderr)
            return 2
        run_armory(
            state_root=state_root,
            walls_file=walls,
            bind=bind,
            port=args.port,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            behind_proxy=args.behind_proxy,
        )
        return 0

    if args.command == "openclaw":
        oc_args = args.args
        if oc_args and oc_args[0] == "--":
            oc_args = oc_args[1:]
        try:
            cmd, extra_env = openclaw_worker.build_command(
                args.profile,
                oc_args,
                binary_override=args.openclaw_bin,
                contained=bool(args.contain),
            )
        except RuntimeError as err:
            print(f"ara openclaw: {err}", file=sys.stderr)
            return 2
        print(
            f"[ara] openclaw profile={args.profile} state={extra_env['OPENCLAW_STATE_DIR']}",
            file=sys.stderr,
        )
        if args.contain:
            print("[ara] containment: on", file=sys.stderr)
        if args.instrument:
            print("[ara] instrumentation: on (verbose node/debug env)",
                  file=sys.stderr)
        # OpenClaw spends ~20-30s in synchronous module-ingest at boot,
        # blocking the heartbeat timer. Let the supervisor's watchdog know
        # before it starts so the first beat-miss check doesn't false-fire.
        if "ARA_HEARTBEAT_TIMEOUT_S" in extra_env:
            os.environ["ARA_HEARTBEAT_TIMEOUT_S"] = extra_env["ARA_HEARTBEAT_TIMEOUT_S"]
        return _with_equipped_weapon(args, label="openclaw", run=lambda: launch_cli(
            cmd,
            walls_file=args.walls,
            extra_env=extra_env,
            contain=args.contain,
            contain_profile=args.profile,
            instrumentation=args.instrument,
            tee_logs=args.contain and not args.no_tee,
            state_root=args.state_root,
        ))

    if args.command == "claude":
        cc_args = args.args
        if cc_args and cc_args[0] == "--":
            cc_args = cc_args[1:]
        try:
            cmd, extra_env = claude_code_worker.build_command(
                args.profile, cc_args,
                binary_override=args.claude_bin,
                contained=bool(args.contain),
            )
        except RuntimeError as err:
            print(f"ara claude: {err}", file=sys.stderr)
            return 2
        print(
            f"[ara] claude profile={args.profile} state={extra_env['CLAUDE_HOME']}",
            file=sys.stderr,
        )
        if args.contain:
            print("[ara] containment: on", file=sys.stderr)
        return _with_equipped_weapon(args, label="claude", run=lambda: launch_cli(
            cmd,
            walls_file=args.walls,
            extra_env=extra_env,
            contain=args.contain,
            contain_profile=args.profile,
            tee_logs=args.contain and not args.no_tee,
            state_root=getattr(args, "state_root", None),
        ))

    if args.command == "forge":
        from .forge.cli import run as forge_run
        return forge_run(args)

    if args.command == "weapon":
        from .weapon_cli import run as weapon_run
        return weapon_run(args)

    if args.command == "engagement":
        from .engagement_cli import run as engagement_run
        return engagement_run(args)

    print(f"[ara] command={args.command} not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
