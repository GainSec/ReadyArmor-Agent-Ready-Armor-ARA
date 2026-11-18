"""Engagement-scoped mats + canonical intake record.

Per spec ``armor-trim-v0.1-amendment.md`` §4 + §12. The mats pool lives
*per engagement*, not per trim, because:

1. The operator may re-derive the trim mid-engagement and shouldn't have
   to re-upload Acme's repo each time.
2. A single engagement that runs multiple weapons (web + API + mobile)
   shares the same mats.
3. The intake (engagement.json) is canonical at the engagement level,
   not the trim level — multi-trim, multi-weapon engagements stop
   duplicating it.

Layout::

    <state>/engagements/<engagement_id>/
      intake.json                ← canonical engagement record
      mats/
        source/                  ← whitebox: cloned repo / source tree
        architecture/            ← gray/white: diagrams, ADRs, threat models
        api-specs/               ← gray: swagger / openapi / postman / wsdl
        docs/                    ← gray: design docs, runbooks
        artifacts/               ← whitebox: build outputs, dep graphs, SBOMs
        creds/                   ← test creds (separate ACL — 0o700 / 0o600)
      manifest.json              ← every file: sha256, class, ts, uploader

Posture-driven `oc:read` allows are emitted by ``derive_trim`` against
this layout — see §4 of the spec for the rules. This module stays
purely about storage + manifest mechanics; policy is the trim's job.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAT_CLASSES: tuple[str, ...] = (
    "source",
    "architecture",
    "api-specs",
    "docs",
    "artifacts",
    "creds",
)

# `creds` get tighter ACLs because they're sensitive even within the
# operator's own state dir (sometimes a teammate is shoulder-surfing).
_CREDS_DIR_MODE = 0o700
_CREDS_FILE_MODE = 0o600
_DEFAULT_DIR_MODE = 0o755
_DEFAULT_FILE_MODE = 0o644

_EID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")
EXCLUDED_TAG = "mats-excluded"


class InvalidEngagementId(ValueError):
    pass


class InvalidMatClass(ValueError):
    pass


def _validate_engagement_id(eid: str) -> str:
    if not isinstance(eid, str) or not _EID_RE.match(eid):
        raise InvalidEngagementId(
            f"engagement_id must match {_EID_RE.pattern!r}, got {eid!r}"
        )
    return eid


def _validate_mat_class(mat_class: str) -> str:
    if mat_class not in MAT_CLASSES:
        raise InvalidMatClass(
            f"unknown mat class {mat_class!r}; valid: {', '.join(MAT_CLASSES)}"
        )
    return mat_class


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def engagements_root(state_root: Path) -> Path:
    return Path(state_root) / "engagements"


def engagement_dir(state_root: Path, eid: str) -> Path:
    _validate_engagement_id(eid)
    return engagements_root(state_root) / eid


def intake_path(state_root: Path, eid: str) -> Path:
    return engagement_dir(state_root, eid) / "intake.json"


def mats_root(state_root: Path, eid: str) -> Path:
    return engagement_dir(state_root, eid) / "mats"


def mats_class_dir(state_root: Path, eid: str, mat_class: str) -> Path:
    _validate_mat_class(mat_class)
    return mats_root(state_root, eid) / mat_class


def manifest_path(state_root: Path, eid: str) -> Path:
    return engagement_dir(state_root, eid) / "manifest.json"


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def ensure_engagement(state_root: Path, eid: str) -> Path:
    """Create the engagement layout. Idempotent."""
    state_root = Path(state_root)
    d = engagement_dir(state_root, eid)
    d.mkdir(parents=True, exist_ok=True)
    mats = mats_root(state_root, eid)
    mats.mkdir(exist_ok=True, mode=_DEFAULT_DIR_MODE)
    for cls in MAT_CLASSES:
        cls_dir = mats / cls
        if cls_dir.exists():
            continue
        if cls == "creds":
            cls_dir.mkdir(mode=_CREDS_DIR_MODE)
        else:
            cls_dir.mkdir(mode=_DEFAULT_DIR_MODE)
    mp = manifest_path(state_root, eid)
    if not mp.exists():
        _save_manifest(state_root, eid, {
            "engagement_id": eid,
            "schema_version": "0.1",
            "created_at": _now_iso(),
            "mats": [],
        })
    return d


# ---------------------------------------------------------------------------
# Intake (canonical engagement record)
# ---------------------------------------------------------------------------

def write_intake(state_root: Path, eid: str, engagement: dict) -> Path:
    """Persist the canonical engagement record. Idempotent overwrite —
    operators iterate intake during the wizard, and re-deriving a trim
    re-writes intake. Schema version is stamped if absent."""
    state_root = Path(state_root)
    ensure_engagement(state_root, eid)
    payload = dict(engagement)
    payload.setdefault("schema_version", "0.1")
    payload.setdefault("engagement_id", eid)
    p = intake_path(state_root, eid)
    _atomic_write_text(p, json.dumps(payload, indent=2, sort_keys=True))
    return p


def read_intake(state_root: Path, eid: str) -> dict | None:
    p = intake_path(Path(state_root), eid)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Mat records + manifest
# ---------------------------------------------------------------------------

@dataclass
class MatRecord:
    path: str                              # relative to mats/  (e.g. "source/main.py")
    mat_class: str
    sha256: str
    bytes: int
    uploaded_at: str                       # ISO-8601 UTC
    uploaded_by: str = ""
    tags: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MatRecord":
        return cls(
            path=d["path"],
            mat_class=d["mat_class"],
            sha256=d["sha256"],
            bytes=int(d.get("bytes") or 0),
            uploaded_at=d.get("uploaded_at") or "",
            uploaded_by=d.get("uploaded_by") or "",
            tags=list(d.get("tags") or []),
            note=d.get("note") or "",
        )


def load_manifest(state_root: Path, eid: str) -> dict:
    p = manifest_path(Path(state_root), eid)
    if not p.is_file():
        return {
            "engagement_id": eid,
            "schema_version": "0.1",
            "mats": [],
        }
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"engagement_id": eid, "schema_version": "0.1", "mats": []}


def _save_manifest(state_root: Path, eid: str, data: dict) -> None:
    p = manifest_path(Path(state_root), eid)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(p, json.dumps(data, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Add / remove mats
# ---------------------------------------------------------------------------

def add_mat_bytes(
    state_root: Path, eid: str, mat_class: str,
    filename: str, content: bytes,
    *, uploaded_by: str = "", tags: list[str] | None = None,
    note: str = "",
) -> MatRecord:
    """Write `content` under mats/<mat_class>/<filename>. Records into
    the manifest. Replaces any existing entry at the same relpath.
    Returns the new MatRecord.
    """
    state_root = Path(state_root)
    _validate_mat_class(mat_class)
    ensure_engagement(state_root, eid)
    safe_name = _sanitize_filename(filename)
    dest = mats_class_dir(state_root, eid, mat_class) / safe_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(dest, content)
    if mat_class == "creds":
        os.chmod(dest, _CREDS_FILE_MODE)
    else:
        os.chmod(dest, _DEFAULT_FILE_MODE)

    rec = MatRecord(
        path=f"{mat_class}/{safe_name}",
        mat_class=mat_class,
        sha256=_sha256(content),
        bytes=len(content),
        uploaded_at=_now_iso(),
        uploaded_by=uploaded_by,
        tags=list(tags or []),
        note=note,
    )
    _upsert_manifest_record(state_root, eid, rec)
    return rec


def add_mat_path(
    state_root: Path, eid: str, mat_class: str, src: Path,
    *, dest_name: str | None = None, uploaded_by: str = "",
    tags: list[str] | None = None, note: str = "",
    link: bool = False,
) -> MatRecord:
    """Add a file from the local filesystem. By default copies into the
    state dir; pass ``link=True`` to hard-link instead (useful for forge
    integration where mats are already in ``<state>/.ara/forge/...``).

    Hard-link mode requires src and dest to be on the same filesystem.
    """
    state_root = Path(state_root)
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"mat source not found or not a file: {src}")
    name = dest_name or src.name
    if link:
        # Skip the copy, but still record + checksum.
        _validate_mat_class(mat_class)
        ensure_engagement(state_root, eid)
        safe_name = _sanitize_filename(name)
        dest = mats_class_dir(state_root, eid, mat_class) / safe_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        try:
            os.link(src, dest)
        except OSError:
            # Cross-fs or unsupported — fall back to copy so the operator
            # isn't stuck waiting for permission troubleshooting.
            shutil.copy2(src, dest)
        content = dest.read_bytes()
        rec = MatRecord(
            path=f"{mat_class}/{safe_name}",
            mat_class=mat_class,
            sha256=_sha256(content),
            bytes=len(content),
            uploaded_at=_now_iso(),
            uploaded_by=uploaded_by,
            tags=list(tags or []) + ["link"],
            note=note,
        )
        _upsert_manifest_record(state_root, eid, rec)
        return rec
    return add_mat_bytes(
        state_root, eid, mat_class, name, src.read_bytes(),
        uploaded_by=uploaded_by, tags=tags, note=note,
    )


def remove_mat(state_root: Path, eid: str, relpath: str) -> bool:
    """Remove a mat by manifest relpath. Returns True if found + removed."""
    state_root = Path(state_root)
    manifest = load_manifest(state_root, eid)
    rows = manifest.get("mats") or []
    keep = [r for r in rows if r.get("path") != relpath]
    if len(keep) == len(rows):
        return False
    manifest["mats"] = keep
    _save_manifest(state_root, eid, manifest)
    fs_path = mats_root(state_root, eid) / relpath
    if fs_path.is_file():
        fs_path.unlink()
    return True


def mat_is_selected(rec: MatRecord) -> bool:
    """Return whether a mat should participate in generation.

    Selection is stored as an exclusion tag so every existing manifest
    record stays selected by default.
    """
    return EXCLUDED_TAG not in rec.tags


def set_mat_selected(
    state_root: Path, eid: str, relpath: str, selected: bool,
) -> bool:
    """Toggle whether a mat is used by generation.

    Returns False when the manifest has no matching relpath.
    """
    state_root = Path(state_root)
    manifest = load_manifest(state_root, eid)
    rows = list(manifest.get("mats") or [])
    found = False
    updated: list[dict] = []
    for row in rows:
        if row.get("path") != relpath:
            updated.append(row)
            continue
        found = True
        next_row = dict(row)
        tags = list(next_row.get("tags") or [])
        tags = [tag for tag in tags if tag != EXCLUDED_TAG]
        if not selected:
            tags.append(EXCLUDED_TAG)
        next_row["tags"] = tags
        updated.append(next_row)
    if not found:
        return False
    manifest["mats"] = updated
    manifest["updated_at"] = _now_iso()
    _save_manifest(state_root, eid, manifest)
    return True


def list_mats(state_root: Path, eid: str) -> list[MatRecord]:
    manifest = load_manifest(Path(state_root), eid)
    return [MatRecord.from_dict(r) for r in (manifest.get("mats") or [])]


def list_mats_by_class(
    state_root: Path, eid: str, mat_class: str,
) -> list[MatRecord]:
    _validate_mat_class(mat_class)
    return [m for m in list_mats(state_root, eid) if m.mat_class == mat_class]


def populated_classes(state_root: Path, eid: str) -> list[str]:
    """Which mat classes actually contain at least one file. Drives the
    `oc:read` allow emission in derive_trim — empty classes don't get
    rules wasted on them."""
    seen = {m.mat_class for m in list_mats(state_root, eid) if mat_is_selected(m)}
    return [c for c in MAT_CLASSES if c in seen]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sanitize_filename(name: str) -> str:
    """Block path traversal + control chars; preserve dots, dashes,
    underscores. Falls back to ``mat`` if the name is empty after
    sanitization."""
    name = Path(name).name        # strip leading dirs
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.strip().strip(".")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "mat"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, data: str) -> None:
    _atomic_write_bytes(path, data.encode("utf-8"))


def _upsert_manifest_record(
    state_root: Path, eid: str, rec: MatRecord,
) -> None:
    manifest = load_manifest(state_root, eid)
    rows = manifest.get("mats") or []
    rows = [r for r in rows if r.get("path") != rec.path]
    rows.append(rec.to_dict())
    rows.sort(key=lambda r: r.get("path") or "")
    manifest["mats"] = rows
    manifest["updated_at"] = _now_iso()
    _save_manifest(state_root, eid, manifest)


# ---------------------------------------------------------------------------
# Disk-scan reconciliation
# ---------------------------------------------------------------------------

def rescan_disk(
    state_root: Path, eid: str, *,
    uploaded_by: str = "rescan",
) -> dict:
    """Walk every ``mats/<class>/`` directory and pick up files not
    already in the manifest. Returns ``{added, removed, kept}`` counts.

    Used when the operator dropped files via scp / rsync / cp without
    going through the wizard. Sha256-checks existing entries — if a
    file's content changed on disk, the manifest record is rewritten
    with the new hash and ``rescan-updated`` tag. Files removed on
    disk are pruned from the manifest.
    """
    state_root = Path(state_root)
    ensure_engagement(state_root, eid)
    manifest = load_manifest(state_root, eid)
    rows = list(manifest.get("mats") or [])
    by_path = {r.get("path"): r for r in rows}
    added = 0
    updated = 0
    removed = 0
    seen: set[str] = set()

    for cls in MAT_CLASSES:
        cls_dir = mats_class_dir(state_root, eid, cls)
        if not cls_dir.is_dir():
            continue
        for fs_path in sorted(cls_dir.rglob("*")):
            if not fs_path.is_file() or fs_path.suffix == ".tmp":
                continue
            rel = fs_path.relative_to(mats_root(state_root, eid)).as_posix()
            seen.add(rel)
            content = fs_path.read_bytes()
            sha = _sha256(content)
            existing = by_path.get(rel)
            if existing is None:
                rec = MatRecord(
                    path=rel, mat_class=cls, sha256=sha,
                    bytes=len(content), uploaded_at=_now_iso(),
                    uploaded_by=uploaded_by, tags=["rescan"],
                )
                by_path[rel] = rec.to_dict()
                added += 1
            elif existing.get("sha256") != sha:
                # Content changed — refresh hash + bytes, tag the change.
                existing["sha256"] = sha
                existing["bytes"] = len(content)
                existing["uploaded_at"] = _now_iso()
                tags = list(existing.get("tags") or [])
                if "rescan-updated" not in tags:
                    tags.append("rescan-updated")
                existing["tags"] = tags
                updated += 1

    # Prune manifest entries for files that vanished from disk.
    for path in list(by_path):
        if path not in seen:
            del by_path[path]
            removed += 1

    rows = sorted(by_path.values(), key=lambda r: r.get("path") or "")
    manifest["mats"] = rows
    manifest["updated_at"] = _now_iso()
    _save_manifest(state_root, eid, manifest)
    return {"added": added, "updated": updated, "removed": removed}


__all__ = [
    "EXCLUDED_TAG",
    "MAT_CLASSES",
    "InvalidEngagementId",
    "InvalidMatClass",
    "MatRecord",
    "add_mat_bytes",
    "add_mat_path",
    "engagement_dir",
    "engagements_root",
    "ensure_engagement",
    "intake_path",
    "list_mats",
    "list_mats_by_class",
    "load_manifest",
    "manifest_path",
    "mat_is_selected",
    "mats_class_dir",
    "mats_root",
    "populated_classes",
    "read_intake",
    "remove_mat",
    "rescan_disk",
    "set_mat_selected",
    "write_intake",
]
