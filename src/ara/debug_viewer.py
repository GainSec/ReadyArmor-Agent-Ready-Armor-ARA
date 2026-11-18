"""Terminal viewer for the ARA debug JSONL stream."""
from __future__ import annotations

import argparse
import json
import shutil
import textwrap
import time
from pathlib import Path
from typing import Any

from .debug_log import debug_log_path


def load_records(
    path: Path,
    *,
    component: str | None = None,
    event: str | None = None,
    task: str | None = None,
    session: str | None = None,
    grep: str | None = None,
    level: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = {"level": "error", "component": "debug", "event": "malformed", "fields": {"line": line}}
            if _matches(row, component=component, event=event, task=task, session=session, grep=grep, level=level):
                rows.append(row)
    if limit and limit > 0:
        return rows[-int(limit):]
    return rows


def format_record(row: dict[str, Any], *, width: int | None = None) -> str:
    width = width or shutil.get_terminal_size((140, 30)).columns
    iso = str(row.get("iso") or row.get("ts") or "")
    ts = iso[11:19] if len(iso) >= 19 and "T" in iso else iso[:8]
    level = str(row.get("level") or "debug")[:5].upper()
    component = str(row.get("component") or "-")[:16]
    event = str(row.get("event") or "-")[:28]
    session_id = _short(row.get("session_id"))
    task_id = _short(row.get("task_id"))
    caller = str(row.get("caller") or "-")
    summary = _summary(row.get("fields") or {})
    caller = textwrap.shorten(caller, width=28, placeholder="...")
    prefix = f"{ts:<8} {level:<5} {component:<14} {event:<24} {session_id:<10} {task_id:<10} {caller:<28} "
    remaining = max(20, int(width) - len(prefix))
    return prefix + textwrap.shorten(summary, width=remaining, placeholder="...")


def print_records(path: Path, *, limit: int = 200, width: int | None = None, **filters: Any) -> int:
    rows = load_records(path, limit=limit, **filters)
    for row in rows:
        print(format_record(row, width=width))
    return 0


def run_curses(path: Path, *, refresh: float = 1.0, **filters: Any) -> int:
    import curses

    def _draw(stdscr) -> int:
        curses.curs_set(0)
        offset = 0
        selected = 0
        while True:
            height, width = stdscr.getmaxyx()
            rows = load_records(path, limit=None, **filters)
            if selected >= len(rows):
                selected = max(0, len(rows) - 1)
            visible_count = max(1, height - 4)
            if selected < offset:
                offset = selected
            if selected >= offset + visible_count:
                offset = selected - visible_count + 1
            stdscr.erase()
            header = f"ARA debug: {path}  rows={len(rows)}  q=quit j/k=move enter=details r=refresh"
            stdscr.addnstr(0, 0, header, width - 1, curses.A_REVERSE)
            for idx, row in enumerate(rows[offset:offset + visible_count], start=offset):
                attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
                stdscr.addnstr(idx - offset + 1, 0, format_record(row, width=width), width - 1, attr)
            if rows:
                detail = json.dumps(rows[selected], indent=2, sort_keys=True)
                stdscr.addnstr(height - 2, 0, textwrap.shorten(detail.replace("\n", " "), width=width - 1, placeholder="..."), width - 1)
            stdscr.refresh()
            stdscr.timeout(int(max(0.1, refresh) * 1000))
            key = stdscr.getch()
            if key in (ord("q"), 27):
                return 0
            if key in (ord("j"), curses.KEY_DOWN):
                selected = min(len(rows) - 1, selected + 1)
            elif key in (ord("k"), curses.KEY_UP):
                selected = max(0, selected - 1)
            elif key in (ord("r"), -1):
                time.sleep(0)
            elif key in (10, 13) and rows:
                _detail_screen(stdscr, rows[selected])
        return 0

    return curses.wrapper(_draw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ara debug-log", description="View ARA debug JSONL events")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--component")
    parser.add_argument("--event")
    parser.add_argument("--task")
    parser.add_argument("--session")
    parser.add_argument("--grep")
    parser.add_argument("--level")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--follow", action="store_true", help="open curses live viewer")
    parser.add_argument("--no-curses", action="store_true", help="print rows and exit")
    args = parser.parse_args(argv)
    path = args.log or debug_log_path(args.state_root)
    if path is None:
        parser.error("provide --state-root or --log")
    filters = {
        "component": args.component,
        "event": args.event,
        "task": args.task,
        "session": args.session,
        "grep": args.grep,
        "level": args.level,
    }
    if args.no_curses or not args.follow:
        return print_records(path, limit=args.limit, **filters)
    return run_curses(path, **filters)


def _detail_screen(stdscr, row: dict[str, Any]) -> None:
    import curses

    lines = json.dumps(row, indent=2, sort_keys=True).splitlines()
    top = 0
    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        stdscr.addnstr(0, 0, "detail  q/esc=back  j/k=scroll", width - 1, curses.A_REVERSE)
        for y, line in enumerate(lines[top:top + height - 2], start=1):
            stdscr.addnstr(y, 0, line, width - 1)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), 27, 10, 13):
            return
        if key in (ord("j"), curses.KEY_DOWN):
            top = min(max(0, len(lines) - 1), top + 1)
        elif key in (ord("k"), curses.KEY_UP):
            top = max(0, top - 1)


def _matches(row: dict[str, Any], **filters: str | None) -> bool:
    for key in ("component", "event", "level"):
        wanted = filters.get(key)
        if wanted and str(row.get(key) or "").lower() != wanted.lower():
            return False
    if filters.get("task") and filters["task"] not in str(row.get("task_id") or ""):
        return False
    if filters.get("session") and filters["session"] not in str(row.get("session_id") or ""):
        return False
    grep = filters.get("grep")
    if grep and grep.lower() not in json.dumps(row, sort_keys=True).lower():
        return False
    return True


def _short(value: Any) -> str:
    text = str(value or "-")
    if text == "-":
        return text
    return text[:9]


def _summary(fields: Any) -> str:
    if not isinstance(fields, dict):
        return str(fields)
    parts: list[str] = []
    for key in sorted(fields):
        value = fields[key]
        if isinstance(value, (dict, list)):
            value_s = json.dumps(value, sort_keys=True)
        else:
            value_s = str(value)
        parts.append(f"{key}={value_s}")
    return " ".join(parts)


__all__ = [
    "format_record",
    "load_records",
    "main",
    "print_records",
    "run_curses",
]
