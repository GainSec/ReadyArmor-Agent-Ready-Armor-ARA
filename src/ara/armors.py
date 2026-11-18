"""Armor Sets — named rule loadouts.

Each armor set lives at ``<state_root>/armors/<name>/`` with:

  WALLS.md        -- rule file (required)
  ABSOLUTE.md     -- envelope content (optional)
  overrides.json  -- entries scoped to this armor (optional)
  meta.json       -- human metadata: description, sigil, tags (optional)

The currently equipped armor is named in ``<state_root>/active-armor``.
``ara launch`` reads that file to pick which WALLS to load; existing
hot-reload machinery then tracks changes to the file inside the active
armor.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .walls import Walls, parse_file as parse_walls_file


ACTIVE_FILE = "active-armor"
ARMORS_DIR = "armors"

WALLS_FILENAME = "WALLS.md"
ABSOLUTE_FILENAME = "ABSOLUTE.md"   # legacy single-file layout
ABSOLUTES_DIR = "absolutes"          # new: multiple prompts per armor
ABSOLUTES_ACTIVE_FILE = "active"     # holds the name of the active file
OVERRIDES_FILENAME = "overrides.json"
META_FILENAME = "meta.json"


# Channels an armor can be scoped to. Empty list on an armor means
# "applies globally to every channel" (back-compat default). The
# registry lives here so the dashboard pill renderer + the future
# enforcement-side channel matcher (Phase 2) read from the same source.
# Colors are hex (no theme tokens) so they survive on the login page
# and any unstyled context.
CHANNEL_REGISTRY: list[dict] = [
    {"id": "discord",     "label": "Discord",      "color": "#5865F2"},
    {"id": "telegram",    "label": "Telegram",     "color": "#229ED9"},
    {"id": "imessage",    "label": "iMessage",     "color": "#34C759"},
    {"id": "signal",      "label": "Signal",       "color": "#3A76F0"},
    {"id": "whatsapp",    "label": "WhatsApp",     "color": "#25D366"},
    {"id": "slack",       "label": "Slack",        "color": "#4A154B"},
    {"id": "bluebubbles", "label": "BlueBubbles",  "color": "#0073E6"},
    {"id": "voice",       "label": "Voice",        "color": "#D97757"},
    {"id": "web",         "label": "Web",          "color": "#888888"},
]
CHANNEL_IDS: tuple = tuple(c["id"] for c in CHANNEL_REGISTRY)


def channel_color(channel_id: str) -> str:
    """Return the brand color for a channel id, or a default for
    unknown ids (so the UI never blanks out on a typo)."""
    for c in CHANNEL_REGISTRY:
        if c["id"] == channel_id:
            return c["color"]
    return "#888888"


def channel_label(channel_id: str) -> str:
    for c in CHANNEL_REGISTRY:
        if c["id"] == channel_id:
            return c["label"]
    return channel_id


@dataclass
class ArmorMeta:
    description: str = ""
    sigil: str = "shield"      # arbitrary short token used as icon hint
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    shadow_mode: bool = False  # dry-run: denies become allow+audit
    # When True, ARA's MITM applies the active ABSOLUTE.md's content as a
    # real rewrite on every matching request — the agent sees the
    # operator's edited prompt instead of openclaw's. Flipped from the
    # /shadows "Promote" button once operators are confident in a variant.
    absolute_enforce: bool = False
    # Phase-1 channel scoping: list of channel ids (subset of
    # CHANNEL_REGISTRY). Empty list = global / no scope, applies to
    # every channel. Phase 2 will add enforcement; for now it's
    # display-only metadata.
    channels: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "ArmorMeta":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except Exception:
            return cls()
        return cls(
            description=str(raw.get("description", "")),
            sigil=str(raw.get("sigil", "shield")),
            tags=list(raw.get("tags") or []),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            shadow_mode=bool(raw.get("shadow_mode") or False),
            absolute_enforce=bool(raw.get("absolute_enforce") or False),
            channels=list(raw.get("channels") or []),
        )

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "sigil": self.sigil,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "shadow_mode": self.shadow_mode,
            "absolute_enforce": self.absolute_enforce,
            "channels": self.channels,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))


@dataclass
class ArmorStats:
    # Bulwark-derived: rules scoped to this armor (plus any globals).
    rules: int = 0                # total applicable rules
    deny: int = 0                 # deny-action count
    approvals: int = 0            # approval-action count
    allows: int = 0               # allow-action count
    # Canary summary (global across the state root, surfaced on the card
    # because canaries are session-agnostic):
    canary_seeded: int = 0
    canary_hits: int = 0
    # Legacy-compat aliases — some external readers still reference
    # these field names. Map them to the Bulwark-derived numbers.
    @property
    def walls(self) -> int:       # historical label: deny rules
        return self.deny
    @property
    def commands(self) -> int:    # historical: exec-layer deny count
        return 0
    @property
    def overrides(self) -> int:   # historical: allow-list size
        return self.allows
    @property
    def blackholes(self) -> int:  # historical: canary hits — the new "always block" signal
        return self.canary_hits

    @property
    def total(self) -> int:
        return self.rules

    @property
    def tier(self) -> int:
        t = self.total
        if t >= 60:
            return 5
        if t >= 31:
            return 4
        if t >= 16:
            return 3
        if t >= 6:
            return 2
        return 1


@dataclass
class ArmorSet:
    name: str
    root: Path
    meta: ArmorMeta
    stats: ArmorStats
    equipped: bool = False

    @property
    def walls_path(self) -> Path:
        return self.root / WALLS_FILENAME

    @property
    def absolute_path(self) -> Path:
        """Path of the currently-active ABSOLUTE for this armor.

        Resolution order:
          1. ``absolutes/<active>.md`` where ``active`` is the name stored
             in ``absolutes/active``.
          2. If no ``active`` file, the first ``*.md`` alphabetically under
             ``absolutes/``.
          3. Legacy: ``ABSOLUTE.md`` at the armor root. Auto-migrated
             on first access into ``absolutes/default.md`` + an
             ``active`` file pointing at it.
        """
        _migrate_legacy_absolute(self.root)
        ad = self.root / ABSOLUTES_DIR
        if ad.is_dir():
            active_file = ad / ABSOLUTES_ACTIVE_FILE
            if active_file.exists():
                name = active_file.read_text().strip()
                candidate = ad / f"{name}.md"
                if candidate.exists():
                    return candidate
            # Fallback: first .md alphabetically.
            mds = sorted(p for p in ad.glob("*.md"))
            if mds:
                return mds[0]
        # Stub path used for "about to create" writes when the armor has
        # no absolutes yet.
        return self.root / ABSOLUTE_FILENAME

    @property
    def absolutes_dir(self) -> Path:
        return self.root / ABSOLUTES_DIR

    @property
    def overrides_path(self) -> Path:
        return self.root / OVERRIDES_FILENAME


def _migrate_legacy_absolute(armor_root: Path) -> None:
    """One-shot rename of legacy ``<armor>/ABSOLUTE.md`` into
    ``<armor>/absolutes/default.md`` so every armor has the new layout.
    Idempotent; no-op when the new dir already exists or the legacy file
    is absent."""
    legacy = armor_root / ABSOLUTE_FILENAME
    target_dir = armor_root / ABSOLUTES_DIR
    if target_dir.is_dir():
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    active_file = target_dir / ABSOLUTES_ACTIVE_FILE
    if legacy.exists():
        try:
            new_path = target_dir / "default.md"
            if not new_path.exists():
                legacy.replace(new_path)
            if not active_file.exists():
                active_file.write_text("default")
        except OSError:
            return
    else:
        # Fresh armor: seed an empty default so the UI has something to
        # target for writes.
        if not (target_dir / "default.md").exists():
            (target_dir / "default.md").write_text("")
        if not active_file.exists():
            active_file.write_text("default")


def list_absolutes(armor: "ArmorSet") -> list[str]:
    """Names (without .md) of every absolute file under the armor, active
    first and the rest alphabetically."""
    _migrate_legacy_absolute(armor.root)
    ad = armor.absolutes_dir
    if not ad.is_dir():
        return []
    names = sorted(p.stem for p in ad.glob("*.md"))
    active = active_absolute_name(armor)
    if active and active in names:
        names.remove(active)
        names.insert(0, active)
    return names


def active_absolute_name(armor: "ArmorSet") -> Optional[str]:
    _migrate_legacy_absolute(armor.root)
    ap = armor.absolutes_dir / ABSOLUTES_ACTIVE_FILE
    if not ap.exists():
        return None
    name = ap.read_text().strip()
    return name or None


def set_active_absolute(armor: "ArmorSet", name: str) -> bool:
    _migrate_legacy_absolute(armor.root)
    candidate = armor.absolutes_dir / f"{name}.md"
    if not candidate.exists():
        return False
    (armor.absolutes_dir / ABSOLUTES_ACTIVE_FILE).write_text(name.strip())
    return True


def create_absolute(armor: "ArmorSet", name: str, contents: str = "") -> Path:
    _migrate_legacy_absolute(armor.root)
    safe = "".join(c for c in name if c.isalnum() or c in "._-") or "untitled"
    p = armor.absolutes_dir / f"{safe}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(contents)
    return p


def delete_absolute(armor: "ArmorSet", name: str) -> bool:
    _migrate_legacy_absolute(armor.root)
    p = armor.absolutes_dir / f"{name}.md"
    if not p.exists():
        return False
    # Don't leave the armor with no absolutes — refuse to drop the last one.
    remaining = [q for q in armor.absolutes_dir.glob("*.md") if q != p]
    if not remaining:
        return False
    p.unlink()
    # If we deleted the active one, promote the next alphabetically.
    active = active_absolute_name(armor)
    if active == name:
        set_active_absolute(armor, remaining[0].stem)
    return True


def rename_absolute(armor: "ArmorSet", old_name: str, new_name: str) -> bool:
    _migrate_legacy_absolute(armor.root)
    safe = "".join(c for c in new_name if c.isalnum() or c in "._-")
    if not safe:
        return False
    src = armor.absolutes_dir / f"{old_name}.md"
    dst = armor.absolutes_dir / f"{safe}.md"
    if not src.exists() or dst.exists():
        return False
    src.rename(dst)
    if active_absolute_name(armor) == old_name:
        set_active_absolute(armor, safe)
    return True


def duplicate_absolute(
    armor: "ArmorSet", src_name: str, dst_name: str,
) -> bool:
    """Copy an existing absolute to a new name within the same armor.
    Returns True on success. Fails if src is missing, dst already
    exists, or dst_name sanitizes to empty."""
    _migrate_legacy_absolute(armor.root)
    safe = "".join(c for c in dst_name if c.isalnum() or c in "._-")
    if not safe:
        return False
    src = armor.absolutes_dir / f"{src_name}.md"
    dst = armor.absolutes_dir / f"{safe}.md"
    if not src.exists() or dst.exists():
        return False
    dst.write_text(src.read_text())
    return True


def path_for_absolute(armor: "ArmorSet", name: str) -> Path:
    """Return the path of a specific absolute (for read/write)."""
    _migrate_legacy_absolute(armor.root)
    return armor.absolutes_dir / f"{name}.md"


def armors_root(state_root: Path) -> Path:
    return state_root / ARMORS_DIR


def active_armor_file(state_root: Path) -> Path:
    return state_root / ACTIVE_FILE


def active_armor_name(state_root: Path) -> Optional[str]:
    path = active_armor_file(state_root)
    if not path.exists():
        return None
    name = path.read_text().strip()
    return name or None


def set_active_armor(state_root: Path, name: Optional[str]) -> None:
    path = active_armor_file(state_root)
    prior = active_armor_name(state_root)
    # When the active armor changes (or clears), release the prior
    # armor's active trim — overlays must not bleed across identities.
    # Per spec §11 (Q-approved), the trim's release_on_armor_unequip
    # flag can opt-out.
    if prior and prior != name:
        try:
            from . import trims as _trims
            _trims.release_active_trim_for_armor(state_root, prior)
        except Exception:
            # Best-effort — don't block armor swap on trim hiccups.
            pass
    if name is None:
        if path.exists():
            path.unlink()
        return
    state_root.mkdir(parents=True, exist_ok=True)
    path.write_text(name.strip())


def _load_stats(armor_root: Path) -> ArmorStats:
    """Compute stats from the unified Bulwark store + canary index.

    ``armor_root`` is ``<state_root>/armors/<name>``; state_root is
    recovered via ``.parent.parent``. Counts rules whose scope matches
    this armor (global or armor-specific) and the global canary summary.
    """
    stats = ArmorStats()
    state_root = armor_root.parent.parent
    armor_name = armor_root.name

    # ---- Bulwark rule counts (scoped to this armor) ----
    bw_path = state_root / "bulwark.md"
    if bw_path.exists():
        try:
            from . import bulwark as _bw
            bw = _bw.parse_file(bw_path)
            applicable = [
                r for r in bw.rules
                if _bw.scope_matches(r.scope, armor_name)
            ]
            stats.rules = len(applicable)
            stats.deny = sum(1 for r in applicable if r.action == "deny")
            stats.approvals = sum(1 for r in applicable if r.action == "approval")
            stats.allows = sum(1 for r in applicable if r.action == "allow")
        except Exception:  # noqa: BLE001
            pass

    # ---- Canary summary (global — applies across all sessions) ----
    canaries_path = state_root / "canaries.json"
    if canaries_path.exists():
        try:
            raw = json.loads(canaries_path.read_text())
            seeds = raw.get("canaries") or []
            stats.canary_seeded = len(seeds)
            stats.canary_hits = sum(
                int(c.get("hit_count") or 0) for c in seeds
            )
        except Exception:  # noqa: BLE001
            pass
    return stats


def load_armor(state_root: Path, name: str) -> Optional[ArmorSet]:
    if not _is_valid_name(name):
        return None
    root = armors_root(state_root) / name
    if not root.is_dir():
        return None
    meta = ArmorMeta.load(root / META_FILENAME)
    stats = _load_stats(root)
    return ArmorSet(
        name=name,
        root=root,
        meta=meta,
        stats=stats,
        equipped=(active_armor_name(state_root) == name),
    )


def list_armors(state_root: Path) -> list[ArmorSet]:
    base = armors_root(state_root)
    if not base.is_dir():
        return []
    active = active_armor_name(state_root)
    out: list[ArmorSet] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or not _is_valid_name(entry.name):
            continue
        meta = ArmorMeta.load(entry / META_FILENAME)
        stats = _load_stats(entry)
        out.append(ArmorSet(
            name=entry.name,
            root=entry,
            meta=meta,
            stats=stats,
            equipped=(active == entry.name),
        ))
    return out


_NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")


def _is_valid_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    return all(c in _NAME_OK for c in name.lower())


def create_armor(
    state_root: Path,
    name: str,
    *,
    from_walls: Optional[Path] = None,
    description: str = "",
    sigil: str = "shield",
    tags: Optional[list[str]] = None,
) -> ArmorSet:
    if not _is_valid_name(name):
        raise ValueError(f"invalid armor name: {name!r}")
    root = armors_root(state_root) / name
    if root.exists():
        raise FileExistsError(f"armor {name!r} already exists")
    root.mkdir(parents=True)

    walls_dst = root / WALLS_FILENAME
    if from_walls and from_walls.exists():
        shutil.copyfile(from_walls, walls_dst)
    else:
        walls_dst.write_text(
            "# Armor Set: " + name + "\n"
            "# Drop rules under the sections below.\n\n"
            "## deny_path\n\n"
            "## deny_command\n\n"
            "## approval_path\n\n"
            "## approval_command\n"
        )

    # New layout: seed one absolute named "default" inside absolutes/
    # so the armor starts compatible with the multi-file editor.
    abs_dir = root / ABSOLUTES_DIR
    abs_dir.mkdir(parents=True, exist_ok=True)
    (abs_dir / "default.md").write_text(
        "# ABSOLUTE (envelope)\n\n"
        "This is the canonical control context injected before every turn.\n"
    )
    (abs_dir / ABSOLUTES_ACTIVE_FILE).write_text("default")

    (root / OVERRIDES_FILENAME).write_text(
        json.dumps({"overrides": [], "blackholes": []}, indent=2) + "\n"
    )

    now = time.time()
    meta = ArmorMeta(
        description=description, sigil=sigil,
        tags=tags or [], created_at=now, updated_at=now,
    )
    meta.save(root / META_FILENAME)
    return load_armor(state_root, name)  # type: ignore[return-value]


def clone_armor(state_root: Path, src: str, dst: str) -> ArmorSet:
    src_root = armors_root(state_root) / src
    if not src_root.is_dir():
        raise FileNotFoundError(f"armor {src!r} not found")
    if not _is_valid_name(dst):
        raise ValueError(f"invalid armor name: {dst!r}")
    dst_root = armors_root(state_root) / dst
    if dst_root.exists():
        raise FileExistsError(f"armor {dst!r} already exists")
    shutil.copytree(src_root, dst_root)
    meta = ArmorMeta.load(dst_root / META_FILENAME)
    now = time.time()
    meta.description = f"(clone of {src}) " + meta.description
    meta.updated_at = now
    meta.save(dst_root / META_FILENAME)
    return load_armor(state_root, dst)  # type: ignore[return-value]


def delete_armor(state_root: Path, name: str) -> bool:
    if not _is_valid_name(name):
        return False
    root = armors_root(state_root) / name
    if not root.is_dir():
        return False
    if active_armor_name(state_root) == name:
        set_active_armor(state_root, None)
    shutil.rmtree(root)
    return True


def equip_armor(state_root: Path, name: str) -> Optional[ArmorSet]:
    armor = load_armor(state_root, name)
    if armor is None:
        return None
    set_active_armor(state_root, name)
    armor.equipped = True
    return armor


def update_armor_meta(
    state_root: Path,
    name: str,
    *,
    description: Optional[str] = None,
    sigil: Optional[str] = None,
    tags: Optional[list[str]] = None,
    shadow_mode: Optional[bool] = None,
) -> Optional[ArmorSet]:
    armor = load_armor(state_root, name)
    if armor is None:
        return None
    if description is not None:
        armor.meta.description = description
    if sigil is not None:
        armor.meta.sigil = sigil
    if tags is not None:
        armor.meta.tags = list(tags)
    if shadow_mode is not None:
        armor.meta.shadow_mode = bool(shadow_mode)
    armor.meta.updated_at = time.time()
    armor.meta.save(armor.root / META_FILENAME)
    return load_armor(state_root, name)


def export_armor(state_root: Path, name: str) -> bytes:
    """Return a gzipped tar of the armor dir, rooted at ``<name>/``.
    Raises FileNotFoundError if the armor doesn't exist."""
    root = armors_root(state_root) / name
    if not root.is_dir():
        raise FileNotFoundError(f"armor {name!r} not found")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(root, arcname=name)
    return buf.getvalue()


def import_armor(
    state_root: Path, blob: bytes, *,
    rename_to: Optional[str] = None,
    overwrite: bool = False,
) -> ArmorSet:
    """Extract a tarball produced by export_armor() into state_root/armors/.
    The tarball's top-level directory is used as the armor name unless
    ``rename_to`` is given. Rejects any entry with absolute paths,
    parent refs, symlinks, or hardlinks (prevents path-traversal imports)."""
    base = armors_root(state_root)
    base.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(blob)
    with tarfile.open(fileobj=buf, mode="r:*") as tar:
        members = tar.getmembers()
        if not members:
            raise ValueError("empty archive")
        top = members[0].name.split("/", 1)[0]
        if not _is_valid_name(top):
            raise ValueError(f"archive top-level name invalid: {top!r}")
        target_name = rename_to or top
        if not _is_valid_name(target_name):
            raise ValueError(f"invalid armor name: {target_name!r}")

        for m in members:
            if m.issym() or m.islnk():
                raise ValueError(f"archive contains {m.name!r} as link — rejected")
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ValueError(f"archive contains unsafe path: {m.name!r}")
            if not m.name.startswith(top + "/") and m.name != top:
                raise ValueError(
                    f"archive entry outside top-level dir: {m.name!r}"
                )

        dst = base / target_name
        if dst.exists():
            if not overwrite:
                raise FileExistsError(f"armor {target_name!r} already exists")
            shutil.rmtree(dst)
        # Extract into a temp dir then rename — avoids partial state on failure.
        tmp = base / f".importing.{target_name}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        try:
            tar.extractall(tmp)  # noqa: S202 — entries pre-validated above
            extracted = tmp / top
            if not extracted.is_dir():
                raise ValueError("archive missing expected top-level dir")
            extracted.rename(dst)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    armor = load_armor(state_root, target_name)
    assert armor is not None
    return armor


def set_absolute_enforce(state_root: Path, armor_name: str, enforce: bool) -> bool:
    """Flip the ``absolute_enforce`` flag on ``<armor>/meta.json``. Called
    by /shadows Promote to turn the ABSOLUTE live projection from a
    shadow-rewrite variant into a real rewrite applied on the wire."""
    armor = load_armor(state_root, armor_name)
    if armor is None:
        return False
    armor.meta.absolute_enforce = bool(enforce)
    armor.meta.updated_at = time.time()
    armor.meta.save(armor.root / META_FILENAME)
    return True


def is_absolute_enforcing(state_root: Path, armor_name: str) -> bool:
    armor = load_armor(state_root, armor_name)
    if armor is None:
        return False
    return bool(armor.meta.absolute_enforce)


def set_channels(
    state_root: Path, armor_name: str, channels: list[str],
) -> bool:
    """Replace the channels list on ``<armor>/meta.json``. Filters to
    known channel ids from ``CHANNEL_REGISTRY`` so typos don't end up
    persisted. Empty list = global / applies to every channel."""
    armor = load_armor(state_root, armor_name)
    if armor is None:
        return False
    sanitised = [c for c in channels if c in CHANNEL_IDS]
    armor.meta.channels = sanitised
    armor.meta.updated_at = time.time()
    armor.meta.save(armor.root / META_FILENAME)
    return True


def get_channels(state_root: Path, armor_name: str) -> list[str]:
    armor = load_armor(state_root, armor_name)
    if armor is None:
        return []
    return list(armor.meta.channels)


def armor_applies_to_channel(
    meta: "ArmorMeta | None", channel_id: Optional[str],
) -> bool:
    """Phase 2 enforcement gate. An armor applies if:

      - its ``channels`` list is empty (the back-compat / global default), OR
      - the detected channel id is in its ``channels`` list.

    When ``channel_id`` is ``None`` (no detection signal at all), only
    global armors apply — channel-scoped armors stay dormant rather
    than firing on a turn we can't classify.
    """
    if meta is None:
        return False
    if not meta.channels:
        return True
    if not channel_id:
        return False
    return channel_id in meta.channels


# Heuristic markers used by ``detect_channel`` to classify a turn into
# one of the channels in CHANNEL_REGISTRY. The order matters: explicit
# prefixes win over generic snowflake / phone-number heuristics so a
# turn carrying ``"chat_id": "imessage:..."`` doesn't get mis-tagged
# as Discord just because it happens to contain a long numeric id.
_CHANNEL_PREFIX_HINTS: tuple = (
    ('"chat_id": "discord:', "discord"),
    ('"chat_id": "tg:',      "telegram"),
    ('"chat_id": "telegram:', "telegram"),
    ('"chat_id": "imessage:', "imessage"),
    ('"chat_id": "signal:',  "signal"),
    ('"chat_id": "whatsapp:', "whatsapp"),
    ('"chat_id": "slack:',   "slack"),
    ('"chat_id": "bluebubbles:', "bluebubbles"),
    ('"chat_id": "voice:',   "voice"),
    ('"chat_id": "web:',     "web"),
)


def detect_channel(body: dict) -> Optional[str]:
    """Best-effort channel classification for a captured request body.

    Walks every string in the body looking for openclaw's chat-context
    metadata block and matching it against ``_CHANNEL_PREFIX_HINTS``.
    Falls through to a heuristic for the generic ``"chat_id": "user:..."``
    shape openclaw emits for Discord (17-19 digit snowflake).

    Returns one of the ids in ``CHANNEL_REGISTRY`` or ``None`` when no
    confident signal is found. Phase-2 enforcement treats ``None`` the
    same as "no global armor matches" — channel-scoped armors stay
    dormant on un-classifiable turns rather than over-applying.
    """
    if not isinstance(body, dict):
        return None
    import re
    haystack: list[str] = []

    def collect(o, depth=0):
        if depth > 8:
            return
        if isinstance(o, str):
            haystack.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                collect(v, depth + 1)
        elif isinstance(o, (list, tuple)):
            for v in o:
                collect(v, depth + 1)

    collect(body)
    blob = " ".join(haystack)

    for needle, channel in _CHANNEL_PREFIX_HINTS:
        if needle in blob:
            return channel

    # Discord-shaped fallback: ``"chat_id": "user:NNN..."`` with 17-19
    # digit snowflake. Fragile — but the Numbuh deployment uses this
    # exact format and it's the most common openclaw -> Discord shape.
    if re.search(r'"chat_id"\s*:\s*"user:\d{17,19}"', blob):
        return "discord"
    return None


def active_shadow_mode(state_root: Path) -> bool:
    """True if the equipped armor (if any) has shadow_mode set."""
    name = active_armor_name(state_root)
    if not name:
        return False
    a = load_armor(state_root, name)
    return bool(a and a.meta.shadow_mode)


def active_walls_file(state_root: Path) -> Optional[Path]:
    """Path to the active armor's WALLS.md, if an armor is equipped."""
    name = active_armor_name(state_root)
    if not name:
        return None
    root = armors_root(state_root) / name
    walls = root / WALLS_FILENAME
    if walls.exists():
        return walls
    return None
