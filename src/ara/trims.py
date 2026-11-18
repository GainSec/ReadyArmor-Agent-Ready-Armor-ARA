"""Armor trims — engagement-scoped armor overlays.

A trim is to armor what the weapon-policy overlay is to ``bulwark.md``:
additive, marker-wrapped, hot-reload-applied, fully reversible. Trims
carry the *where / when / against whom / under what ROE* that the
parent armor doesn't know.

See ``docs/specs/armor-trim-v0.md`` for the spec; this module is the
v0 implementation per §11 (Q10-approved).

Layout::

    <state>/armors/<armor>/trims/<trim_id>/
      meta.json                 -- engagement metadata + flags
      bulwark-section.md        -- DSL merged into <state>/bulwark.md
      absolute-trim.md          -- prose appended to active absolute
      source/                   -- (optional) derivation inputs
      uploads/                  -- (optional) operator file uploads
    <state>/armors/<armor>/trims-active   -- single line, applied trim_id

Marker styles (line-start anchored — prose mentions don't false-strip):

  - bulwark.md:   ``# ARA-ARMOR-TRIM-BEGIN <armor> <trim_id>`` ... ``# ARA-ARMOR-TRIM-END``
  - active absolute:  ``<!-- ARA-ARMOR-TRIM-ABSOLUTE-BEGIN <trim_id> -->`` ... ``<!-- ARA-ARMOR-TRIM-ABSOLUTE-END -->``
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ara import armors as armors_mod
from ara import tokens as tokens_mod


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

_TRIM_BEGIN_PREFIX = "# ARA-ARMOR-TRIM-BEGIN"
_TRIM_END = "# ARA-ARMOR-TRIM-END"

_ABS_TRIM_BEGIN_PREFIX = "<!-- ARA-ARMOR-TRIM-ABSOLUTE-BEGIN"
_ABS_TRIM_END = "<!-- ARA-ARMOR-TRIM-ABSOLUTE-END -->"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TrimMeta:
    trim_id: str
    armor: str
    weapon_ref: str | None = None
    engagement_id: str | None = None
    target: str = ""
    test_window_start: str | None = None         # ISO-8601 UTC
    test_window_end: str | None = None
    expires_at: str | None = None                # mirrors test_window_end by default
    channels: list[str] = field(default_factory=list)
    absolute_enforce: bool = True
    operator_id: str = ""
    authorization: dict = field(default_factory=dict)
    operator_prefs: dict = field(default_factory=dict)
    declared_tokens: list[str] = field(default_factory=list)
    upload_max_mb: float | None = None
    derived_from: str | None = None              # null if hand-authored
    release_on_armor_unequip: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0

    # ── v0.1 amendment fields ──────────────────────────────────────
    schema_version: str = "0.1"
    env: str = "nonprod"                         # "prod" | "nonprod" | "mixed"
    assets: list[dict] = field(default_factory=list)   # required iff env="mixed"
    posture: str = "blackbox"                    # "blackbox" | "graybox" | "whitebox"
    active_testing: bool = False
    depth: str = "recon"                         # informational only — recon|scan|vc|exploitation|post_exploit
    tod_allowed: list[dict] = field(default_factory=list)
    rate_limits: dict = field(default_factory=dict)
    stop_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrimMeta":
        return cls(
            trim_id=d["trim_id"],
            armor=d["armor"],
            weapon_ref=d.get("weapon_ref"),
            engagement_id=d.get("engagement_id"),
            target=d.get("target") or "",
            test_window_start=d.get("test_window_start"),
            test_window_end=d.get("test_window_end"),
            expires_at=d.get("expires_at"),
            channels=list(d.get("channels") or []),
            absolute_enforce=bool(d.get("absolute_enforce", True)),
            operator_id=d.get("operator_id") or "",
            authorization=dict(d.get("authorization") or {}),
            operator_prefs=dict(d.get("operator_prefs") or {}),
            declared_tokens=list(d.get("declared_tokens") or []),
            upload_max_mb=d.get("upload_max_mb"),
            derived_from=d.get("derived_from"),
            release_on_armor_unequip=bool(d.get("release_on_armor_unequip", True)),
            created_at=float(d.get("created_at") or 0.0),
            updated_at=float(d.get("updated_at") or 0.0),
            # v0.1 — defaults preserve v0 behavior when fields are absent
            schema_version=d.get("schema_version") or "0.1",
            env=d.get("env") or "nonprod",
            assets=list(d.get("assets") or []),
            posture=d.get("posture") or "blackbox",
            active_testing=bool(d.get("active_testing", False)),
            depth=d.get("depth") or "recon",
            tod_allowed=list(d.get("tod_allowed") or []),
            rate_limits=dict(d.get("rate_limits") or {}),
            stop_conditions=list(d.get("stop_conditions") or []),
        )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def trims_dir(state_root: Path, armor: str) -> Path:
    return Path(state_root) / "armors" / armor / "trims"


def trim_dir(state_root: Path, armor: str, trim_id: str) -> Path:
    return trims_dir(state_root, armor) / trim_id


def trim_meta_path(state_root: Path, armor: str, trim_id: str) -> Path:
    return trim_dir(state_root, armor, trim_id) / "meta.json"


def trim_bulwark_section_path(state_root: Path, armor: str, trim_id: str) -> Path:
    return trim_dir(state_root, armor, trim_id) / "bulwark-section.md"


def trim_absolute_path(state_root: Path, armor: str, trim_id: str) -> Path:
    return trim_dir(state_root, armor, trim_id) / "absolute-trim.md"


def active_trim_path(state_root: Path, armor: str) -> Path:
    return Path(state_root) / "armors" / armor / "trims-active"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_TRIM_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,63}$")


class InvalidTrimId(ValueError):
    pass


def _validate_trim_id(trim_id: str) -> str:
    if not _TRIM_ID_RE.match(trim_id):
        raise InvalidTrimId(
            f"invalid trim id {trim_id!r}: must start with alnum and contain"
            f" only alnum/dash/underscore/dot, max 64 chars"
        )
    return trim_id


# ---------------------------------------------------------------------------
# Marker matching (line-start anchored — prose mentions don't false-strip)
# ---------------------------------------------------------------------------

def _find_line_start_marker(s: str, marker: str, cursor: int) -> int:
    """Find the next occurrence of ``marker`` at start of a line."""
    pos = cursor
    while pos < len(s):
        idx = s.find(marker, pos)
        if idx < 0:
            return -1
        if idx == 0 or s[idx - 1] == "\n":
            return idx
        pos = idx + 1
    return -1


def _strip_trim_block(text: str, armor: str, trim_id: str) -> str:
    """Remove the ``# ARA-ARMOR-TRIM-BEGIN <armor> <trim_id>`` ... ``END``
    block. Operator content above and below preserved exactly.
    """
    begin_marker = f"{_TRIM_BEGIN_PREFIX} {armor} {trim_id}"
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        begin = _find_line_start_marker(text, begin_marker, cursor)
        if begin < 0:
            out.append(text[cursor:])
            break
        prefix = text[cursor:begin]
        if prefix.endswith("\n\n"):
            prefix = prefix[:-1]
        out.append(prefix)
        end = _find_line_start_marker(text, _TRIM_END, begin)
        if end < 0:
            cursor = len(text)
            break
        cursor = end + len(_TRIM_END)
        if cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    return "".join(out)


def _strip_all_armor_trim_blocks(text: str, armor: str) -> str:
    """Remove EVERY ``# ARA-ARMOR-TRIM-BEGIN <armor> <any-trim>`` ... END block
    for this armor. Enforces the single-trim-per-armor invariant and cleans
    orphaned blocks left by a trim that was activated but never released (the
    exact desync that silently dropped an engagement's net scope)."""
    begin_prefix = f"{_TRIM_BEGIN_PREFIX} {armor} "
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        begin = _find_line_start_marker(text, begin_prefix, cursor)
        if begin < 0:
            out.append(text[cursor:])
            break
        prefix = text[cursor:begin]
        if prefix.endswith("\n\n"):
            prefix = prefix[:-1]
        out.append(prefix)
        end = _find_line_start_marker(text, _TRIM_END, begin)
        if end < 0:
            cursor = len(text)
            break
        cursor = end + len(_TRIM_END)
        if cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    return "".join(out)


def _wrap_trim_block(armor: str, trim_id: str, body: str, *, base: str) -> str:
    body = body.rstrip("\n")
    if base.strip():
        prefix = "" if base.endswith("\n\n") else (
            "\n" if base.endswith("\n") else "\n\n"
        )
    else:
        prefix = ""
    return (
        f"{prefix}{_TRIM_BEGIN_PREFIX} {armor} {trim_id}\n"
        f"{body}\n"
        f"{_TRIM_END}\n"
    )


def _strip_absolute_trim(text: str, trim_id: str) -> str:
    """Remove ``<!-- ARA-ARMOR-TRIM-ABSOLUTE-BEGIN <trim_id> -->`` ...
    ``END`` from absolute prose."""
    begin_marker = f"{_ABS_TRIM_BEGIN_PREFIX} {trim_id} -->"
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        begin = _find_line_start_marker(text, begin_marker, cursor)
        if begin < 0:
            out.append(text[cursor:])
            break
        prefix = text[cursor:begin]
        if prefix.endswith("\n\n"):
            prefix = prefix[:-1]
        out.append(prefix)
        end = _find_line_start_marker(text, _ABS_TRIM_END, begin)
        if end < 0:
            cursor = len(text)
            break
        cursor = end + len(_ABS_TRIM_END)
        if cursor < len(text) and text[cursor] == "\n":
            cursor += 1
    return "".join(out)


def _wrap_absolute_trim(trim_id: str, body: str, *, base: str) -> str:
    body = body.rstrip("\n")
    if base.strip():
        prefix = "" if base.endswith("\n\n") else (
            "\n" if base.endswith("\n") else "\n\n"
        )
    else:
        prefix = ""
    return (
        f"{prefix}{_ABS_TRIM_BEGIN_PREFIX} {trim_id} -->\n"
        f"{body}\n"
        f"{_ABS_TRIM_END}\n"
    )


# ---------------------------------------------------------------------------
# Listing / loading
# ---------------------------------------------------------------------------

def list_trims(state_root: Path, armor: str) -> list[TrimMeta]:
    """Every trim under ``<state>/armors/<armor>/trims/``, sorted by id."""
    state_root = Path(state_root)
    d = trims_dir(state_root, armor)
    if not d.is_dir():
        return []
    out: list[TrimMeta] = []
    for entry in sorted(d.iterdir()):
        if not entry.is_dir():
            continue
        meta_p = entry / "meta.json"
        if not meta_p.is_file():
            continue
        try:
            out.append(TrimMeta.from_dict(json.loads(meta_p.read_text())))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def list_all_trims(state_root: Path) -> list[TrimMeta]:
    """Every trim across every armor (per Q2: armory entry gate lists
    cross-armor)."""
    state_root = Path(state_root)
    armors_root = state_root / "armors"
    if not armors_root.is_dir():
        return []
    out: list[TrimMeta] = []
    for armor_dir in sorted(armors_root.iterdir()):
        if not armor_dir.is_dir():
            continue
        out.extend(list_trims(state_root, armor_dir.name))
    return out


def load_trim(
    state_root: Path, armor: str, trim_id: str,
) -> TrimMeta | None:
    p = trim_meta_path(Path(state_root), armor, trim_id)
    if not p.is_file():
        return None
    try:
        return TrimMeta.from_dict(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def active_trim_id(state_root: Path, armor: str) -> str | None:
    p = active_trim_path(Path(state_root), armor)
    if not p.is_file():
        return None
    txt = p.read_text().strip()
    return txt or None


def _set_active_trim(state_root: Path, armor: str, trim_id: str | None) -> None:
    p = active_trim_path(Path(state_root), armor)
    p.parent.mkdir(parents=True, exist_ok=True)
    if trim_id:
        p.write_text(trim_id + "\n")
    elif p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Create / delete
# ---------------------------------------------------------------------------

class TrimNotFound(Exception):
    pass


class TrimCurrentlyApplied(Exception):
    pass


def new_trim(
    state_root: Path, armor: str, trim_id: str, *,
    target: str = "",
    operator_id: str = "",
    weapon_ref: str | None = None,
    engagement_id: str | None = None,
) -> TrimMeta:
    """Create an empty trim dir + minimal meta. Operator authors the
    bulwark/absolute files manually (or via ``derive_trim``)."""
    state_root = Path(state_root)
    _validate_trim_id(trim_id)
    if armors_mod.load_armor(state_root, armor) is None:
        raise FileNotFoundError(f"armor {armor!r} not found at {state_root}")
    d = trim_dir(state_root, armor, trim_id)
    if d.exists():
        raise FileExistsError(f"trim {trim_id!r} already exists for armor {armor!r}")
    d.mkdir(parents=True, exist_ok=False)
    now = time.time()
    meta = TrimMeta(
        trim_id=trim_id,
        armor=armor,
        weapon_ref=weapon_ref,
        engagement_id=engagement_id,
        target=target,
        operator_id=operator_id,
        created_at=now,
        updated_at=now,
    )
    save_meta(state_root, meta)
    # Empty placeholder files so operator has something to edit
    trim_bulwark_section_path(state_root, armor, trim_id).write_text(
        f"## armor: {armor}\n# (trim {trim_id} — add allow/deny rules here)\n"
    )
    trim_absolute_path(state_root, armor, trim_id).write_text(
        f"## Engagement: {target or trim_id}\n"
    )
    return meta


def save_meta(state_root: Path, meta: TrimMeta) -> Path:
    p = trim_meta_path(Path(state_root), meta.armor, meta.trim_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    meta.updated_at = time.time()
    if not meta.created_at:
        meta.created_at = meta.updated_at
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(meta.to_dict(), sort_keys=True, indent=2))
    os.replace(tmp, p)
    return p


def refresh_trim_window(
    state_root: Path,
    armor: str,
    trim_id: str,
    *,
    start: str | None,
    end: str | None,
    apply_if_active: bool = False,
) -> TrimMeta:
    """Refresh a derived trim's active window and materialized prompt text.

    Updating only ``meta.json`` leaves stale ``absolute-trim.md`` prose in the
    active armor absolute. This helper keeps all window-bearing artifacts in
    step for task-run refreshes.
    """
    state_root = Path(state_root)
    meta = load_trim(state_root, armor, trim_id)
    if meta is None:
        raise TrimNotFound(f"trim {trim_id!r} under armor {armor!r} not found")
    meta.test_window_start = start
    meta.test_window_end = end
    meta.expires_at = end
    save_meta(state_root, meta)
    _rewrite_materialized_absolute_from_meta(state_root, meta)
    if apply_if_active and active_trim_id(state_root, armor) == trim_id:
        apply_trim(state_root, armor, trim_id)
    return meta


def _rewrite_materialized_absolute_from_meta(
    state_root: Path,
    meta: TrimMeta,
) -> None:
    d = trim_dir(state_root, meta.armor, meta.trim_id)
    source_dir = d / "source"
    source_path = source_dir / "engagement.json"
    engagement: dict
    if source_path.is_file():
        try:
            loaded = json.loads(source_path.read_text())
            engagement = loaded if isinstance(loaded, dict) else {}
        except Exception:
            engagement = {}
    else:
        engagement = {}

    target = dict(engagement.get("target") or {})
    if meta.target and not target.get("primary"):
        target["primary"] = meta.target
    engagement["target"] = target
    test_window = dict(engagement.get("test_window") or {})
    if meta.test_window_start is not None:
        test_window["start"] = meta.test_window_start
    if meta.test_window_end is not None:
        test_window["end"] = meta.test_window_end
    if meta.tod_allowed:
        test_window["tod_allowed"] = meta.tod_allowed
    engagement["test_window"] = test_window
    if meta.engagement_id and not engagement.get("engagement_id"):
        engagement["engagement_id"] = meta.engagement_id
    if meta.operator_id and not engagement.get("operator_id"):
        engagement["operator_id"] = meta.operator_id
    if meta.authorization and not engagement.get("authorization"):
        engagement["authorization"] = meta.authorization
    if meta.operator_prefs and not engagement.get("operator_prefs"):
        engagement["operator_prefs"] = meta.operator_prefs
    engagement["active_testing"] = meta.active_testing
    engagement["posture"] = meta.posture
    engagement["depth"] = meta.depth
    if meta.rate_limits:
        engagement["rate_limits"] = meta.rate_limits
    if meta.stop_conditions:
        engagement["stop_conditions"] = meta.stop_conditions
    if meta.channels and not engagement.get("delivery_channels"):
        engagement["delivery_channels"] = meta.channels

    primary = target.get("primary") or meta.target or ""
    absolute_body = _compose_absolute_trim(
        primary=primary,
        scope_allows=list(target.get("scope_allows") or []),
        oos=list(target.get("oos") or []),
        test_window=test_window,
        tod_allowed=list(test_window.get("tod_allowed") or meta.tod_allowed or []),
        operator_id=engagement.get("operator_id") or meta.operator_id,
        authorization=dict(engagement.get("authorization") or meta.authorization or {}),
        prefs=dict(engagement.get("operator_prefs") or meta.operator_prefs or {}),
        misc=engagement.get("misc") or "",
        env=meta.env,
        posture=meta.posture,
        active_testing=meta.active_testing,
        depth=meta.depth,
        rate_limits=meta.rate_limits,
        stop_conditions=meta.stop_conditions,
        engagement_id=meta.engagement_id,
        populated_mat_classes=[],
    )
    trim_absolute_path(state_root, meta.armor, meta.trim_id).write_text(absolute_body)
    source_dir.mkdir(exist_ok=True)
    source_path.write_text(json.dumps(engagement, sort_keys=True, indent=2))


def delete_trim(state_root: Path, armor: str, trim_id: str) -> bool:
    state_root = Path(state_root)
    if active_trim_id(state_root, armor) == trim_id:
        raise TrimCurrentlyApplied(
            f"trim {trim_id!r} is currently applied; release before deleting"
        )
    d = trim_dir(state_root, armor, trim_id)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# ---------------------------------------------------------------------------
# TTL injection
# ---------------------------------------------------------------------------

# Match the action token at the start of a rule line so we can inject
# `ttl <duration>` between action and layer. Uses the same shape Bulwark
# parses: <action>[ <modifier>]* <layer>[:<sub>] <match>.
_RULE_LINE_RE = re.compile(
    r"^(\s*)(allow|deny|approval|rewrite|shadow_rewrite)(\s+)(.*)$",
)
# If the rule already has a `ttl <duration>` modifier, capture it.
_TTL_MOD_RE = re.compile(r"\bttl\s+(\d+[smhdw])\b")


def _seconds_from_duration(s: str) -> int:
    if not s:
        return 0
    m = re.match(r"^(\d+)([smhdw])$", s)
    if not m:
        return 0
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def _duration_from_seconds(seconds: float) -> str:
    """Return the largest exact unit that fits, falling back to seconds."""
    seconds = max(1, int(seconds))
    for unit, mult in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % mult == 0:
            return f"{seconds // mult}{unit}"
    return f"{seconds}s"


def _expires_at_to_epoch(expires_at: str | None) -> float | None:
    if not expires_at:
        return None
    try:
        return datetime.strptime(
            expires_at, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _inject_ttl_into_rules(
    body: str, expires_at: str | None, *, now: float | None = None,
) -> str:
    """Inject ``ttl <duration>`` into every rule line based on
    ``expires_at`` − now. Per Q11: min(operator_ttl, engagement_ttl) —
    if the rule already has a manual ttl, keep it iff it's shorter.
    No-op when ``expires_at`` is None or already past.
    """
    end_epoch = _expires_at_to_epoch(expires_at)
    if end_epoch is None:
        return body
    n = now if now is not None else time.time()
    seconds_left = int(end_epoch - n)
    if seconds_left <= 0:
        return body
    eng_dur = _duration_from_seconds(seconds_left)

    out_lines: list[str] = []
    for raw in body.splitlines():
        m = _RULE_LINE_RE.match(raw)
        if not m:
            out_lines.append(raw)
            continue
        indent, action, gap, rest = m.groups()
        # Deny rules are the safe default — never TTL them. Expiring a
        # `deny net *:*` would silently re-open egress when the window ends
        # (fail-unsafe). Only allow/approval rules carry engagement TTL, so
        # stale *access* auto-drops while the default-deny persists until the
        # whole trim block is released.
        if action.strip().lower().startswith("deny"):
            out_lines.append(raw)
            continue
        # Existing manual ttl?
        existing = _TTL_MOD_RE.search(rest)
        if existing:
            existing_seconds = _seconds_from_duration(existing.group(1))
            # Per Q11: min wins
            if existing_seconds and existing_seconds <= seconds_left:
                out_lines.append(raw)
                continue
            # Replace the existing ttl with the engagement one
            new_rest = _TTL_MOD_RE.sub(f"ttl {eng_dur}", rest, count=1)
            out_lines.append(f"{indent}{action}{gap}{new_rest}")
        else:
            # Insert ttl right after the action
            out_lines.append(f"{indent}{action} ttl {eng_dur}{gap}{rest}")
    return "\n".join(out_lines) + ("\n" if body.endswith("\n") else "")


# ---------------------------------------------------------------------------
# Apply / release
# ---------------------------------------------------------------------------

def apply_trim(
    state_root: Path, armor: str, trim_id: str,
    *, now: float | None = None,
) -> dict:
    """Merge trim's bulwark-section + absolute-trim into the running
    state. Idempotent: re-applying strips any prior block first.

    Per spec §8 (Q2-approved): single trim active per armor at a
    time. Refuses to apply if a different trim is already applied.
    """
    state_root = Path(state_root)
    meta = load_trim(state_root, armor, trim_id)
    if meta is None:
        raise TrimNotFound(f"trim {trim_id!r} under armor {armor!r} not found")

    # Single-trim-per-armor enforcement (§8 v0)
    current = active_trim_id(state_root, armor)
    if current and current != trim_id:
        raise TrimCurrentlyApplied(
            f"armor {armor!r} already has trim {current!r} applied; "
            f"release before applying {trim_id!r}"
        )

    # ---- bulwark.md merge ----
    section_path = trim_bulwark_section_path(state_root, armor, trim_id)
    section_body = (
        section_path.read_text() if section_path.is_file() else ""
    )
    # Inject TTL based on expires_at
    section_body = _inject_ttl_into_rules(
        section_body, meta.expires_at, now=now,
    )
    bw_path = state_root / "bulwark.md"
    existing_bw = bw_path.read_text() if bw_path.is_file() else ""
    # Strip ALL trim blocks for this armor (not just this trim_id) so an
    # orphaned block from a previously-activated-but-never-released trim
    # can't linger and shadow this engagement's scope.
    base_bw = _strip_all_armor_trim_blocks(existing_bw, armor)
    if section_body.strip():
        merged_bw = base_bw + _wrap_trim_block(
            armor, trim_id, section_body, base=base_bw,
        )
    else:
        merged_bw = base_bw
    if merged_bw != existing_bw:
        bw_path.parent.mkdir(parents=True, exist_ok=True)
        bw_path.write_text(merged_bw)

    # ---- active-absolute merge ----
    armor_obj = armors_mod.load_armor(state_root, armor)
    abs_changed = False
    abs_target_path: Path | None = None
    if armor_obj is not None:
        abs_target_path = armor_obj.absolute_path
        # The above getter migrates legacy ABSOLUTE.md → absolutes/default.md
        # transparently. After migration, write back to the canonical path.
        trim_abs_path = trim_absolute_path(state_root, armor, trim_id)
        trim_abs_body = (
            trim_abs_path.read_text() if trim_abs_path.is_file() else ""
        )
        existing_abs = (
            abs_target_path.read_text() if abs_target_path.is_file() else ""
        )
        base_abs = _strip_absolute_trim(existing_abs, trim_id)
        if trim_abs_body.strip():
            merged_abs = base_abs + _wrap_absolute_trim(
                trim_id, trim_abs_body, base=base_abs,
            )
        else:
            merged_abs = base_abs
        if merged_abs != existing_abs:
            abs_target_path.parent.mkdir(parents=True, exist_ok=True)
            abs_target_path.write_text(merged_abs)
            abs_changed = True

    # ---- mark active ----
    _set_active_trim(state_root, armor, trim_id)

    # ---- emit TRIM-CONFIRMED token ----
    tokens_mod.set_token(
        f"TRIM-CONFIRMED:{trim_id}",
        reason=f"trim {trim_id} applied to armor {armor}",
        state_root=state_root,
    )

    # ---- declared tokens ----
    for t in meta.declared_tokens:
        try:
            tokens_mod.register_token(
                t, source=f"trim:{trim_id}",
                description=f"declared by trim {trim_id}",
            )
        except tokens_mod.InvalidTokenName:
            pass

    return {
        "armor": armor,
        "trim_id": trim_id,
        "bulwark_overlaid": bool(section_body.strip()),
        "absolute_overlaid": abs_changed,
        "expires_at": meta.expires_at,
        "absolute_path": str(abs_target_path) if abs_target_path else None,
    }


def release_trim(
    state_root: Path, armor: str, trim_id: str | None = None,
) -> dict:
    """Strip trim's overlays. Idempotent — no-op if not applied.

    If ``trim_id`` is None, releases whatever trim is currently active
    on this armor (if any).
    """
    state_root = Path(state_root)
    if trim_id is None:
        trim_id = active_trim_id(state_root, armor)
    if trim_id is None:
        return {"armor": armor, "trim_id": None, "released": False}

    bulwark_changed = False
    absolute_changed = False

    bw_path = state_root / "bulwark.md"
    if bw_path.is_file():
        before = bw_path.read_text()
        after = _strip_trim_block(before, armor, trim_id)
        if after != before:
            bw_path.write_text(after)
            bulwark_changed = True

    armor_obj = armors_mod.load_armor(state_root, armor)
    if armor_obj is not None:
        abs_p = armor_obj.absolute_path
        if abs_p.is_file():
            before = abs_p.read_text()
            after = _strip_absolute_trim(before, trim_id)
            if after != before:
                abs_p.write_text(after)
                absolute_changed = True

    if active_trim_id(state_root, armor) == trim_id:
        _set_active_trim(state_root, armor, None)

    # Unset TRIM-CONFIRMED token for this trim
    tokens_mod.unset_token(
        f"TRIM-CONFIRMED:{trim_id}",
        reason=f"trim {trim_id} released from armor {armor}",
        state_root=state_root,
    )

    return {
        "armor": armor,
        "trim_id": trim_id,
        "released": True,
        "bulwark_changed": bulwark_changed,
        "absolute_changed": absolute_changed,
    }


# ---------------------------------------------------------------------------
# Engagement-context derivation (per spec §6)
# ---------------------------------------------------------------------------

def derive_trim(
    state_root: Path, armor: str, *,
    weapon_id: str | None = None,
    engagement: dict,
    trim_id: str | None = None,
    operator_id: str = "",
) -> TrimMeta:
    """Pure-function derivation: ``engagement`` dict → trim files.

    No LLM. Same forge-v1 deterministic-core discipline.

    Engagement schema (per spec §6.1, abbreviated):

        {
          "engagement_id": "acme-portal-2026-04",
          "operator_id": "ops@acme.com",
          "target": {
            "primary": "https://customer-portal.acme.com",
            "scope_allows": [...],
            "oos": [...]
          },
          "test_window": {"start": "...Z", "end": "...Z"},
          "authorization": {...},
          "delivery_channels": [...],
          "operator_prefs": {...}
        }

    The derived trim materializes ``meta.json``,
    ``bulwark-section.md``, and ``absolute-trim.md`` under the armor.
    """
    state_root = Path(state_root)
    if armors_mod.load_armor(state_root, armor) is None:
        raise FileNotFoundError(f"armor {armor!r} not found at {state_root}")

    if trim_id is None:
        trim_id = engagement.get("engagement_id") or _generate_trim_id()
    _validate_trim_id(trim_id)

    target_block = engagement.get("target") or {}
    primary = target_block.get("primary") or ""
    scope_allows = list(target_block.get("scope_allows") or [])
    oos = list(target_block.get("oos") or [])
    env = (target_block.get("env") or "nonprod").lower()
    if env not in ("prod", "nonprod", "mixed"):
        env = "nonprod"
    assets = list(target_block.get("assets") or [])
    test_window = engagement.get("test_window") or {}
    tod_allowed = list(test_window.get("tod_allowed") or [])
    authorization = dict(engagement.get("authorization") or {})
    channels = list(engagement.get("delivery_channels") or [])
    prefs = dict(engagement.get("operator_prefs") or {})
    op_id = operator_id or engagement.get("operator_id") or ""

    # v0.1 fields — accept legacy "YES"/"NO" strings *and* native bools
    raw_active = engagement.get("active_testing", False)
    if isinstance(raw_active, str):
        active_testing = raw_active.strip().upper() in ("YES", "TRUE", "1")
    else:
        active_testing = bool(raw_active)
    posture = (engagement.get("posture") or "blackbox").lower()
    if posture not in ("blackbox", "graybox", "whitebox"):
        posture = "blackbox"
    depth = (engagement.get("depth") or "recon").lower()
    rate_limits = dict(engagement.get("rate_limits") or {})
    stop_conditions = list(engagement.get("stop_conditions") or [])
    eid = engagement.get("engagement_id")

    # Engagement-scoped intake + mats discovery (§4 + §12). Best-effort:
    # if the engagement_mats module is unavailable for any reason, the
    # trim still derives — the policy effect just degrades to v0.
    populated_mat_classes: list[str] = []
    if eid:
        try:
            from . import engagement_mats as _em
            _em.ensure_engagement(state_root, eid)
            _em.write_intake(state_root, eid, engagement)
            populated_mat_classes = _em.populated_classes(state_root, eid)
        except Exception:
            populated_mat_classes = []

    # Compose bulwark-section.md from the engagement context.
    bulwark_body = _compose_bulwark_section(
        armor=armor,
        scope_allows=scope_allows,
        oos=oos,
        prefs=prefs,
        env=env,
        assets=assets,
        posture=posture,
        active_testing=active_testing,
        engagement_id=eid,
        populated_mat_classes=populated_mat_classes,
        rate_limits=rate_limits,
    )

    # Compose absolute-trim.md.
    absolute_body = _compose_absolute_trim(
        primary=primary,
        scope_allows=scope_allows,
        oos=oos,
        test_window=test_window,
        tod_allowed=tod_allowed,
        operator_id=op_id,
        authorization=authorization,
        prefs=prefs,
        misc=engagement.get("misc") or "",
        env=env,
        posture=posture,
        active_testing=active_testing,
        depth=depth,
        rate_limits=rate_limits,
        stop_conditions=stop_conditions,
        engagement_id=eid,
        populated_mat_classes=populated_mat_classes,
    )

    # Materialize files
    d = trim_dir(state_root, armor, trim_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "bulwark-section.md").write_text(bulwark_body)
    (d / "absolute-trim.md").write_text(absolute_body)

    # Stash the source for traceability
    src = d / "source"
    src.mkdir(exist_ok=True)
    (src / "engagement.json").write_text(
        json.dumps(engagement, sort_keys=True, indent=2)
    )
    if weapon_id:
        (src / "weapon-ref.txt").write_text(weapon_id + "\n")

    # Build declared-tokens list per §13. Order is stable for diffs.
    declared: list[str] = []
    # Activity
    if active_testing or prefs.get("approve_high_risk"):
        declared.append("ACTIVE-TESTING-APPROVAL")
    # Environment
    if env == "prod":
        declared.append("ENV-PROD")
        if active_testing:
            declared.append("ACTIVE-PROD-APPROVAL")    # §5 + Q11
    elif env == "nonprod":
        declared.append("ENV-NONPROD")
    # mixed declares neither standing — per-asset rules carry it.
    # Posture
    if posture == "graybox":
        declared.append("POSTURE-GRAYBOX")
    elif posture == "whitebox":
        declared.append("POSTURE-WHITEBOX")

    meta = TrimMeta(
        trim_id=trim_id,
        armor=armor,
        weapon_ref=weapon_id,
        engagement_id=eid,
        target=primary,
        test_window_start=test_window.get("start"),
        test_window_end=test_window.get("end"),
        expires_at=test_window.get("end"),
        channels=channels,
        absolute_enforce=bool(prefs.get("absolute_enforce", True)),
        operator_id=op_id,
        authorization=authorization,
        operator_prefs=prefs,
        declared_tokens=list(dict.fromkeys(declared)),
        derived_from="engagement-derive-v0.1",
        release_on_armor_unequip=bool(prefs.get("release_on_armor_unequip", True)),
        created_at=time.time(),
        updated_at=time.time(),
        # v0.1 fields persisted into meta.json
        env=env,
        assets=assets,
        posture=posture,
        active_testing=active_testing,
        depth=depth,
        tod_allowed=tod_allowed,
        rate_limits=rate_limits,
        stop_conditions=stop_conditions,
    )
    save_meta(state_root, meta)
    return meta


def _generate_trim_id() -> str:
    """Generate a sortable trim id from the current UTC timestamp."""
    return datetime.now(timezone.utc).strftime("eng-%Y%m%dT%H%M%S")


# Loopback control-plane services remain reachable in generated Trims.
# Add explicit provider and resolver rules for the selected deployment.
_CONTROL_PLANE_NET_ALLOWS = (
    "127.*:*",
    "::1:*",
)

def _compose_bulwark_section(
    *, armor: str,
    scope_allows: list[str],
    oos: list[str],
    prefs: dict,
    env: str = "nonprod",
    assets: list[dict] | None = None,
    posture: str = "blackbox",
    active_testing: bool = False,
    engagement_id: str | None = None,
    populated_mat_classes: list[str] | None = None,
    rate_limits: dict | None = None,
) -> str:
    """Compose ``bulwark-section.md`` body. v0.1 extends v0 with env-,
    posture-, and mats-driven rule emission. Output is markdown-comment
    friendly so it round-trips through Bulwark's parser."""
    assets = assets or []
    populated_mat_classes = populated_mat_classes or []
    rate_limits = rate_limits or {}
    lines: list[str] = []
    lines.append(f"## armor: {armor}")
    lines.append("")
    lines.append("# Engagement scope — generated by derive_trim() (v0.1).")
    lines.append("")

    # ── In-scope + OOS rules (kept compatible with v0) ───────────────
    if scope_allows:
        lines.append("# In-scope allows")
        for host in scope_allows:
            host = str(host).strip()
            if not host:
                continue
            if ":" in host:
                lines.append(f"allow net   {host}")
            else:
                normalized = re.sub(r"^https?://", "", host)
                # Cover both schemes and all ports — a target may be
                # http:80, https:443, or a nonstandard port like :8080.
                lines.append(f"allow url   http://{normalized}/**")
                lines.append(f"allow url   https://{normalized}/**")
                lines.append(f"allow net   {normalized}:*")
        lines.append("")
    if oos:
        lines.append("# Operator OOS denies")
        for host in oos:
            host = str(host).strip()
            if not host:
                continue
            if ":" in host:
                lines.append(f"deny  net   {host}")
            else:
                normalized = re.sub(r"^https?://", "", host)
                lines.append(f"deny  url   https://{normalized}/**")
                lines.append(f"deny  net   {normalized}:*")
        lines.append("")
    if scope_allows:
        host_patterns: list[str] = []
        for host in scope_allows:
            host = str(host).strip()
            if not host:
                continue
            normalized = re.sub(r"^https?://", "", host)
            normalized = normalized.split(":", 1)[0]
            re_host = re.escape(normalized).replace(r"\*", "[^/]*")
            host_patterns.append(re_host)
        if host_patterns:
            joined = "|".join(host_patterns)
            lines.append("# Default-deny everything outside the allowlist.")
            lines.append("# `(?::\\d+)?` makes the port optional so URLs like")
            lines.append("# `https://host:443/path` still match the allow side.")
            lines.append("# `(?:/|$)` makes the path delimiter optional so")
            lines.append("# response-scope checks (which sometimes pass a")
            lines.append("# path-less `https://host:443`) don't false-deny.")
            lines.append(
                "deny  url   regex:^https?://"
                f"(?!(?:{joined})(?::\\d+)?(?:/|$)).*$"
            )
            lines.append("")

        # ── Net-layer egress control (CRITICAL) ──────────────────────
        # The url deny above only governs MITM-proxied traffic. The libc
        # shim enforces the *net* layer on direct socket connect()s — the
        # path a tool takes with raw sockets / http.client (which ignore
        # HTTP_PROXY and never touch the MITM). Without a net-layer deny-all
        # the shim default-allows every such connect, so the agent can
        # egress to any host on the internet. Emit control-plane allows,
        # then deny all other egress. The shim checks allow_net BEFORE deny,
        # so the in-scope target (allowed above with `net host:*`) and the
        # control plane stay reachable while everything else is blocked.
        lines.append("# Net-layer egress control (libc shim; direct sockets).")
        lines.append("# Control plane — model provider / DNS / loopback:")
        for _h in _CONTROL_PLANE_NET_ALLOWS:
            lines.append(f"allow net   {_h}")
        lines.append("# Default-deny all other direct-socket egress.")
        lines.append("deny  net   *:*")
        lines.append("")

    # ── §5: env=prod approval gates on state-changing patterns ───────
    if env == "prod":
        lines.append("# Prod environment — state-changing ops require")
        lines.append("# per-action PROD-DESTRUCTIVE-APPROVAL token.")
        lines.append("approval oc:bash * delete *")
        lines.append("approval oc:bash * drop *")
        lines.append("approval oc:bash * rm -rf *")
        lines.append("approval oc:write **")
        lines.append("approval url     * method:DELETE")
        lines.append("approval url     * method:PUT")
        lines.append("approval url     * method:POST")
        lines.append("approval url     * method:PATCH")
        lines.append("")

    # ── §5 mixed: per-asset prod denies (operator-curated) ───────────
    if env == "mixed" and assets:
        prod_hosts = [
            str(a.get("host") or "").strip()
            for a in assets if (a.get("env") == "prod")
        ]
        prod_hosts = [h for h in prod_hosts if h]
        if prod_hosts:
            lines.append("# Mixed env — prod-tagged assets need approval gates")
            for h in prod_hosts:
                norm = re.sub(r"^https?://", "", h).split(":", 1)[0]
                lines.append(f"approval url     https://{norm}/* method:DELETE")
                lines.append(f"approval url     https://{norm}/* method:PUT")
                lines.append(f"approval url     https://{norm}/* method:POST")
                lines.append(f"approval url     https://{norm}/* method:PATCH")
            lines.append("")

    # ── §6 active=false: gate outbound HTTP at scope hosts ───────────
    if not active_testing and scope_allows:
        lines.append("# Passive-only — active outbound to scope needs approval")
        for host in scope_allows:
            host = str(host).strip()
            if not host:
                continue
            normalized = re.sub(r"^https?://", "", host).split(":", 1)[0]
            lines.append(f"approval url     https://{normalized}/**")
        lines.append("")

    # ── §4: posture-driven engagement-mats reads ─────────────────────
    if engagement_id:
        if posture == "blackbox":
            lines.append("# Blackbox — agent cannot read engagement mats")
            lines.append(
                f"deny  oc:read **/engagements/{engagement_id}/mats/**"
            )
            lines.append("")
        elif populated_mat_classes:
            lines.append(
                f"# {posture.capitalize()} — operator-provided mats readable"
            )
            for cls in populated_mat_classes:
                lines.append(
                    f"allow oc:read **/engagements/{engagement_id}/mats/{cls}/**"
                )
            lines.append(
                f"allow oc:read **/engagements/{engagement_id}/intake.json"
            )
            lines.append(
                f"allow oc:read **/engagements/{engagement_id}/manifest.json"
            )
            lines.append("")

    # ── v0 high-risk patterns (kept for back-compat) ─────────────────
    if prefs.get("approve_high_risk") and prefs.get("high_risk_patterns"):
        lines.append("# High-risk operator-elected approval gates")
        for pat in prefs["high_risk_patterns"]:
            pat = str(pat).strip()
            if not pat:
                continue
            lines.append(f"approval oc:bash {pat}")
        lines.append("")

    return "\n".join(lines)


def _compose_absolute_trim(
    *, primary: str,
    scope_allows: Iterable[str],
    oos: Iterable[str],
    test_window: dict,
    operator_id: str,
    authorization: dict,
    prefs: dict,
    misc: str = "",
    tod_allowed: list[dict] | None = None,
    env: str = "nonprod",
    posture: str = "blackbox",
    active_testing: bool = False,
    depth: str = "recon",
    rate_limits: dict | None = None,
    stop_conditions: list[str] | None = None,
    engagement_id: str | None = None,
    populated_mat_classes: list[str] | None = None,
) -> str:
    """Compose ``absolute-trim.md`` body. v0.1 prepends an env warning,
    appends mats / TOD / rate-limit / stop-condition / depth sections."""
    tod_allowed = tod_allowed or []
    rate_limits = rate_limits or {}
    stop_conditions = stop_conditions or []
    populated_mat_classes = populated_mat_classes or []

    out: list[str] = []

    # ── §5 prod warning leads, before anything else ─────────────────
    if env == "prod":
        out.append("> ⚠ **PROD ENVIRONMENT.** Read-only by default. "
                   "Confirm with operator before any state-changing "
                   "action. State changes (POST/PUT/DELETE/PATCH, file "
                   "writes, destructive bash) require per-action "
                   "`PROD-DESTRUCTIVE-APPROVAL`.")
        out.append("")
    elif env == "mixed":
        out.append("> ⚠ **MIXED ENVIRONMENT.** Per-host classification "
                   "applies — prod-tagged hosts need per-action approval "
                   "for state changes; nonprod-tagged hosts run at "
                   "standard posture.")
        out.append("")

    title = primary or "(target unset)"
    out.append(f"## Engagement: {title}")
    out.append("")
    if operator_id or authorization:
        actor = operator_id or authorization.get("signed_by") or "(operator)"
        signed_at = authorization.get("signed_at") or "(unsigned)"
        out.append(
            f"You are engaging **{title}** on behalf of {actor}. "
            f"ROE attested {signed_at}, valid through "
            f"{test_window.get('end') or '(no end)'}."
        )
        out.append("")

    # Activity / env / posture summary
    summary_bits = [
        f"env=`{env}`",
        f"posture=`{posture}`",
        f"activity=`{'active' if active_testing else 'passive'}`",
        f"depth=`{depth}` (informational)",
    ]
    out.append("**Engagement contour:** " + ", ".join(summary_bits) + ".")
    out.append("")

    if scope_allows:
        out.append("**In scope:** " + ", ".join(str(h) for h in scope_allows) + ".")
    if oos:
        out.append("**Out of scope:** " + ", ".join(str(h) for h in oos) + ".")
    if scope_allows or oos:
        out.append("")

    # Mats listing — operator's whole point of providing them is so the
    # LLM thinks to look. Without explicit mention, it won't.
    if populated_mat_classes and engagement_id and posture != "blackbox":
        out.append("**Provided mats:**")
        for cls in populated_mat_classes:
            out.append(
                f"- `engagements/{engagement_id}/mats/{cls}/` "
                f"({cls.replace('-', ' ')})"
            )
        out.append("")

    # Time-of-day windows
    if tod_allowed:
        out.append("**Test window (time-of-day):**")
        for w in tod_allowed:
            days = "/".join(w.get("days") or [])
            tz = w.get("tz") or "UTC"
            out.append(
                f"- {days}, {w.get('start') or '?'}–{w.get('end') or '?'} {tz}"
            )
        out.append("Outside these hours all active operations halt; "
                   "you'll receive an operator directive (resume / "
                   "extend / pivot to artifact review / interactive).")
        out.append("")

    # Rate limits
    if rate_limits:
        gl = rate_limits.get("global_rps")
        per = rate_limits.get("per_host") or {}
        bits = []
        if gl:
            bits.append(f"{gl} RPS global")
        for h, rps in per.items():
            bits.append(f"{rps} RPS for `{h}`")
        if bits:
            out.append("**Rate limits:** " + ", ".join(bits) + ". Pace "
                       "active scanning accordingly; the runtime will "
                       f"{rate_limits.get('overflow', 'queue')} overage.")
            out.append("")

    # Stop conditions
    if stop_conditions:
        out.append("**Stop conditions:** " + ", ".join(
            f"`{c}`" for c in stop_conditions
        ) + ". When tripped, new crumb dispatch halts; in-flight crumbs "
        "finish; operator decides resume vs close.")
        out.append("")

    if prefs:
        out.append("**Operator preferences:**")
        for k, v in prefs.items():
            if isinstance(v, (list, dict)):
                v = json.dumps(v, separators=(",", ":"))
            out.append(f"- {k}: {v}")
        out.append("")
    end = test_window.get("end")
    if end:
        out.append(
            f"When the engagement window closes ({end}), this trim "
            f"auto-releases and any in-flight tests stop."
        )
        out.append("")
    if misc:
        out.append("## Operator Notes")
        out.append("")
        out.append(misc.strip())
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Auto-release on armor unequip (called from armors.set_active_armor)
# ---------------------------------------------------------------------------

def release_active_trim_for_armor(
    state_root: Path, armor: str,
) -> dict | None:
    """Release whichever trim is active for this armor (if any),
    respecting the trim's ``release_on_armor_unequip`` flag. Called
    by the armor-unequip path."""
    state_root = Path(state_root)
    tid = active_trim_id(state_root, armor)
    if tid is None:
        return None
    meta = load_trim(state_root, armor, tid)
    if meta is not None and not meta.release_on_armor_unequip:
        return {"armor": armor, "trim_id": tid, "released": False,
                "reason": "release_on_armor_unequip=False"}
    return release_trim(state_root, armor, tid)


__all__ = [
    # Path helpers
    "trims_dir", "trim_dir", "trim_meta_path",
    "trim_bulwark_section_path", "trim_absolute_path", "active_trim_path",
    # Dataclass + errors
    "TrimMeta", "TrimNotFound", "TrimCurrentlyApplied", "InvalidTrimId",
    # CRUD
    "list_trims", "list_all_trims", "load_trim", "active_trim_id",
    "new_trim", "save_meta", "delete_trim",
    # Apply / release
    "apply_trim", "release_trim", "release_active_trim_for_armor",
    # Derivation
    "derive_trim",
]
