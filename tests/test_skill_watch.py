"""Skill change detection round-trips."""

from __future__ import annotations

import json
from pathlib import Path

from ara import skill_watch


def _mk(root: Path, name: str, *, files: dict[str, str]) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def test_snapshot_and_index_round_trip(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _mk(skills_root, "todo", files={"index.js": "export {};",
                                    "pkg/manifest.json": "{}"})
    _mk(skills_root, "summarize", files={"index.js": "console.log('hi');"})
    state = tmp_path / ".ara"
    snap, changes = skill_watch.scan(state, roots=[skills_root])
    assert len(snap.entries) == 2
    # First scan → no prior snapshot → no changes reported.
    assert changes == []
    assert (state / "skill_index.json").exists()


def test_added_skill_fires_event(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _mk(skills_root, "summarize", files={"index.js": "export {};"})
    state = tmp_path / ".ara"
    skill_watch.scan(state, roots=[skills_root])
    # Add a new skill.
    _mk(skills_root, "read-github", files={"index.js": "export {};"})
    _snap, changes = skill_watch.scan(state, roots=[skills_root])
    assert len(changes) == 1
    assert changes[0].kind == "added"
    assert changes[0].name == "read-github"
    events = skill_watch.changes_to_events(changes)
    assert events[0]["kind"] == "skill_changed"
    assert events[0]["change"] == "added"
    assert events[0]["name"] == "read-github"


def test_modified_skill_fires_event(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _mk(skills_root, "todo", files={"index.js": "v1"})
    state = tmp_path / ".ara"
    skill_watch.scan(state, roots=[skills_root])
    (skills_root / "todo" / "index.js").write_text("v2")
    _snap, changes = skill_watch.scan(state, roots=[skills_root])
    assert any(c.kind == "modified" and c.name == "todo" for c in changes)


def test_removed_skill_fires_event(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _mk(skills_root, "doomed", files={"index.js": "export {};"})
    _mk(skills_root, "keeper", files={"index.js": "export {};"})
    state = tmp_path / ".ara"
    skill_watch.scan(state, roots=[skills_root])
    import shutil
    shutil.rmtree(skills_root / "doomed")
    _snap, changes = skill_watch.scan(state, roots=[skills_root])
    assert any(c.kind == "removed" and c.name == "doomed" for c in changes)


def test_env_override_roots(tmp_path: Path, monkeypatch) -> None:
    skills_root = tmp_path / "custom"
    _mk(skills_root, "extra", files={"index.js": "export {};"})
    monkeypatch.setenv("ARA_SKILL_ROOTS", str(skills_root))
    roots = skill_watch.resolve_roots()
    assert roots == [skills_root]
    snap = skill_watch.snapshot()
    assert any(k.endswith(":extra") for k in snap.entries)


def test_large_file_hashed_by_metadata(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _mk(skills_root, "bulky", files={"meta.json": "{}"})
    # Write a 6 MB file — over the max_file threshold.
    (skills_root / "bulky" / "model.bin").write_bytes(b"\x00" * (6 * 1024 * 1024))
    fp = skill_watch._fingerprint_dir(skills_root / "bulky")
    assert fp.files_count == 2
    assert fp.bytes_total == 6 * 1024 * 1024 + 2


def test_snapshot_ignores_dot_entries(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _mk(skills_root, ".hidden", files={"index.js": ""})
    _mk(skills_root, "visible", files={"index.js": ""})
    snap = skill_watch.snapshot(roots=[skills_root])
    keys = list(snap.entries.keys())
    assert any("visible" in k for k in keys)
    assert not any(".hidden" in k for k in keys)
