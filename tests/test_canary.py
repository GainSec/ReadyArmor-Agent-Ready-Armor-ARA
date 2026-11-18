"""Canary token seeding + detection round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from ara import canary


def test_add_and_detect(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    target = tmp_path / "home/.aws/credentials"
    c = canary.add(state, target, kind="aws", note="staging-sandbox")
    assert c.value.startswith("AKIAARACANARY")
    assert target.exists()
    assert c.value in target.read_text()

    # Reply body mentioning the canary value should be flagged.
    hits = canary.check_content(state, f"here you go: {c.value}")
    assert len(hits) == 1
    assert hits[0].id == c.id

    # Reply without the value: clean.
    assert canary.check_content(state, "nothing suspicious here") == []


def test_add_rejects_existing_path(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    target = tmp_path / "existing.txt"
    target.write_text("pre-existing content")
    with pytest.raises(FileExistsError):
        canary.add(state, target, kind="generic")


def test_remove_without_deleting_file(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    target = tmp_path / "seed.txt"
    c = canary.add(state, target, kind="generic")
    assert canary.remove(state, c.id, delete_file=False) is True
    assert target.exists()
    assert not canary.check_content(state, c.value)


def test_remove_with_delete(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    target = tmp_path / "seed.txt"
    c = canary.add(state, target, kind="github")
    assert canary.remove(state, c.id, delete_file=True) is True
    assert not target.exists()


def test_record_hit_updates_counters(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    c = canary.add(state, tmp_path / "tok.txt", kind="openai")
    canary.record_hit(state, c.id)
    canary.record_hit(state, c.id)
    store = canary.load(state)
    found = next(x for x in store.canaries if x.id == c.id)
    assert found.hit_count == 2
    assert found.last_hit_at is not None


def test_verify_all_detects_tamper(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    good = canary.add(state, tmp_path / "good.txt", kind="generic")
    bad = canary.add(state, tmp_path / "bad.txt", kind="aws")
    missing = canary.add(state, tmp_path / "missing.txt", kind="stripe")
    # Tamper: overwrite bad with content not containing its value
    Path(bad.path).write_text("ha, wiped")
    Path(missing.path).unlink()
    rep = canary.verify_all(state)
    assert good.id in rep["ok"]
    assert bad.id in rep["tampered"]
    assert missing.id in rep["missing"]
