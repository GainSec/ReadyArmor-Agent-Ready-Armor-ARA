"""Tests for ``ara.engagement_mats`` — engagement-scoped mats + intake.

Per spec ``armor-trim-v0.1-amendment.md`` §4 + §12. Covers:

- Engagement-id + mat-class validation.
- ``ensure_engagement`` creates the layout idempotently and sets the
  tighter ACL on the ``creds`` directory.
- Intake round-trip; missing-engagement read returns None.
- ``add_mat_bytes`` / ``add_mat_path`` write content + manifest entries
  with correct sha256 + bytes + classification.
- Hard-link mode for forge integration; falls back to copy on
  cross-filesystem error.
- Re-adding the same path replaces the prior manifest record.
- ``remove_mat`` clears both the file and the manifest entry.
- ``populated_classes`` surfaces only classes with at least one file.
- Filename sanitization blocks path traversal.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ara import engagement_mats as em


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", [
    "acme-portal-2026-04", "abc", "A.B_C-1", "x" * 128, "engagement1",
])
def test_engagement_id_accepts(good: str) -> None:
    em._validate_engagement_id(good)


@pytest.mark.parametrize("bad", [
    "", "-leading", ".dot", "/slash", "has space", "x" * 129,
])
def test_engagement_id_rejects(bad: str) -> None:
    with pytest.raises(em.InvalidEngagementId):
        em._validate_engagement_id(bad)


def test_mat_class_validation() -> None:
    em._validate_mat_class("source")
    em._validate_mat_class("creds")
    with pytest.raises(em.InvalidMatClass):
        em._validate_mat_class("nope")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def test_ensure_engagement_creates_full_layout(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "acme-portal-2026-04")
    root = em.engagement_dir(tmp_path, "acme-portal-2026-04")
    assert root.is_dir()
    for cls in em.MAT_CLASSES:
        d = em.mats_class_dir(tmp_path, "acme-portal-2026-04", cls)
        assert d.is_dir(), f"{cls} dir missing"
    assert em.manifest_path(tmp_path, "acme-portal-2026-04").is_file()


def test_ensure_engagement_is_idempotent(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")
    em.add_mat_bytes(tmp_path, "eng1", "docs", "a.md", b"hello")
    em.ensure_engagement(tmp_path, "eng1")     # second call shouldn't wipe state
    mats = em.list_mats(tmp_path, "eng1")
    assert len(mats) == 1


def test_creds_dir_has_tighter_perms(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")
    creds_dir = em.mats_class_dir(tmp_path, "eng1", "creds")
    docs_dir = em.mats_class_dir(tmp_path, "eng1", "docs")
    creds_mode = stat.S_IMODE(os.stat(creds_dir).st_mode)
    docs_mode = stat.S_IMODE(os.stat(docs_dir).st_mode)
    assert creds_mode == 0o700
    assert docs_mode == 0o755


# ---------------------------------------------------------------------------
# Intake (canonical engagement record)
# ---------------------------------------------------------------------------

def test_intake_roundtrip(tmp_path: Path) -> None:
    payload = {
        "engagement_id": "eng1",
        "operator_id": "ops@acme.com",
        "target": {
            "primary": "https://x", "scope_allows": ["x"], "oos": [],
            "env": "prod",
        },
        "posture": "whitebox",
        "active_testing": True,
    }
    p = em.write_intake(tmp_path, "eng1", payload)
    assert p.is_file()
    loaded = em.read_intake(tmp_path, "eng1")
    assert loaded is not None
    assert loaded["operator_id"] == "ops@acme.com"
    assert loaded["target"]["env"] == "prod"
    assert loaded["schema_version"] == "0.1"


def test_intake_missing_returns_none(tmp_path: Path) -> None:
    assert em.read_intake(tmp_path, "ghost-eng") is None


def test_intake_overwrite_idempotent(tmp_path: Path) -> None:
    em.write_intake(tmp_path, "eng1", {"target": {"primary": "v1"}})
    em.write_intake(tmp_path, "eng1", {"target": {"primary": "v2"}})
    loaded = em.read_intake(tmp_path, "eng1")
    assert loaded is not None and loaded["target"]["primary"] == "v2"


# ---------------------------------------------------------------------------
# Mats — add / list / remove
# ---------------------------------------------------------------------------

def test_add_mat_bytes_writes_file_and_manifest(tmp_path: Path) -> None:
    rec = em.add_mat_bytes(
        tmp_path, "eng1", "docs", "design.md", b"hello world",
        uploaded_by="ops",
    )
    assert rec.path == "docs/design.md"
    assert rec.mat_class == "docs"
    assert rec.bytes == 11
    assert len(rec.sha256) == 64
    fs_path = em.mats_root(tmp_path, "eng1") / "docs" / "design.md"
    assert fs_path.is_file() and fs_path.read_bytes() == b"hello world"
    manifest = em.load_manifest(tmp_path, "eng1")
    assert manifest["mats"][0]["sha256"] == rec.sha256


def test_add_mat_bytes_creds_chmod(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "creds", "test.env", b"PWD=hunter2")
    fs_path = em.mats_root(tmp_path, "eng1") / "creds" / "test.env"
    mode = stat.S_IMODE(os.stat(fs_path).st_mode)
    assert mode == 0o600


def test_add_mat_path_copies_file(tmp_path: Path) -> None:
    src = tmp_path / "_src.md"
    src.write_text("from disk")
    rec = em.add_mat_path(tmp_path, "eng1", "architecture", src)
    assert rec.mat_class == "architecture"
    fs_path = em.mats_root(tmp_path, "eng1") / "architecture" / "_src.md"
    assert fs_path.is_file()
    # Source still exists — we copied, not moved.
    assert src.is_file()


def test_add_mat_path_link_mode(tmp_path: Path) -> None:
    src = tmp_path / "_src.txt"
    src.write_text("forge mat")
    rec = em.add_mat_path(
        tmp_path, "eng1", "source", src, link=True,
    )
    fs_path = em.mats_root(tmp_path, "eng1") / "source" / "_src.txt"
    assert fs_path.is_file()
    # Same inode (hard link) on same filesystem.
    assert os.stat(fs_path).st_ino == os.stat(src).st_ino
    assert "link" in rec.tags


def test_add_mat_replaces_prior_record(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "docs", "x.md", b"v1")
    em.add_mat_bytes(tmp_path, "eng1", "docs", "x.md", b"v2-longer")
    mats = em.list_mats(tmp_path, "eng1")
    assert len(mats) == 1
    assert mats[0].bytes == len(b"v2-longer")


def test_remove_mat_returns_true_on_hit(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "docs", "x.md", b"hi")
    assert em.remove_mat(tmp_path, "eng1", "docs/x.md") is True
    assert em.list_mats(tmp_path, "eng1") == []
    fs_path = em.mats_root(tmp_path, "eng1") / "docs" / "x.md"
    assert not fs_path.exists()


def test_remove_mat_returns_false_on_miss(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")
    assert em.remove_mat(tmp_path, "eng1", "docs/nope.md") is False


def test_list_mats_by_class(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "docs", "a.md", b"a")
    em.add_mat_bytes(tmp_path, "eng1", "docs", "b.md", b"b")
    em.add_mat_bytes(tmp_path, "eng1", "source", "main.py", b"x")
    docs = em.list_mats_by_class(tmp_path, "eng1", "docs")
    src = em.list_mats_by_class(tmp_path, "eng1", "source")
    assert {m.path for m in docs} == {"docs/a.md", "docs/b.md"}
    assert {m.path for m in src} == {"source/main.py"}


def test_populated_classes_excludes_empty(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")
    em.add_mat_bytes(tmp_path, "eng1", "docs", "a.md", b"a")
    em.add_mat_bytes(tmp_path, "eng1", "source", "m.py", b"x")
    pops = em.populated_classes(tmp_path, "eng1")
    assert set(pops) == {"docs", "source"}
    # Returned in canonical MAT_CLASSES order so callers get stable
    # output regardless of upload order.
    assert pops == [c for c in em.MAT_CLASSES if c in pops]


def test_mats_are_selected_by_default_and_can_be_excluded(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "docs", "a.md", b"a")
    em.add_mat_bytes(tmp_path, "eng1", "source", "m.py", b"x")

    mats = {m.path: m for m in em.list_mats(tmp_path, "eng1")}
    assert em.mat_is_selected(mats["docs/a.md"]) is True
    assert em.mat_is_selected(mats["source/m.py"]) is True
    assert set(em.populated_classes(tmp_path, "eng1")) == {"docs", "source"}

    assert em.set_mat_selected(tmp_path, "eng1", "docs/a.md", False) is True
    mats = {m.path: m for m in em.list_mats(tmp_path, "eng1")}
    assert em.mat_is_selected(mats["docs/a.md"]) is False
    assert em.mat_is_selected(mats["source/m.py"]) is True
    assert "mats-excluded" in mats["docs/a.md"].tags
    assert em.populated_classes(tmp_path, "eng1") == ["source"]

    assert em.set_mat_selected(tmp_path, "eng1", "docs/a.md", True) is True
    mats = {m.path: m for m in em.list_mats(tmp_path, "eng1")}
    assert em.mat_is_selected(mats["docs/a.md"]) is True
    assert "mats-excluded" not in mats["docs/a.md"].tags
    assert set(em.populated_classes(tmp_path, "eng1")) == {"docs", "source"}


def test_set_mat_selected_returns_false_for_missing_mat(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")

    assert em.set_mat_selected(tmp_path, "eng1", "docs/missing.md", False) is False


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

def test_filename_sanitization_blocks_traversal(tmp_path: Path) -> None:
    rec = em.add_mat_bytes(
        tmp_path, "eng1", "docs", "../../etc/passwd", b"x",
    )
    # path component stripped; only the basename retained.
    assert rec.path == "docs/passwd"
    fs_path = em.mats_root(tmp_path, "eng1") / "docs" / "passwd"
    assert fs_path.is_file()
    # Nothing landed outside the engagement dir.
    assert not (tmp_path / "etc" / "passwd").exists()


def test_filename_sanitization_replaces_special_chars(tmp_path: Path) -> None:
    rec = em.add_mat_bytes(tmp_path, "eng1", "docs", "weird name!@.md", b"x")
    assert rec.path == "docs/weird_name__.md"


# ---------------------------------------------------------------------------
# MatRecord round-trip
# ---------------------------------------------------------------------------

def test_matrecord_roundtrip() -> None:
    rec = em.MatRecord(
        path="source/main.py",
        mat_class="source",
        sha256="a" * 64,
        bytes=42,
        uploaded_at="2026-04-27T13:00:00Z",
        uploaded_by="ops",
        tags=["link", "verified"],
        note="forge artifact",
    )
    rt = em.MatRecord.from_dict(rec.to_dict())
    assert rt == rec


# ---------------------------------------------------------------------------
# Manifest persistence shape
# ---------------------------------------------------------------------------

def test_forge_deposit_hardlinks_into_engagement_source(tmp_path: Path) -> None:
    """v0.1 §4: forge runs with --engagement <eid> hard-link mats into
    <state>/engagements/<eid>/mats/source/forge-<fid>/ so the trim picks
    them up via populated_classes() without re-uploading."""
    import os
    from ara import armors, engagement_mats as em
    from ara.forge.session import ForgeSession
    from ara.forge.phases import deposit

    armors.create_armor(tmp_path, "blacksmith")
    sess = ForgeSession.new(
        tmp_path, weapon_target_id="x-weapon", operator_id="ops",
        engagement_id="eng1",
    )
    src = tmp_path / "_orig.md"
    src.write_text("forge mat content")
    deposit.deposit_mat(sess, src)

    # Engagement source/ now has a hard-link.
    src_dir = em.mats_class_dir(tmp_path, "eng1", "source")
    landed = list(src_dir.iterdir())
    assert len(landed) == 1
    assert landed[0].name.startswith(f"forge-{sess.forge_id}__")

    # Same inode as the forge's copy.
    forge_mat = tmp_path / "forge" / sess.forge_id / "mats" / "_orig.md"
    assert os.stat(forge_mat).st_ino == os.stat(landed[0]).st_ino

    # Manifest tagged appropriately.
    rec = em.list_mats(tmp_path, "eng1")[0]
    assert "forge" in rec.tags and "link" in rec.tags
    assert rec.uploaded_by == f"forge:{sess.forge_id}"


def test_forge_session_without_engagement_skips_link(tmp_path: Path) -> None:
    """No engagement_id → no engagement dir, no link."""
    from ara import armors, engagement_mats as em
    from ara.forge.session import ForgeSession
    from ara.forge.phases import deposit
    armors.create_armor(tmp_path, "blacksmith")
    sess = ForgeSession.new(
        tmp_path, weapon_target_id="x-weapon", operator_id="ops",
    )
    src = tmp_path / "_orig.md"
    src.write_text("standalone forge mat")
    deposit.deposit_mat(sess, src)
    # No engagement dir was created.
    assert not (tmp_path / "engagements").exists() or \
           list((tmp_path / "engagements").iterdir()) == []


def test_rescan_picks_up_files_dropped_on_disk(tmp_path: Path) -> None:
    em.ensure_engagement(tmp_path, "eng1")
    docs_dir = em.mats_class_dir(tmp_path, "eng1", "docs")
    (docs_dir / "scp-uploaded.md").write_text("# arrived via scp")
    src_dir = em.mats_class_dir(tmp_path, "eng1", "source")
    (src_dir / "main.py").write_text("print('x')")

    res = em.rescan_disk(tmp_path, "eng1")
    assert res["added"] == 2
    assert res["removed"] == 0
    paths = sorted(m.path for m in em.list_mats(tmp_path, "eng1"))
    assert paths == ["docs/scp-uploaded.md", "source/main.py"]


def test_rescan_detects_content_changes(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "docs", "x.md", b"v1")
    fs_path = em.mats_root(tmp_path, "eng1") / "docs" / "x.md"
    fs_path.write_bytes(b"v2-newer")
    res = em.rescan_disk(tmp_path, "eng1")
    assert res["updated"] == 1
    rec = em.list_mats(tmp_path, "eng1")[0]
    assert rec.bytes == len(b"v2-newer")
    assert "rescan-updated" in rec.tags


def test_rescan_prunes_removed_files(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "docs", "ghost.md", b"x")
    fs_path = em.mats_root(tmp_path, "eng1") / "docs" / "ghost.md"
    fs_path.unlink()
    res = em.rescan_disk(tmp_path, "eng1")
    assert res["removed"] == 1
    assert em.list_mats(tmp_path, "eng1") == []


def test_manifest_json_is_sorted_and_stable(tmp_path: Path) -> None:
    em.add_mat_bytes(tmp_path, "eng1", "docs", "z.md", b"z")
    em.add_mat_bytes(tmp_path, "eng1", "docs", "a.md", b"a")
    em.add_mat_bytes(tmp_path, "eng1", "source", "main.py", b"x")
    raw = em.manifest_path(tmp_path, "eng1").read_text()
    data = json.loads(raw)
    paths = [r["path"] for r in data["mats"]]
    assert paths == sorted(paths)   # path-sorted for stable diffs
