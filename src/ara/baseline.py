"""Baseline: capture "what one good run looked like" so the next run's
deviations are easy to spot.

A baseline is a JSON document listing the unique paths, commands, and
connect targets observed during a session. It lives at
``<state_root>/baselines/<name>.json``. Later:

  - ``ara baseline diff <name>`` shows everything in a new session that
    wasn't in the baseline.
  - ``ara baseline to-armor <name> --armor <new>`` (future) materializes
    a WALLS loadout where baseline resources are allowed and the rest
    prompt for approval.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


BASELINES_DIR = "baselines"


@dataclass
class Baseline:
    name: str
    created_at: float
    source_session: str = ""
    paths: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "source_session": self.source_session,
            "paths": sorted(set(self.paths)),
            "commands": sorted(set(self.commands)),
            "hosts": sorted(set(self.hosts)),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Baseline":
        return cls(
            name=str(raw.get("name", "")),
            created_at=float(raw.get("created_at") or 0.0),
            source_session=str(raw.get("source_session", "")),
            paths=list(raw.get("paths") or []),
            commands=list(raw.get("commands") or []),
            hosts=list(raw.get("hosts") or []),
        )


def baselines_root(state_root: Path) -> Path:
    return state_root / BASELINES_DIR


def path_for(state_root: Path, name: str) -> Path:
    return baselines_root(state_root) / f"{_safe(name)}.json"


def _safe(name: str) -> str:
    if not name or any(c in name for c in "/\x00"):
        raise ValueError(f"invalid baseline name: {name!r}")
    return name


def save(state_root: Path, baseline: Baseline) -> Path:
    dst = path_for(state_root, baseline.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(baseline.to_dict(), indent=2, sort_keys=True))
    os.replace(tmp, dst)
    return dst


def load(state_root: Path, name: str) -> Baseline:
    p = path_for(state_root, name)
    if not p.exists():
        raise FileNotFoundError(f"no baseline named {name!r}")
    return Baseline.from_dict(json.loads(p.read_text()))


def list_names(state_root: Path) -> list[str]:
    d = baselines_root(state_root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def _iter_session_hits(session_dir: Path) -> Iterable[dict]:
    for name in ("policy_hits.jsonl", "events.jsonl"):
        f = session_dir / name
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def observed_from_session(session_dir: Path) -> tuple[set[str], set[str], set[str]]:
    """Extract unique (paths, commands, hosts) from a session's audit files."""
    paths: set[str] = set()
    commands: set[str] = set()
    hosts: set[str] = set()
    for r in _iter_session_hits(session_dir):
        op = r.get("op") or r.get("kind") or ""
        if isinstance(op, str) and op.startswith("fs."):
            p = r.get("path") or r.get("touched")
            if isinstance(p, str) and p:
                paths.add(p)
        elif op in ("exec.spawn", "spawn"):
            c = r.get("command")
            if isinstance(c, str) and c:
                commands.add(c)
        elif op in ("net.connect", "http.request", "net_connect", "http_request"):
            host = r.get("host") or r.get("touched")
            port = r.get("port")
            if isinstance(host, str) and host:
                key = f"{host}:{port}" if port not in (None, "") else host
                hosts.add(key)
    return paths, commands, hosts


def record_from_session(
    state_root: Path, name: str, session_dir: Path,
) -> Baseline:
    paths, commands, hosts = observed_from_session(session_dir)
    b = Baseline(
        name=name,
        created_at=time.time(),
        source_session=session_dir.name,
        paths=sorted(paths),
        commands=sorted(commands),
        hosts=sorted(hosts),
    )
    save(state_root, b)
    return b


@dataclass
class Diff:
    new_paths: list[str]
    new_commands: list[str]
    new_hosts: list[str]

    @property
    def total(self) -> int:
        return len(self.new_paths) + len(self.new_commands) + len(self.new_hosts)


def diff_against_session(
    baseline: Baseline, session_dir: Path,
) -> Diff:
    paths, commands, hosts = observed_from_session(session_dir)
    return Diff(
        new_paths=sorted(paths - set(baseline.paths)),
        new_commands=sorted(commands - set(baseline.commands)),
        new_hosts=sorted(hosts - set(baseline.hosts)),
    )


def drift_score(baseline: Baseline, session_dir: Path) -> dict:
    """Compute a drift summary for a single session against a baseline.

    Returns a dict with:
      - ``new_count``   — new paths + commands + hosts observed
      - ``baseline_total`` — size of the baseline union
      - ``drift_ratio`` — ``new_count / max(baseline_total, 1)``, as a
        float. >0 means this session tripped new resources that the
        baseline didn't record. Bounded only by session activity.
      - ``new_paths``, ``new_commands``, ``new_hosts`` — lists (small
        samples are useful on the UI; the caller can truncate).
    """
    d = diff_against_session(baseline, session_dir)
    baseline_total = (
        len(baseline.paths) + len(baseline.commands) + len(baseline.hosts)
    )
    new_total = d.total
    ratio = new_total / baseline_total if baseline_total else (
        1.0 if new_total else 0.0
    )
    return {
        "new_count": new_total,
        "baseline_total": baseline_total,
        "drift_ratio": ratio,
        "new_paths": d.new_paths,
        "new_commands": d.new_commands,
        "new_hosts": d.new_hosts,
    }


_BASELINE_WALLS_TEMPLATE = """\
# Baseline-derived armor: {name}
# Generated from baseline {baseline!r} (session {source!r}).
#
# Policy: prompt on everything; baseline resources are pre-allowed via the
# armor's overrides.json. Tighten over time by moving entries from the
# overrides file into deny_* sections or by removing them entirely.

## approval_path
**

## approval_command
*

## approval_net
*:*
"""


def to_armor(
    state_root: Path,
    baseline_name: str,
    armor_name: str,
    *,
    overwrite: bool = False,
) -> dict:
    """Materialize a baseline as a new armor set.

    The generated armor's WALLS.md prompts-on-everything; its overrides
    list pre-allows the baseline's paths/commands/hosts. Combined effect:
    exactly the resources the baseline saw proceed silently; anything
    else goes to the operator.

    Returns a summary dict with counts and the destination path."""
    # Imported here to avoid a circular import (baseline → armors).
    from . import armors as armors_mod
    from . import overrides as overrides_mod

    b = load(state_root, baseline_name)
    if not armors_mod._is_valid_name(armor_name):
        raise ValueError(f"invalid armor name: {armor_name!r}")

    armor_root = armors_mod.armors_root(state_root) / armor_name
    if armor_root.exists():
        if not overwrite:
            raise FileExistsError(f"armor {armor_name!r} already exists")
        import shutil
        shutil.rmtree(armor_root)
    armor_root.mkdir(parents=True)

    (armor_root / armors_mod.WALLS_FILENAME).write_text(
        _BASELINE_WALLS_TEMPLATE.format(
            name=armor_name, baseline=b.name, source=b.source_session,
        )
    )
    (armor_root / armors_mod.ABSOLUTE_FILENAME).write_text(
        "# ABSOLUTE (envelope)\n\n"
        f"Derived from baseline {b.name!r} observed in session "
        f"{b.source_session!r}.\n"
    )

    overrides_path = armor_root / armors_mod.OVERRIDES_FILENAME
    tag = f"baseline:{b.name}"
    now = time.time()
    entries = []
    for p in b.paths:
        entries.append(overrides_mod.Entry(
            id=overrides_mod.new_entry_id(),
            kind="path", pattern=p,
            added_at=now, added_by=tag,
            reason=f"baseline-allowed ({b.name})",
        ))
    for c in b.commands:
        entries.append(overrides_mod.Entry(
            id=overrides_mod.new_entry_id(),
            kind="command", pattern=c,
            added_at=now, added_by=tag,
            reason=f"baseline-allowed ({b.name})",
        ))
    for h in b.hosts:
        entries.append(overrides_mod.Entry(
            id=overrides_mod.new_entry_id(),
            kind="path", pattern=h,  # net overrides key on "host:port" as path
            added_at=now, added_by=tag,
            reason=f"baseline-allowed net ({b.name})",
        ))
    store = overrides_mod.Store(overrides=entries, blackholes=[])
    overrides_mod.save_at(overrides_path, store)

    meta = armors_mod.ArmorMeta(
        description=f"Baseline {b.name!r} — prompts on anything the baseline didn't see",
        sigil="shield",
        tags=[f"baseline:{b.name}"],
        created_at=now,
        updated_at=now,
    )
    meta.save(armor_root / armors_mod.META_FILENAME)

    return {
        "armor": armor_name,
        "armor_root": str(armor_root),
        "overrides_count": len(entries),
        "paths": len(b.paths),
        "commands": len(b.commands),
        "hosts": len(b.hosts),
    }
