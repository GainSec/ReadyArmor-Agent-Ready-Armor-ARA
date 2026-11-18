"""``ara tool`` subcommand — manage the tool classification registry.

Mirrors the armory ``/tools`` UI as a headless CLI for scripted
workflows + operators who'd rather stay on the terminal. Per spec
armor-trim-v0.1-amendment.md §11.

Verbs:

- ``list`` — table of every entry with classification flags + status.
- ``show <key>`` — full record (raw JSON + first-seen + override).
- ``approve <key>`` — flip status to ``confirmed`` (operator OK'd).
- ``override <key>`` — operator-edit classification with a reason.
- ``reclassify <key>`` — flag for re-run on next call.
- ``delete <key>`` — drop an entry from the registry.
- ``sweep-expired`` — fold ``pending_operator_review`` entries past
  their TTL into ``merged_by_ttl`` (auditably distinct from
  ``confirmed``).

Keys are ``<tool> | <flag_pattern>`` strings — same shape the armory
URL-encodes. CLI accepts them verbatim (use shell quoting).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import tool_registry as tr
from .session import default_state_root


def _resolve_state_root(override: Path | None) -> Path:
    return override or default_state_root()


def _add_state_root_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--state-root", type=Path, default=None)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    tr.STATUS_PENDING_FIRST_USE:    "blocked",
    tr.STATUS_PENDING_REVIEW:       "pending",
    tr.STATUS_CONFIRMED:            "confirmed",
    tr.STATUS_OPERATOR_OVERRIDDEN:  "overridden",
    tr.STATUS_MERGED_BY_TTL:        "merged",
}


def _flag_summary(c: tr.Classification) -> str:
    bits = [f for f in ("active", "destructive", "intrusive", "scanner")
            if getattr(c, f)]
    return ",".join(bits) or "passive"


def cmd_list(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    rows = tr.list_entries(state_root)
    if args.status:
        rows = [r for r in rows if r.status == args.status]
    if not rows:
        print("(registry is empty)")
        return 0
    print(f"{'TOOL':<20} {'FLAG_PATTERN':<22} {'CLASS':<28} "
          f"{'CONF':>5} {'STATUS':<10} USES  ENGAGEMENTS")
    for r in rows:
        eff = r.effective_classification
        engs = ",".join(r.engagements_used[:3]) or "-"
        if len(r.engagements_used) > 3:
            engs += f"+{len(r.engagements_used) - 3}"
        status = _STATUS_LABELS.get(r.status) or r.status or "?"
        if r.operator_override is not None:
            status = status + "*"
        if r.reclassify_on_next_call:
            status = status + "↻"
        print(
            f"{r.tool[:20]:<20} {r.flag_pattern[:22]:<22} "
            f"{_flag_summary(eff)[:28]:<28} "
            f"{r.confidence}/3   {status[:10]:<10} "
            f"{r.use_count:>4}  {engs}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    e = tr.lookup(state_root, args.key)
    if e is None:
        print(f"ara tool show: no entry for {args.key!r}", file=sys.stderr)
        return 2
    print(json.dumps(e.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    e = tr.operator_approve(state_root, args.key)
    if e is None:
        print(f"ara tool approve: no entry for {args.key!r}", file=sys.stderr)
        return 2
    print(f"approved {args.key} (status={_STATUS_LABELS[e.status]})")
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    cls = tr.Classification(
        active=bool(args.active),
        destructive=bool(args.destructive),
        intrusive=bool(args.intrusive),
        scanner=bool(args.scanner),
    )
    e = tr.operator_override(
        state_root, args.key,
        classification=cls, reason=args.reason,
    )
    if e is None:
        print(f"ara tool override: no entry for {args.key!r}", file=sys.stderr)
        return 2
    print(f"override saved on {args.key}: {_flag_summary(cls)}")
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    e = tr.operator_request_reclassify(state_root, args.key)
    if e is None:
        print(f"ara tool reclassify: no entry for {args.key!r}", file=sys.stderr)
        return 2
    print(f"flagged {args.key} for reclassify on next call")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    if not tr.delete_entry(state_root, args.key):
        print(f"ara tool delete: no entry for {args.key!r}", file=sys.stderr)
        return 2
    print(f"deleted {args.key}")
    return 0


def cmd_sweep_expired(args: argparse.Namespace) -> int:
    state_root = _resolve_state_root(args.state_root)
    merged = tr.merge_expired_reviews(state_root)
    if not merged:
        print("(no expired pending-review entries)")
        return 0
    for e in merged:
        print(f"merged {e.key} (was {_STATUS_LABELS[tr.STATUS_PENDING_REVIEW]})")
    print(f"\ntotal merged: {len(merged)}")
    return 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def install_tool_subcommand(sub: argparse._SubParsersAction) -> None:
    tool = sub.add_parser(
        "tool",
        help="manage the tool classification registry",
    )
    tsub = tool.add_subparsers(dest="verb", required=True)

    p = tsub.add_parser("list", help="list registry entries")
    p.add_argument("--status", default=None,
                   help=f"filter by status: {', '.join(tr.VALID_STATUSES)}")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_list)

    p = tsub.add_parser("show", help="dump one entry's full record")
    p.add_argument("key", help="registry key, e.g. \"nmap | -sV*\"")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_show)

    p = tsub.add_parser("approve", help="confirm an entry's classification")
    p.add_argument("key")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_approve)

    p = tsub.add_parser("override", help="operator-edit classification")
    p.add_argument("key")
    p.add_argument("--active", action="store_true")
    p.add_argument("--destructive", action="store_true")
    p.add_argument("--intrusive", action="store_true")
    p.add_argument("--scanner", action="store_true")
    p.add_argument("--reason", required=True,
                   help="why this differs from the agent's classification")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_override)

    p = tsub.add_parser("reclassify",
                        help="re-run classification on next call")
    p.add_argument("key")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_reclassify)

    p = tsub.add_parser("delete", help="drop an entry")
    p.add_argument("key")
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_delete)

    p = tsub.add_parser(
        "sweep-expired",
        help="fold TTL-expired pending_operator_review entries into "
             "merged_by_ttl",
    )
    _add_state_root_arg(p)
    p.set_defaults(_func=cmd_sweep_expired)
