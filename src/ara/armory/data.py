"""Shared data layer for Armory views.

Every page used to collect sessions / policy hits / events inline. That
meant five different "sessions table" implementations, four different
"read recent policy_hits" implementations, and no way to test the
collection logic without also rendering HTML.

This module is the single source of truth for what the Armory knows
about sessions, their hits, and their events. Pages consume it and only
render.

Nothing here depends on Flask/HTTP; pure data + stdlib. Anything that
does IO is fail-open (returns empty/partial rather than raising) so the
web UI never crashes on a corrupt session dir.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# ---------- sessions ------------------------------------------------------

@dataclass
class SessionRow:
    """One row in a sessions listing. Populated from session.json + a
    lightweight scan of the audit files. Expensive fields (like full
    transcript) live on SessionDetail instead."""
    id: str
    root: Path
    started: float                # epoch seconds
    pid: Optional[int] = None
    is_active: bool = False       # pid alive right now
    is_contained: bool = False    # contain.manifest.json present
    hits: int = 0
    denies: int = 0
    guards: int = 0               # count of "hello" events
    pending_approvals: int = 0
    # Contained-specific stats; 0 when not contained.
    contain_files: int = 0
    contain_bytes: int = 0
    contain_home: str = ""        # from manifest

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.started)


@dataclass
class SessionDetail:
    """Deep-view of one session: everything a page might need.

    Kept as raw records (list of dicts) so the renderer can decide what
    to show. ``events`` and ``policy_hits`` are unbounded by default;
    callers cap as they wish."""
    row: SessionRow
    meta: dict
    events: list[dict] = field(default_factory=list)
    policy_hits: list[dict] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    contain_manifest: Optional[dict] = None
    contain_tree: list[tuple[Path, int]] = field(default_factory=list)  # (rel, size)


# ---------- tiny utilities ------------------------------------------------

def _pid_alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_tail_lines(
    path: Path, *, tail: int, max_bytes: int = 2 * 1024 * 1024,
) -> list[str]:
    """Return up to ``tail`` trailing lines without reading huge logs."""
    if tail <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            start = max(0, end - max_bytes)
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        # Drop the first partial line from a mid-file read window.
        lines = lines[1:]
    return lines[-tail:]


def _read_jsonl(path: Path, *, tail: Optional[int] = None) -> list[dict]:
    """Return a list of parsed records from a JSONL file. ``tail``
    trims to the last N lines before parsing so we don't parse 2 MB of
    heartbeat records just to show 20 rows."""
    if not path.exists():
        return []
    if tail is not None and tail >= 0:
        lines = _read_tail_lines(path, tail=tail)
    else:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
    out: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def _read_session_meta(session_dir: Path) -> tuple[dict, float]:
    """Returns (meta_dict, started_at). Falls back to mtime if meta missing."""
    meta_path = session_dir / "session.json"
    started = session_dir.stat().st_mtime
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            started = float(meta.get("started_at", started))
        except Exception:  # noqa: BLE001
            pass
    return meta, started


# ---------- public API ----------------------------------------------------

def list_sessions(
    state_root: Path, *, limit: int = 200, summary_tail: int = 2000,
) -> list[SessionRow]:
    """Every session dir, newest-first, capped. Populates summary counts
    by scanning each session's events + policy_hits. Skips corrupt dirs
    silently."""
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    out: list[SessionRow] = []
    entries = sorted(
        (p for p in sessions_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for s in entries[:limit]:
        meta, started = _read_session_meta(s)
        pid = meta.get("pid")
        alive = _pid_alive(pid)
        manifest_path = s / "contain.manifest.json"
        is_contained = manifest_path.exists()
        contain_home = ""
        if is_contained:
            try:
                manifest = json.loads(manifest_path.read_text())
                contain_home = manifest.get("home") or ""
            except Exception:  # noqa: BLE001
                pass
        row = SessionRow(
            id=s.name,
            root=s,
            started=started,
            pid=pid if isinstance(pid, int) else None,
            is_active=alive,
            is_contained=is_contained,
            contain_home=contain_home,
        )
        # Lightweight scan: full files — these are bounded by practical
        # session sizes. For very large sessions we can tail-limit later.
        hits = _read_jsonl(s / "policy_hits.jsonl", tail=summary_tail)
        row.hits = len(hits)
        row.denies = sum(1 for h in hits if h.get("verdict") == "deny")
        events = _read_jsonl(s / "events.jsonl", tail=summary_tail)
        row.guards = sum(1 for e in events if e.get("kind") == "hello")
        pdir = s / "approvals" / "pending"
        if pdir.is_dir():
            try:
                row.pending_approvals = sum(
                    1 for p in pdir.iterdir() if p.suffix == ".json"
                )
            except OSError:
                pass
        if is_contained:
            contain_root = s / "contain"
            if contain_root.is_dir():
                files = 0
                total = 0
                try:
                    for p in contain_root.rglob("*"):
                        if p.is_file():
                            files += 1
                            try:
                                total += p.stat().st_size
                            except OSError:
                                continue
                        if files >= 5000:
                            break
                except OSError:
                    pass
                row.contain_files = files
                row.contain_bytes = total
        out.append(row)
    return out


def session_detail(
    state_root: Path, session_id: str, *,
    events_tail: int = 2000,
    hits_tail: int = 2000,
    transcript_tail: int = 200,
    tree_limit: int = 200,
) -> Optional[SessionDetail]:
    """Return everything about one session, or None if not found.
    Tailed by default — pass larger numbers if a page needs the full
    stream, but the defaults are sized for the common "glance at recent"
    use-case."""
    s = state_root / "sessions" / session_id
    if not s.is_dir():
        return None
    meta, started = _read_session_meta(s)
    pid = meta.get("pid") if isinstance(meta.get("pid"), int) else None
    manifest_path = s / "contain.manifest.json"
    manifest: Optional[dict] = None
    contain_home = ""
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            contain_home = (manifest or {}).get("home") or ""
        except Exception:  # noqa: BLE001
            manifest = None
    row = SessionRow(
        id=session_id,
        root=s,
        started=started,
        pid=pid,
        is_active=_pid_alive(pid),
        is_contained=manifest_path.exists(),
        contain_home=contain_home,
    )
    events = _read_jsonl(s / "events.jsonl", tail=events_tail)
    hits = _read_jsonl(s / "policy_hits.jsonl", tail=hits_tail)
    transcript = _read_jsonl(s / "transcript.jsonl", tail=transcript_tail)
    row.hits = len(hits)
    row.denies = sum(1 for h in hits if h.get("verdict") == "deny")
    row.guards = sum(1 for e in events if e.get("kind") == "hello")

    tree: list[tuple[Path, int]] = []
    contain_root = s / "contain"
    if contain_root.is_dir():
        try:
            for p in sorted(contain_root.rglob("*")):
                if p.is_file():
                    try:
                        tree.append((p.relative_to(contain_root), p.stat().st_size))
                    except OSError:
                        continue
                if len(tree) >= tree_limit:
                    break
        except OSError:
            pass

    return SessionDetail(
        row=row, meta=meta, events=events, policy_hits=hits,
        transcript=transcript, contain_manifest=manifest, contain_tree=tree,
    )


# ---------- aggregated hit stream (for audit / status histograms) ---------

def iter_policy_hits(
    state_root: Path, *,
    window_s: float = 3600.0,
    session_limit: int = 20,
    session: Optional[str] = None,
) -> Iterable[dict]:
    """Yield policy_hits newer than ``window_s`` across the N most-recent
    sessions, optionally narrowed to a single session_id."""
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    cutoff = time.time() - window_s
    if session:
        candidates = [sessions_dir / session]
    else:
        candidates = sorted(
            (p for p in sessions_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:session_limit]
    out: list[dict] = []
    for s in candidates:
        if not s.is_dir():
            continue
        for r in _read_jsonl(s / "policy_hits.jsonl"):
            if (r.get("ts") or 0) < cutoff:
                continue
            r.setdefault("_session", s.name[:10])
            out.append(r)
    return out


def iter_recent_events(
    state_root: Path, *, limit: int = 200, session_limit: int = 5,
) -> list[dict]:
    """Newest-first unified stream of events.jsonl + policy_hits.jsonl
    from the N most-recent sessions. Each record is tagged with
    ``_session`` (short id) and ``_source`` (filename)."""
    sessions_dir = state_root / "sessions"
    if not sessions_dir.is_dir():
        return []
    rows: list[dict] = []
    for s in sorted(
        (p for p in sessions_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:session_limit]:
        for name in ("events.jsonl", "policy_hits.jsonl"):
            for r in _read_jsonl(s / name, tail=limit):
                r["_session"] = s.name[:10]
                r["_source"] = name
                rows.append(r)
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit]
