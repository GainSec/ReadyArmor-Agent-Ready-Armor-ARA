"""Tests for the v0.1 §10 fast-fail stop-watcher hook on FindingFileChannel.

When a finding lands, the channel synchronously consults
``stop_watcher.evaluate_and_latch`` so the next tool call across any
session sees the tripped latch — without waiting for the next supervisor
tick.
"""
from __future__ import annotations

from pathlib import Path

from ara import armors as armors_mod
from ara import trims as trims_mod
from ara.runtime import stop_watcher as sw
from ara.runtime.findings.channels import _check_stop_conditions_and_latch
from ara.runtime.findings.record import Finding
from ara.runtime.findings.store import FindingStore


class _FakeCtx:
    engagement_id = "session-1"


def _setup_state_with_trim(
    tmp_path: Path, *, stop_conditions: list[str] | None = None,
    eng_id: str = "eng1",
) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    if stop_conditions is None:
        return
    eng = {
        "engagement_id": eng_id,
        "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": "nonprod"},
        "test_window": {
            "start": "2026-04-27T00:00:00Z",
            "end":   "2026-05-04T00:00:00Z",
        },
        "stop_conditions": stop_conditions,
        "active_testing": True,
    }
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement=eng)
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)


def _seed_finding(store_dir: Path, *, severity_name: str) -> Finding:
    f = Finding(
        id="f1", title="t", category="cat", severity_cell=("a", "b"),
        severity_score=9.0, severity_name=severity_name,
        fix_template_id=None, fix_custom=None,
        evidence_refs=[], fact_refs=[], lead_refs=[],
        provenance_chain=[], impact={}, affected_assets=[],
        origin="agent_derived", status="draft", activation_id="a",
        created_at=0.0, category_proposed=None, notes=None,
    )
    store = FindingStore(store_dir)
    store.append(f)
    return f


# ---------------------------------------------------------------------------
# Hook firing semantics
# ---------------------------------------------------------------------------

def test_hook_no_op_when_no_armor(tmp_path: Path) -> None:
    """No active armor → no trim → no stop-conditions to consult.
    Hook must return cleanly without raising."""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(parents=True)
    store_dir = sessions_root / "session-1"
    store_dir.mkdir()
    _seed_finding(store_dir, severity_name="critical")
    store = FindingStore(store_dir)
    _check_stop_conditions_and_latch(sessions_root, _FakeCtx(), store)
    # Latch should NOT have been written for any engagement.
    assert not (tmp_path / "stop_state").exists()


def test_hook_no_op_when_trim_has_no_stop_conditions(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(parents=True)
    store_dir = sessions_root / "session-1"
    store_dir.mkdir()
    _setup_state_with_trim(tmp_path, stop_conditions=None)
    _seed_finding(store_dir, severity_name="critical")
    store = FindingStore(store_dir)
    _check_stop_conditions_and_latch(sessions_root, _FakeCtx(), store)
    latch = sw.load_latch(tmp_path, "eng1")
    assert latch.tripped is False


def test_hook_trips_latch_on_first_critical(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(parents=True)
    store_dir = sessions_root / "session-1"
    store_dir.mkdir()
    _setup_state_with_trim(tmp_path, stop_conditions=["first_critical"])
    _seed_finding(store_dir, severity_name="critical")
    store = FindingStore(store_dir)
    _check_stop_conditions_and_latch(sessions_root, _FakeCtx(), store)
    latch = sw.load_latch(tmp_path, "eng1")
    assert latch.tripped is True
    assert latch.condition == "first_critical"


def test_hook_does_not_trip_on_low_severity(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(parents=True)
    store_dir = sessions_root / "session-1"
    store_dir.mkdir()
    _setup_state_with_trim(tmp_path, stop_conditions=["first_critical"])
    _seed_finding(store_dir, severity_name="low")
    store = FindingStore(store_dir)
    _check_stop_conditions_and_latch(sessions_root, _FakeCtx(), store)
    latch = sw.load_latch(tmp_path, "eng1")
    assert latch.tripped is False


def test_hook_max_findings_trip(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(parents=True)
    store_dir = sessions_root / "session-1"
    store_dir.mkdir()
    _setup_state_with_trim(tmp_path, stop_conditions=["max_findings:2"])
    # Seed 2 findings (any severity).
    for i in range(2):
        f = Finding(
            id=f"f{i}", title=f"t{i}", category="cat",
            severity_cell=("a", "b"), severity_score=4.0,
            severity_name="medium",
            fix_template_id=None, fix_custom=None,
            evidence_refs=[], fact_refs=[], lead_refs=[],
            provenance_chain=[], impact={}, affected_assets=[],
            origin="agent_derived", status="draft", activation_id="a",
            created_at=0.0, category_proposed=None, notes=None,
        )
        FindingStore(store_dir).append(f)
    store = FindingStore(store_dir)
    _check_stop_conditions_and_latch(sessions_root, _FakeCtx(), store)
    latch = sw.load_latch(tmp_path, "eng1")
    assert latch.tripped is True
    assert latch.condition == "max_findings:2"


def test_hook_idempotent_when_already_latched(tmp_path: Path) -> None:
    """Second finding into a tripped engagement leaves the latch alone."""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(parents=True)
    store_dir = sessions_root / "session-1"
    store_dir.mkdir()
    _setup_state_with_trim(tmp_path, stop_conditions=["first_critical"])
    _seed_finding(store_dir, severity_name="critical")
    store = FindingStore(store_dir)
    _check_stop_conditions_and_latch(sessions_root, _FakeCtx(), store)
    latch1 = sw.load_latch(tmp_path, "eng1")
    assert latch1.tripped is True
    first_ts = latch1.tripped_at

    # Second call doesn't reset.
    _check_stop_conditions_and_latch(sessions_root, _FakeCtx(), store)
    latch2 = sw.load_latch(tmp_path, "eng1")
    assert latch2.tripped is True
    assert latch2.tripped_at == first_ts
