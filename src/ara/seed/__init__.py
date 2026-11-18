"""Seed data shipped with ARA.

Contains the default-included artifacts that ARA installs into a state
root when the relevant subsystem first needs them. Today: the
blacksmith armor + the blacksmith-hammer weapon, copied on first
``ara forge new``.

Public API: :func:`ensure_seed` — idempotent copy from the wheel-bundled
seed tree into the operator's state root, plus an idempotent merge of
the armor's bulwark section into the global ``<state>/bulwark.md``.

Per the spec at ``docs/specs/blacksmith-hammer-v0.md``:

- Seeds are **included by default, not equipped by default.** Forge
  CLI handles equip/unequip lifecycle separately.
- Operator-customized seed dirs are never overwritten. A subsequent
  ARA upgrade that ships an updated seed leaves the operator's
  on-disk version alone.
- The global ``bulwark.md`` armor scope block uses sentinel markers
  so re-runs strip and re-apply cleanly.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_SEED_ROOT = Path(__file__).resolve().parent

# Marker tokens used to wrap a seeded armor's scope section in the
# global bulwark.md so we can find + strip + re-apply idempotently.
# Markers use bulwark-native '#'-comments so the parser ignores them
# (HTML-style '<!-- -->' would be parsed as malformed rules and emit
# bulwark_reload_error events on every reload). Operator content above
# and below the block is preserved exactly across strip+re-apply.
ARMOR_SCOPE_BEGIN_PREFIX = "# ARA-ARMOR-SCOPE-BEGIN"
ARMOR_SCOPE_END = "# ARA-ARMOR-SCOPE-END"


@dataclass(frozen=True)
class SeedReport:
    """What ensure_seed() did this call. All lists are paths relative to
    the state root; empty when nothing happened."""
    copied_armors: list[str] = field(default_factory=list)
    copied_weapons: list[str] = field(default_factory=list)
    bulwark_sections_applied: list[str] = field(default_factory=list)
    bulwark_sections_unchanged: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    installed_tempers: list[str] = field(default_factory=list)


def seed_root() -> Path:
    """Absolute path to the wheel-bundled seed tree
    (``<package>/seed/``). Tests + CLI inspect this; production code
    only needs :func:`ensure_seed`."""
    return _SEED_ROOT


def _copy_tree_if_missing(src: Path, dst: Path) -> bool:
    """Copy ``src`` → ``dst`` recursively iff ``dst`` doesn't exist.
    Returns True when the copy ran. False if ``dst`` was already
    present (operator customization preserved)."""
    if dst.exists():
        return False
    if not src.is_dir():
        return False
    shutil.copytree(src, dst)
    return True


def _find_line_start_marker(s: str, marker: str, cursor: int) -> int:
    """Find the next occurrence of ``marker`` that starts at the
    beginning of a line. Required so prose mentioning the token (e.g.,
    inside a comment block) doesn't trigger false matches."""
    pos = cursor
    while pos < len(s):
        idx = s.find(marker, pos)
        if idx < 0:
            return -1
        if idx == 0 or s[idx - 1] == "\n":
            return idx
        pos = idx + 1
    return -1


def _strip_armor_scope(text: str, armor_name: str) -> str:
    """Remove a ``# ARA-ARMOR-SCOPE-BEGIN <name>`` ...
    ``# ARA-ARMOR-SCOPE-END`` block (with the matching name) from
    ``text``. Operator content above and below the block is preserved
    exactly. Tolerates missing-end markers (drops to EOF) and
    multiple blocks for the same armor (strips all). Marker tokens
    must appear at the start of a line to be recognized."""
    begin_marker = f"{ARMOR_SCOPE_BEGIN_PREFIX} {armor_name}"
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        begin = _find_line_start_marker(text, begin_marker, cursor)
        if begin < 0:
            out.append(text[cursor:])
            break
        prefix = text[cursor:begin]
        # Trim a single trailing blank line so stripping doesn't leave
        # double-blanks behind.
        if prefix.endswith("\n\n"):
            prefix = prefix[:-1]
        out.append(prefix)
        end = _find_line_start_marker(text, ARMOR_SCOPE_END, begin)
        if end < 0:
            cursor = len(text)
            break
        cursor = end + len(ARMOR_SCOPE_END)
        if cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    return "".join(out)


def _wrap_armor_scope(armor_name: str, body: str, *, base: str) -> str:
    """Build the marker-wrapped block to append to bulwark.md."""
    body = body.rstrip("\n")
    if base.strip():
        prefix = "" if base.endswith("\n\n") else (
            "\n" if base.endswith("\n") else "\n\n"
        )
    else:
        prefix = ""
    return (
        f"{prefix}{ARMOR_SCOPE_BEGIN_PREFIX} {armor_name}\n"
        f"{body}\n"
        f"{ARMOR_SCOPE_END}\n"
    )


def _apply_armor_scope(
    state_bulwark: Path, armor_name: str, section_body: str,
) -> bool:
    """Strip any prior marker block for this armor and append a fresh
    one wrapping ``section_body``. Returns True when the file changed,
    False when it was already byte-identical to the desired state."""
    existing = state_bulwark.read_text() if state_bulwark.is_file() else ""
    base = _strip_armor_scope(existing, armor_name)
    merged = base + _wrap_armor_scope(armor_name, section_body, base=base)
    if merged == existing:
        return False
    state_bulwark.parent.mkdir(parents=True, exist_ok=True)
    state_bulwark.write_text(merged)
    return True


def ensure_seed(
    state_root: Path,
    *,
    armors: Iterable[str] = ("blacksmith",),
    weapons: Iterable[str] = ("blacksmith-hammer",),
) -> SeedReport:
    """Idempotently materialize seeded armors + weapons under
    ``state_root``, and merge each armor's bulwark scope section into
    the global ``<state_root>/bulwark.md``.

    Defaults seed exactly the artifacts referenced by the
    blacksmith-hammer spec; callers that ship additional seeds extend
    the iterables.

    Behaviors:

    - ``<state>/armors/<name>/``: copied from
      ``<package>/seed/armors/<name>/`` only when missing on disk.
    - ``<state>/weapons/<name>/``: same.
    - ``<state>/bulwark.md`` ``## armor: <name>`` section: read from
      the seed's ``bulwark-section.md`` and append-merged with marker
      comments. Re-runs strip + re-apply (so editing the seed file
      and re-installing picks up changes).
    - Seed armors with no ``bulwark-section.md`` skip the merge step.

    Returns a :class:`SeedReport` describing what changed.
    """
    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    report = SeedReport()

    armors_root = _SEED_ROOT / "armors"
    weapons_root = _SEED_ROOT / "weapons"
    state_bulwark = state_root / "bulwark.md"

    # ---- weapons (must come before armors so blacksmith's bulwark
    #      scope can mention weapon paths if wanted in future) ------
    state_weapons = state_root / "weapons"
    state_weapons.mkdir(parents=True, exist_ok=True)
    for name in weapons:
        src = weapons_root / name
        dst = state_weapons / name
        if _copy_tree_if_missing(src, dst):
            report.copied_weapons.append(name)
        elif src.is_dir():
            report.skipped_existing.append(f"weapons/{name}")

    # ---- armors --------------------------------------------------
    state_armors_dir = state_root / "armors"
    state_armors_dir.mkdir(parents=True, exist_ok=True)
    for name in armors:
        src = armors_root / name
        dst = state_armors_dir / name
        if _copy_tree_if_missing(src, dst):
            report.copied_armors.append(name)
        elif src.is_dir():
            report.skipped_existing.append(f"armors/{name}")

        # Apply bulwark scope section (separate from the dir copy:
        # always reconciles, even if the armor dir already exists).
        section_path = src / "bulwark-section.md"
        if section_path.is_file():
            section_body = section_path.read_text()
            if _apply_armor_scope(state_bulwark, name, section_body):
                report.bulwark_sections_applied.append(name)
            else:
                report.bulwark_sections_unchanged.append(name)

    # ---- baseline hardening temper (curated cross-cutting doctrines) ----
    # A temper is a small authored set, not forged output; ship the default so
    # it is available in the temper slot. Materialize (not equip) if absent.
    try:
        from ara import seed_temper, tempers
        if not tempers.bulwark_policy_path(
            state_root, seed_temper.BASELINE_TEMPER_ID,
        ).is_file():
            seed_temper.install_baseline_temper(state_root)
            report.installed_tempers.append(seed_temper.BASELINE_TEMPER_ID)
    except Exception as _exc:  # noqa: BLE001 — seeding a temper is best-effort
        pass

    return report


__all__ = [
    "SeedReport",
    "seed_root",
    "ensure_seed",
    "ARMOR_SCOPE_BEGIN_PREFIX",
    "ARMOR_SCOPE_END",
]
