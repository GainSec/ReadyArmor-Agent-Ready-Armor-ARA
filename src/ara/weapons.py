"""Equipped-weapon state — parallels :mod:`ara.armors` equip/unequip.

A weapon equip is the operator's declaration that a sealed weapon at
``<state_root>/weapons/<weapon_id>/`` should drive the supervisor's
forge-v1 :class:`EngagementWiring` for the active session.

State file: ``<state_root>/equipped_weapon`` containing the
weapon_id (one line, no trailing whitespace). Absent or empty file
means no weapon is equipped — the supervisor still runs but the
``channel_call`` IPC handler returns
``{type: error, error: "no_weapon_equipped"}`` until one is.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


_EQUIPPED_FILE = "equipped_weapon"


def equipped_weapon_file(state_root: Path) -> Path:
    return Path(state_root) / _EQUIPPED_FILE


def equipped_weapon_id(state_root: Path) -> Optional[str]:
    """Return the equipped weapon_id, or None if none equipped."""
    p = equipped_weapon_file(state_root)
    if not p.exists():
        return None
    name = p.read_text().strip()
    return name or None


def set_equipped_weapon(state_root: Path, weapon_id: Optional[str]) -> None:
    """Equip ``weapon_id`` (writes the state file) or unequip
    (when ``weapon_id is None``)."""
    p = equipped_weapon_file(state_root)
    if weapon_id is None:
        if p.exists():
            p.unlink()
        return
    Path(state_root).mkdir(parents=True, exist_ok=True)
    p.write_text(weapon_id.strip())


def weapon_dir(state_root: Path, weapon_id: str) -> Path:
    return Path(state_root) / "weapons" / weapon_id


def weapon_exists(state_root: Path, weapon_id: str) -> bool:
    return (weapon_dir(state_root, weapon_id) / "seal.json").is_file()


def list_sealed_weapons(state_root: Path) -> list[str]:
    """Return weapon_ids of weapons under ``<state_root>/weapons/``
    that have a ``seal.json``. Sorted alphabetically."""
    base = Path(state_root) / "weapons"
    if not base.is_dir():
        return []
    out: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if (child / "seal.json").is_file():
            out.append(child.name)
    return out


# ---------- ARA policy overlay (bulwark / WALLS / rhema) ----------
#
# Weapons layer ON TOP of ARA's standing config — they don't replace
# operator-set rules. Equip appends the weapon's policy text between
# sentinel markers in the operator's file; unequip strips between
# markers, leaving everything else untouched. Re-equipping (different
# weapon) strips any prior overlay first so policies don't stack.
#
# Markers use bulwark/markdown ``#``-comment style so the bulwark
# parser ignores them. Earlier versions used HTML comments
# (``<!-- ARA-WEAPON-OVERLAY-BEGIN ... -->``) which the bulwark parser
# treated as malformed rules and emitted ``bulwark_reload_error``
# events on every reload. The legacy HTML markers are still
# recognized by ``_strip_overlay`` for backward compatibility, so old
# state self-heals on the next equip cycle.
#
#   # ARA-WEAPON-OVERLAY-BEGIN <weapon_id>
#   ... weapon's rule text ...
#   # ARA-WEAPON-OVERLAY-END

_POLICY_FILES: list[tuple[str, str]] = [
    ("WALLS.md", "WALLS.md"),
    ("bulwark.md", "bulwark.md"),
    ("rhema.md", "rhema.md"),
]

# Current marker style (bulwark-comment-friendly).
_OVERLAY_BEGIN_PREFIX = "# ARA-WEAPON-OVERLAY-BEGIN"
_OVERLAY_END = "# ARA-WEAPON-OVERLAY-END"

# Legacy marker style — recognized by ``_strip_overlay`` so existing
# operator state migrates transparently on next equip. Never written
# by ``_wrap_overlay``.
_LEGACY_OVERLAY_BEGIN_PREFIX = "<!-- ARA-WEAPON-OVERLAY-BEGIN"
_LEGACY_OVERLAY_END = "<!-- ARA-WEAPON-OVERLAY-END -->"


def policy_dir_for(state_root: Path, weapon_id: str) -> Path:
    return weapon_dir(state_root, weapon_id) / "policy"


def _find_line_start_marker(s: str, marker: str, cursor: int) -> int:
    """Find the next occurrence of ``marker`` that starts at the
    beginning of a line (preceded by ``\\n`` or at file offset 0).
    Returns the offset, or -1 if none found.

    Required so prose / docstrings that mention the marker token
    inline (e.g., a bulwark.md header comment that documents the
    overlay format) don't get matched as real markers and cause
    premature strip termination.
    """
    pos = cursor
    while pos < len(s):
        idx = s.find(marker, pos)
        if idx < 0:
            return -1
        if idx == 0 or s[idx - 1] == "\n":
            return idx
        pos = idx + 1
    return -1


def _strip_overlay(text: str) -> str:
    """Remove every weapon overlay block from ``text``, leaving
    surrounding operator content untouched. Tolerates multiple
    overlays (strips all), missing-end markers (drops from BEGIN to
    EOF), and the legacy HTML marker style
    (``<!-- ARA-WEAPON-OVERLAY-BEGIN ... -->``) so old operator state
    self-heals on the next equip cycle.

    Marker tokens must appear at the start of a line to be recognized
    (preceded by a newline or at file offset 0). Inline mentions of
    the marker text inside operator prose / weapon docstrings are
    therefore not matched.
    """
    # Find the next overlay block from any of the supported marker
    # styles. Returns (begin_offset, end_marker_token) or None.
    def _next_block(s: str, cursor: int):
        candidates: list[tuple[int, str]] = []
        for begin_tok, end_tok in (
            (_OVERLAY_BEGIN_PREFIX, _OVERLAY_END),
            (_LEGACY_OVERLAY_BEGIN_PREFIX, _LEGACY_OVERLAY_END),
        ):
            pos = _find_line_start_marker(s, begin_tok, cursor)
            if pos >= 0:
                candidates.append((pos, end_tok))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        return candidates[0]

    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        nxt = _next_block(text, cursor)
        if nxt is None:
            out.append(text[cursor:])
            break
        begin, end_tok = nxt
        # Keep text up to the begin marker (trim trailing newline so
        # we don't leave a stray blank line behind on strip).
        prefix = text[cursor:begin]
        if prefix.endswith("\n\n"):
            prefix = prefix[:-1]
        out.append(prefix)
        end = _find_line_start_marker(text, end_tok, begin)
        if end < 0:
            # Malformed: BEGIN without matching END. Drop to EOF.
            cursor = len(text)
            break
        cursor = end + len(end_tok)
        # Skip a trailing newline immediately after END for cleanliness
        if cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    return "".join(out)


def _wrap_overlay(weapon_id: str, body: str, *, base: str) -> str:
    """Build the overlay block to append. ``base`` is the operator's
    text being appended-to; we use it to decide leading whitespace —
    no leading newlines when the base is empty (so the file starts
    cleanly with the begin marker), one blank line of separation when
    the base has content.
    """
    body = body.rstrip("\n")
    if base.strip():
        # Ensure exactly one blank line between base and overlay
        prefix = "" if base.endswith("\n\n") else (
            "\n" if base.endswith("\n") else "\n\n"
        )
    else:
        prefix = ""
    return (
        f"{prefix}{_OVERLAY_BEGIN_PREFIX} {weapon_id}\n"
        f"{body}\n"
        f"{_OVERLAY_END}\n"
    )


def equip_policy(state_root: Path, weapon_id: str) -> dict:
    """Append the weapon's policy text as overlay sections in the
    operator's existing files. Operator's standing rules are NOT
    modified outside the overlay markers.

    For each ``WALLS.md``/``bulwark.md``/``rhema.md`` shipped under
    ``<weapon>/policy/``: any prior overlay (from a previous weapon)
    is first stripped, then the new weapon's body is appended between
    fresh markers. Files the weapon doesn't ship are left untouched.

    Existing supervisor watchers (``_watch_walls``, ``_watch_bulwark``)
    detect the mtime change and reload the merged base+overlay.

    Returns ``{overlaid: [...], kept: [...]}``.
    """
    state_root = Path(state_root)
    pdir = policy_dir_for(state_root, weapon_id)
    overlaid: list[str] = []
    kept: list[str] = []
    if not pdir.is_dir():
        return {"overlaid": overlaid,
                "kept": [n for n, _ in _POLICY_FILES]}
    state_root.mkdir(parents=True, exist_ok=True)
    for state_name, weapon_name in _POLICY_FILES:
        src = pdir / weapon_name
        if not src.is_file():
            kept.append(state_name)
            continue
        weapon_body = src.read_text()
        if not weapon_body.strip():
            kept.append(state_name)
            continue
        dst = state_root / state_name
        existing = dst.read_text() if dst.is_file() else ""
        # Strip any prior overlay (from a previously-equipped weapon
        # OR a re-equip of the same one) so we don't stack.
        base = _strip_overlay(existing)
        merged = base + _wrap_overlay(weapon_id, weapon_body, base=base)
        dst.write_text(merged)
        overlaid.append(state_name)
    return {"overlaid": overlaid, "kept": kept}


def unequip_policy(state_root: Path) -> dict:
    """Strip every weapon overlay section from the operator's policy
    files. Operator's standing rules are preserved unchanged.

    Files that contained ONLY an overlay (no operator base content)
    end up empty — the file is left in place as an empty document so
    the operator can see the slot exists.
    """
    state_root = Path(state_root)
    stripped: list[str] = []
    untouched: list[str] = []
    for state_name, _ in _POLICY_FILES:
        live = state_root / state_name
        if not live.is_file():
            untouched.append(state_name)
            continue
        before = live.read_text()
        after = _strip_overlay(before)
        if after != before:
            live.write_text(after)
            stripped.append(state_name)
        else:
            untouched.append(state_name)
    return {"stripped": stripped, "untouched": untouched}
