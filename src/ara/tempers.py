"""Temper as a first-class, hot-swappable equipment slot.

A temper is a policy-hardening pack distilled from a forge's source knowledge
(WSTG, pentest reports, standards) into grounded guardrails. Unlike armor
(identity), weapon (capability), or trim (engagement), a temper is orthogonal:
it carries its OWN overlay files and is equipped independently of the rest of
the loadout. Multiple tempers STACK — each merges its own marked block, and
they don't conflict.

Layout (state-local, editable):
  <state>/tempers/<temper_id>/
    meta.json           name, source_forge, created_at, counts
    policy/bulwark.md    its own bulwark rules (## global + rule lines)
    policy/absolute.md   its own doctrine/prompt overlay (optional)
  <state>/active-tempers  newline list of equipped temper_ids (stacking)

Equip merges ``policy/bulwark.md`` into ``<state>/bulwark.md`` under
``# ARA-TEMPER-OVERLAY-BEGIN <id>`` … ``# ARA-TEMPER-OVERLAY-END`` markers —
the same overlay machinery weapons use. Unequip strips that block cleanly, so
swapping temper A for temper B changes the live policy set with no residue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import temper_ingest

TEMPERS_DIR = "tempers"
ACTIVE_TEMPERS_FILE = "active-tempers"

_OVERLAY_BEGIN_PREFIX = "# ARA-TEMPER-OVERLAY-BEGIN"
_OVERLAY_END = "# ARA-TEMPER-OVERLAY-END"


# --- paths -----------------------------------------------------------------

def temper_dir(state_root: Path, temper_id: str) -> Path:
    return Path(state_root) / TEMPERS_DIR / temper_id


def policy_dir(state_root: Path, temper_id: str) -> Path:
    return temper_dir(state_root, temper_id) / "policy"


def bulwark_policy_path(state_root: Path, temper_id: str) -> Path:
    return policy_dir(state_root, temper_id) / "bulwark.md"


def absolute_policy_path(state_root: Path, temper_id: str) -> Path:
    return policy_dir(state_root, temper_id) / "absolute.md"


def meta_path(state_root: Path, temper_id: str) -> Path:
    return temper_dir(state_root, temper_id) / "meta.json"


# --- block merge (mirrors the weapon-overlay semantics) --------------------

def _find_line_start(text: str, marker: str, cursor: int) -> int:
    pos = cursor
    while pos < len(text):
        idx = text.find(marker, pos)
        if idx < 0:
            return -1
        if idx == 0 or text[idx - 1] == "\n":
            return idx
        pos = idx + 1
    return -1


def _begin(temper_id: str) -> str:
    return f"{_OVERLAY_BEGIN_PREFIX} {temper_id}"


def _strip_block(text: str, temper_id: str) -> str:
    begin_marker = _begin(temper_id)
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        begin = _find_line_start(text, begin_marker, cursor)
        if begin < 0:
            out.append(text[cursor:])
            break
        prefix = text[cursor:begin]
        if prefix.endswith("\n\n"):
            prefix = prefix[:-1]
        out.append(prefix)
        end = _find_line_start(text, _OVERLAY_END, begin)
        if end < 0:
            cursor = len(text)
            break
        cursor = end + len(_OVERLAY_END)
        if cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    return "".join(out)


def _merge_block(path: Path, temper_id: str, body: str) -> None:
    existing = path.read_text() if path.is_file() else ""
    base = _strip_block(existing, temper_id)
    merged = base
    if body.strip():
        if base.strip():
            sep = "" if base.endswith("\n\n") else ("\n" if base.endswith("\n") else "\n\n")
        else:
            sep = ""
        merged = base + f"{sep}{_begin(temper_id)}\n{body.rstrip()}\n{_OVERLAY_END}\n"
    if merged != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(merged)


# --- ingest (write the temper's own policy files) --------------------------

def ingest(
    state_root: Path, temper_id: str, items,
    *, name: str | None = None, source_forge: str | None = None,
    source_armor: str | None = None,
) -> dict:
    """Materialize ``items`` into this temper's own policy files.

    Each item is routed via :func:`temper_ingest.plan_item`; bulwark-home rules
    are written to ``policy/bulwark.md`` (with a self-contained ``## global``
    heading so they register), absolute-home bodies to ``policy/absolute.md``.
    Returns a summary of what landed and how it enforces.
    """
    state_root = Path(state_root)
    plans = [temper_ingest.plan_item(item) for item in items]
    bulwark_lines = [
        p.rule_text for p in plans if p.home == temper_ingest.BULWARK_HOME
    ]
    absolute_bodies = [
        p.rule_text for p in plans
        if p.home == temper_ingest.ABSOLUTE_HOME and p.rule_text.strip()
    ]

    pdir = policy_dir(state_root, temper_id)
    pdir.mkdir(parents=True, exist_ok=True)

    bw_path = bulwark_policy_path(state_root, temper_id)
    if bulwark_lines:
        body = "## global\n" + "\n".join(
            line.rstrip() for line in bulwark_lines if line.strip()
        ).rstrip() + "\n"
        bw_path.write_text(body)
    elif bw_path.exists():
        bw_path.unlink()

    abs_path = absolute_policy_path(state_root, temper_id)
    if absolute_bodies:
        abs_path.write_text(
            "\n\n".join(b.rstrip() for b in absolute_bodies).rstrip() + "\n"
        )
    elif abs_path.exists():
        abs_path.unlink()

    meta = {
        "temper_id": temper_id,
        "name": name or temper_id,
        "source_forge": source_forge,
        "source_armor": source_armor,
        "bulwark_rules": len(bulwark_lines),
        "absolute_bodies": len(absolute_bodies),
        "enforced": sorted({p.destination for p in plans if p.enforced}),
        "advisory": sorted({p.destination for p in plans if not p.enforced}),
    }
    meta_path(state_root, temper_id).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n"
    )
    return meta


# --- discovery + active set (stacking) -------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return s[:48] or "temper"


def new_temper_id(state_root: Path, name: str) -> str:
    """A fresh, unique temper id derived from ``name``. Each forge run yields a
    distinct temper (like every other equipment), so an existing slug is
    suffixed ``-2``, ``-3``, … rather than overwritten."""
    base = _slug(name)
    existing = set(list_tempers(state_root))
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def list_tempers(state_root: Path) -> list[str]:
    base = Path(state_root) / TEMPERS_DIR
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if (p / "meta.json").is_file())


def load_meta(state_root: Path, temper_id: str) -> dict | None:
    p = meta_path(state_root, temper_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _active_path(state_root: Path) -> Path:
    return Path(state_root) / ACTIVE_TEMPERS_FILE


def active_tempers(state_root: Path) -> list[str]:
    """Equipped temper ids, in stack order. Filtered to those still present."""
    p = _active_path(state_root)
    if not p.is_file():
        return []
    ids: list[str] = []
    present = set(list_tempers(state_root))
    for line in p.read_text().splitlines():
        tid = line.strip()
        if tid and tid in present and tid not in ids:
            ids.append(tid)
    return ids


def _write_active(state_root: Path, ids: list[str]) -> None:
    p = _active_path(state_root)
    if ids:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(ids) + "\n")
    elif p.exists():
        p.unlink()


def is_equipped(state_root: Path, temper_id: str) -> bool:
    return temper_id in active_tempers(state_root)


# --- equip / unequip (overlay merge into live policy) ----------------------

def equip(state_root: Path, temper_id: str) -> dict:
    """Merge this temper's overlay into the live ``<state>/bulwark.md`` and add
    it to the active stack. Idempotent."""
    state_root = Path(state_root)
    if load_meta(state_root, temper_id) is None:
        raise FileNotFoundError(f"temper {temper_id!r} not found")
    bw_src = bulwark_policy_path(state_root, temper_id)
    body = bw_src.read_text() if bw_src.is_file() else ""
    _merge_block(state_root / "bulwark.md", temper_id, body)

    ids = active_tempers(state_root)
    if temper_id not in ids:
        ids.append(temper_id)
        _write_active(state_root, ids)
    return {"temper_id": temper_id, "equipped": True, "active_tempers": ids}


def unequip(state_root: Path, temper_id: str) -> dict:
    """Strip this temper's overlay from live policy and drop it from the stack."""
    state_root = Path(state_root)
    bw_path = state_root / "bulwark.md"
    if bw_path.is_file():
        stripped = _strip_block(bw_path.read_text(), temper_id)
        bw_path.write_text(stripped)
    ids = [t for t in active_tempers(state_root) if t != temper_id]
    _write_active(state_root, ids)
    return {"temper_id": temper_id, "equipped": False, "active_tempers": ids}
