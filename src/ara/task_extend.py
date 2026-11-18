"""Runtime deadline extension for focused (budgeted) task runs.

A focused "Polish" task starts on a fixed wall-clock budget (default 10 min).
Mid-task, the agent may ask for more time *with a justification* by running

    python3.9 -m ara.cli task-extend --minutes N --justification "..."

Requests are auto-granted up to a bounded ceiling (so an unattended focused
task can grow but not run forever); beyond the ceiling they are logged as a
pending operator approval and the deadline is left unchanged. The operator can
grant an over-ceiling (or any) extension with:

    python3.9 -m ara.cli task-extend --approve --minutes N \
        --task-id <id> --state-root <path>

The mechanism is a single ``deadline`` file (epoch seconds) that the durable
loop reads live each cycle; this module only ever pushes it *later*, and every
grant/denial is appended to ``deadline.grants.jsonl`` and the debug stream.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import debug_log

# ---- policy (seconds) -----------------------------------------------------
DEFAULT_BUDGET = 600          # initial focused-task budget (10 min)
MAX_PER_REQUEST = 15 * 60     # a single request may not exceed this
AUTO_CEILING = 40 * 60        # total wall-clock the agent may reach via auto-grants
MIN_JUSTIFICATION = 12        # chars — a real reason, not "more"


def _now() -> int:
    return int(time.time())


def _read_deadline(p: Path, fallback: int) -> int:
    try:
        return int(p.read_text().strip())
    except Exception:  # noqa: BLE001
        return fallback


def _write_deadline(p: Path, epoch: int) -> None:
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(str(int(epoch)))
    os.replace(str(tmp), str(p))  # atomic swap — the loop never reads a partial


def _grants_path(deadline_file: Path) -> Path:
    return Path(str(deadline_file) + ".grants.jsonl")


def _granted_seconds(grants_file: Path) -> int:
    total = 0
    if grants_file.is_file():
        for line in grants_file.open():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("granted"):
                total += int(r.get("seconds") or 0)
    return total


def _audit(grants_file: Path, rec: dict, *, state_root: str | None,
           session_id: str | None, task_id: str | None) -> None:
    grants_file.parent.mkdir(parents=True, exist_ok=True)
    with grants_file.open("a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    if state_root:
        try:
            debug_log.emit(
                "task-extend",
                "granted" if rec.get("granted") else "pending_operator",
                state_root=Path(state_root), session_id=session_id,
                task_id=task_id, minutes=rec.get("minutes"),
                seconds=rec.get("seconds"), mode=rec.get("mode"),
                justification=str(rec.get("justification") or "")[:200],
            )
        except Exception:  # noqa: BLE001
            pass


def request_extension(minutes: int, justification: str) -> "tuple[int, str]":
    """Agent-side request. Returns (exit_code, message). Never fatal to the run —
    the agent should keep working whether granted or pending."""
    df = os.environ.get("ARA_TASK_DEADLINE_FILE")
    if not df:
        return 2, ("task-extend: not inside a focused/extendable task "
                   "(no ARA_TASK_DEADLINE_FILE). Nothing to extend.")
    deadline_file = Path(df)
    state_root = os.environ.get("ARA_STATE_ROOT")
    session_id = os.environ.get("ARA_SESSION_ID")
    task_id = os.environ.get("ARA_TASK_RUN_ID")
    budget = int(os.environ.get("ARA_TASK_BUDGET_SECONDS") or DEFAULT_BUDGET)

    justification = (justification or "").strip()
    if len(justification) < MIN_JUSTIFICATION:
        return 2, (f"task-extend: a real justification is required "
                   f"(>= {MIN_JUSTIFICATION} chars). Not granted.")
    if minutes < 1:
        return 2, "task-extend: --minutes must be >= 1."
    req_seconds = min(int(minutes) * 60, MAX_PER_REQUEST)

    now = _now()
    cur_deadline = _read_deadline(deadline_file, now + budget)
    grants_file = _grants_path(deadline_file)
    granted_so_far = _granted_seconds(grants_file)
    projected_total = budget + granted_so_far + req_seconds

    if projected_total <= AUTO_CEILING:
        new_deadline = cur_deadline + req_seconds
        _write_deadline(deadline_file, new_deadline)
        _audit(grants_file, {
            "at": now, "minutes": int(minutes), "seconds": req_seconds,
            "justification": justification, "granted": True, "mode": "auto",
        }, state_root=state_root, session_id=session_id, task_id=task_id)
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(new_deadline))
        used = (granted_so_far + req_seconds) // 60
        cap = (AUTO_CEILING - budget) // 60
        actual = req_seconds // 60
        capped = (f" (capped from {int(minutes)})"
                  if actual < int(minutes) else "")
        return 0, (f"GRANTED +{actual} min{capped} (auto). New deadline {iso}. "
                   f"Auto-extension used {used}/{cap} min. Keep working.")

    _audit(grants_file, {
        "at": now, "minutes": int(minutes), "seconds": req_seconds,
        "justification": justification, "granted": False,
        "mode": "operator_pending",
    }, state_root=state_root, session_id=session_id, task_id=task_id)
    ceil_min = AUTO_CEILING // 60
    return 0, (f"PENDING OPERATOR APPROVAL: +{int(minutes)} min would exceed the "
               f"{ceil_min}-min auto-grant ceiling. Logged for the operator. Do "
               f"NOT block — keep working within the current deadline and wrap up "
               f"cleanly if it is not approved in time.")


def operator_grant(minutes: int, *, task_id: str, state_root: str) -> "tuple[int, str]":
    """Operator override — bump a task's deadline regardless of the auto ceiling."""
    deadline_file = Path(state_root) / "task-runs" / task_id / "deadline"
    if not deadline_file.exists():
        return 2, f"task-extend: no deadline file for task {task_id}."
    now = _now()
    req_seconds = int(minutes) * 60
    new_deadline = _read_deadline(deadline_file, now) + req_seconds
    _write_deadline(deadline_file, new_deadline)
    _audit(_grants_path(deadline_file), {
        "at": now, "minutes": int(minutes), "seconds": req_seconds,
        "justification": "operator override", "granted": True,
        "mode": "operator_approved",
    }, state_root=state_root, session_id=None, task_id=task_id)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(new_deadline))
    return 0, f"Operator granted +{int(minutes)} min to {task_id}. New deadline {iso}."


def install_task_extend_subcommand(sub) -> None:
    p = sub.add_parser(
        "task-extend",
        help="request/grant more wall-clock time for a focused task",
    )
    p.add_argument("--minutes", type=int, required=True,
                   help="minutes to add")
    p.add_argument("--justification", default="",
                   help="why more time is needed (agent request)")
    p.add_argument("--approve", action="store_true",
                   help="operator override grant (bypasses the auto ceiling)")
    p.add_argument("--task-id", default=None,
                   help="task run id (operator --approve)")
    p.add_argument("--state-root", default=None,
                   help="state root (operator --approve)")
    p.set_defaults(_func=_cmd_task_extend)


def _cmd_task_extend(args) -> int:
    if args.approve:
        tid = args.task_id or os.environ.get("ARA_TASK_RUN_ID") or ""
        sr = args.state_root or os.environ.get("ARA_STATE_ROOT") or ""
        if not tid or not sr:
            print("task-extend --approve needs --task-id and --state-root")
            return 2
        code, msg = operator_grant(args.minutes, task_id=tid, state_root=sr)
    else:
        code, msg = request_extension(args.minutes, args.justification)
    print(msg)
    return code
