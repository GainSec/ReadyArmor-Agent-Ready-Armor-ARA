"""Persistent registry of danger-classified target actions + auto-bulwark.

Every danger flag (irreversible/provable) the guard or MITM raises is recorded
to ``<state_root>/danger_flags.jsonl`` (deduped by tier+target) so the Spoils
UI can surface it: contents, why flagged, tier, and whether it was added to the
armor bulwark. Irreversible flags are additionally auto-appended to the armor's
bulwark as a hard ``deny`` under the active armor's scope — a permanent,
operator-visible block that survives restarts.

The bulwark append is best-effort persistence/visibility; the mechanical
guarantee is the code-level deny in the supervisor/MITM. Both together mean an
irreversible op is blocked now *and* recorded as a standing rule.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


def flags_path(state_root: Path) -> Path:
    return Path(state_root) / "danger_flags.jsonl"


def list_flags(state_root: Path) -> list[dict]:
    """All flags, latest record per (tier, target) wins."""
    p = flags_path(state_root)
    if not p.exists():
        return []
    out: dict[tuple, dict] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        out[(r.get("tier"), r.get("target"))] = r
    return sorted(out.values(), key=lambda r: r.get("at") or 0, reverse=True)


def _existing(state_root: Path) -> dict[tuple, dict]:
    return {(r["tier"], r["target"]): r for r in list_flags(state_root)}


def record_flag(
    state_root: Path, *, tier: str, target: str, pattern: str, reason: str,
    op: str, layer: str = "", method: str = "",
    armor_id: Optional[str] = None, added_to_bulwark: bool = False,
    session_id: Optional[str] = None,
) -> dict:
    """Append a flag record. No-op if an identical record (same tier/target
    and same bulwark state) already exists, so the log stays bounded."""
    prev = _existing(state_root).get((tier, target))
    if prev and bool(prev.get("added_to_bulwark")) == bool(added_to_bulwark):
        return prev
    rec = {
        "tier": tier, "target": target, "pattern": pattern, "reason": reason,
        "op": op, "layer": layer, "method": method, "armor_id": armor_id,
        "added_to_bulwark": bool(added_to_bulwark),
        "session_id": session_id, "at": time.time(),
    }
    p = flags_path(state_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def bulwark_rule_for(op: str, target: str) -> Optional[tuple[str, str]]:
    """Map a flagged op/target to a bulwark ``(layer, match)`` for a deny.

    exec.spawn -> exec <command>. Network/nav -> url <host><path> (or just the
    path when no host is known). Returns None when nothing is expressible.
    """
    op = str(op or "")
    target = str(target or "")
    if op == "exec.spawn":
        return ("exec", target) if target else None
    # network/nav/rhema: prefer host+path, fall back to path
    if "://" in target:
        parts = urlsplit(target)
        match = (parts.netloc + parts.path) or target
    else:
        match = target
    return ("url", match) if match else None


_SCOPE_HDR = "## armor: {armor}"


def add_to_bulwark(
    state_root: Path, *, op: str, target: str, tier: str,
    armor_id: Optional[str],
) -> bool:
    """Ensure a ``deny <layer> <match>`` rule exists under the active armor's
    scope (or ``## global`` when no armor). Returns True when the deny is
    present after the call (newly written or already there); False only when
    nothing deny-able could be expressed from the op/target."""
    lm = bulwark_rule_for(op, target)
    if lm is None:
        return False
    layer, match = lm
    line = f"deny {layer} {match}  # danger:{tier}:auto"
    bw_path = Path(state_root) / "bulwark.md"
    existing = bw_path.read_text() if bw_path.exists() else ""
    # Idempotency: same layer+match already denied anywhere in the file.
    needle = re.compile(
        rf"^\s*deny\s+{re.escape(layer)}\s+{re.escape(match)}(\s|$)",
        re.MULTILINE,
    )
    if needle.search(existing):
        return True
    header = "## global" if not armor_id else _SCOPE_HDR.format(armor=armor_id)
    hdr_re = re.compile(rf"^{re.escape(header)}\s*$", re.MULTILINE)
    if hdr_re.search(existing):
        new_text = hdr_re.sub(
            lambda m: m.group(0) + "\n" + line, existing, count=1)
    else:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        new_text = existing + sep + f"\n{header}\n{line}\n"
    bw_path.parent.mkdir(parents=True, exist_ok=True)
    bw_path.write_text(new_text)
    return True


def remove_from_bulwark(state_root: Path, *, op: str, target: str) -> bool:
    """Strip the auto-added ``deny <layer> <match>`` line for this op/target.
    Returns True if a line was removed."""
    lm = bulwark_rule_for(op, target)
    if lm is None:
        return False
    layer, match = lm
    bw_path = Path(state_root) / "bulwark.md"
    if not bw_path.exists():
        return False
    needle = re.compile(
        rf"^\s*deny\s+{re.escape(layer)}\s+{re.escape(match)}(\s|$)")
    lines = bw_path.read_text().splitlines()
    kept = [ln for ln in lines if not needle.match(ln)]
    if len(kept) == len(lines):
        return False
    bw_path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return True


def set_bulwark(
    state_root: Path, *, op: str, target: str, add: bool,
    armor_id: Optional[str],
) -> bool:
    """Operator toggle: add or remove this target's bulwark deny, preserving
    the flag's tier/pattern/reason. Returns the resulting bulwark state."""
    flag = next(
        (f for f in list_flags(state_root) if f.get("target") == target), None)
    tier = (flag or {}).get("tier") or ("irreversible" if add else "provable")
    if add:
        state = add_to_bulwark(
            state_root, op=op, target=target, tier=tier, armor_id=armor_id)
    else:
        remove_from_bulwark(state_root, op=op, target=target)
        state = False
    record_flag(
        state_root, tier=tier, target=target,
        pattern=(flag or {}).get("pattern", ""),
        reason=(flag or {}).get("reason", "operator toggle"),
        op=op, layer=(flag or {}).get("layer", ""),
        method=(flag or {}).get("method", ""),
        armor_id=armor_id, added_to_bulwark=state,
        session_id=(flag or {}).get("session_id"),
    )
    return state


def flag_and_maybe_block(
    state_root: Path, *, op: str, flag, armor_id: Optional[str],
    target: Optional[str] = None, session_id: Optional[str] = None,
) -> dict:
    """Record a DangerFlag and, for the irreversible tier, auto-append a
    bulwark deny. ``target`` overrides the flag's classified target (a path)
    with the original full target (e.g. host+path URL) for display and the
    bulwark rule. Recording and bulwark append are both idempotent."""
    tgt = target or flag.target
    added = False
    if flag.tier == "irreversible":
        try:
            added = add_to_bulwark(
                state_root, op=op, target=tgt, tier=flag.tier,
                armor_id=armor_id)
        except Exception:  # noqa: BLE001
            added = False
    return record_flag(
        state_root, tier=flag.tier, target=tgt, pattern=flag.pattern,
        reason=flag.reason, op=op,
        layer=getattr(flag, "layer", ""), method=getattr(flag, "method", ""),
        armor_id=armor_id, added_to_bulwark=added, session_id=session_id,
    )


__all__ = [
    "flags_path", "list_flags", "record_flag", "bulwark_rule_for",
    "add_to_bulwark", "remove_from_bulwark", "set_bulwark",
    "flag_and_maybe_block",
]
