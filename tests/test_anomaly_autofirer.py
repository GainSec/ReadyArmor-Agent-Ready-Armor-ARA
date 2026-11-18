"""Tests for runtime.anomaly.autofire."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.anomaly import autofire as af
from ara.runtime.anomaly.autofire import AnomalyAutoFirer
from ara.runtime.anomaly.channels import install_anomaly_channels
from ara.runtime.anomaly.crumb_tracker import (
    CrumbTracker,
    for_session,
    reset_for_tests as reset_crumb_tracker,
)
from ara.runtime.anomaly.shape import (
    ExpectedEvidenceShape,
    FieldRequirement,
)
from ara.runtime.anomaly.weapon_shapes import WeaponShapes, load_from_weapon
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.bootstrap import (
    install_engagement,
    install_engagement_channels,
)
from ara.runtime.channels import registry


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    registry.unregister_for_tests()
    raudit.reset_registry_for_tests()
    af.reset_hooks_for_tests()
    reset_crumb_tracker()
    yield
    registry.unregister_for_tests()
    raudit.reset_registry_for_tests()
    af.reset_hooks_for_tests()
    reset_crumb_tracker()


# ---------- crumb tracker ----------

def test_crumb_tracker_set_and_clear() -> None:
    t = CrumbTracker()
    assert t.current() is None
    t.set_active("c1")
    assert t.current() == "c1"
    t.set_active("c2")
    assert t.current() == "c2"
    t.clear()
    assert t.current() is None


def test_for_session_singleton(tmp_path: Path) -> None:
    a = for_session(tmp_path / "eng")
    b = for_session(tmp_path / "eng")
    assert a is b


# ---------- weapon shapes ----------

def test_load_from_weapon_with_no_shapes(tmp_path: Path) -> None:
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    (weapon_dir / "weapon.json").write_text(json.dumps({
        "crumbs": [{"crumb_id": "c1", "name": "no shape"}],
    }))
    shapes = load_from_weapon(weapon_dir)
    assert shapes.get("c1") is None
    assert shapes.known_crumb_ids() == []


def test_load_from_weapon_parses_shapes(tmp_path: Path) -> None:
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    (weapon_dir / "weapon.json").write_text(json.dumps({
        "crumbs": [{
            "crumb_id": "c1", "name": "x",
            "expected_evidence_shape": {
                "required_fields": [{"name": "$.plugins", "cardinality": "one_or_more"}],
                "divergence_threshold": 0.4,
            },
        }, {
            "crumb_id": "c2", "name": "no shape",
        }],
    }))
    shapes = load_from_weapon(weapon_dir)
    assert shapes.has("c1")
    assert not shapes.has("c2")
    s = shapes.get("c1")
    assert s is not None
    assert s.divergence_threshold == 0.4


def test_load_from_weapon_skips_malformed_shape(tmp_path: Path) -> None:
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    # required_fields expects a list of dicts; an int is a TypeError
    # at FieldRequirement construction.
    (weapon_dir / "weapon.json").write_text(json.dumps({
        "crumbs": [{"crumb_id": "c1",
                    "expected_evidence_shape": {"required_fields": [42]}}],
    }))
    shapes = load_from_weapon(weapon_dir)
    # Malformed → not loaded; auto-firer will skip silently
    assert not shapes.has("c1")


# ---------- auto-firer: shape-less + no-crumb skips ----------

def _make_firer(tmp_path: Path,
                 shapes_dict: dict[str, ExpectedEvidenceShape] | None = None,
                 ) -> tuple[AnomalyAutoFirer, EvidenceBinder, Path]:
    sessions_root = tmp_path / "sessions"
    eng = "eng-1"
    session_dir = sessions_root / eng
    session_dir.mkdir(parents=True)
    install_anomaly_channels(sessions_root=sessions_root)
    tracker = for_session(session_dir)
    shapes = WeaponShapes(shapes_dict or {})
    firer = AnomalyAutoFirer(
        sessions_root=sessions_root,
        engagement_id=eng,
        crumb_tracker=tracker,
        weapon_shapes=shapes,
    )
    af.register_hook(session_dir, firer.on_evidence)
    binder = EvidenceBinder(session_dir)
    return firer, binder, session_dir


def test_skip_when_no_active_crumb(tmp_path: Path) -> None:
    firer, binder, _ = _make_firer(tmp_path)
    binder.put(b"x", source="tool:curl", source_metadata={}, summary="s",
                activation_id="a")
    assert firer.stats.checks == 1
    assert firer.stats.skipped_no_crumb == 1
    assert firer.stats.fired == 0


def test_skip_when_crumb_has_no_shape(tmp_path: Path) -> None:
    firer, binder, _ = _make_firer(tmp_path)
    firer.crumb_tracker.set_active("c_no_shape")
    binder.put(b"x", source="tool:curl", source_metadata={}, summary="s",
                activation_id="a")
    assert firer.stats.skipped_no_shape == 1
    assert firer.stats.fired == 0


# ---------- auto-firer: above-threshold fires anomaly.emit ----------

def test_fires_anomaly_emit_when_divergent(tmp_path: Path) -> None:
    shape = ExpectedEvidenceShape(
        required_fields=[FieldRequirement(name="$.plugins", cardinality="one_or_more")],
        divergence_threshold=0.1,  # very strict — any miss fires
    )
    firer, binder, session_dir = _make_firer(tmp_path, {"c1": shape})
    firer.crumb_tracker.set_active("c1")
    # Evidence missing the required field → divergence > threshold
    binder.put(b'{"users": []}', source="tool:curl", source_metadata={},
                summary="s", activation_id="a")
    assert firer.stats.fired == 1
    # Anomaly recorded via the channel
    from ara.runtime.anomaly.record import AnomalyStore
    store = AnomalyStore(session_dir)
    pending = store.by_status("pending")
    assert len(pending) == 1
    assert pending[0].crumb_id == "c1"
    assert pending[0].divergence["raw_score"] > 0


def test_telemetry_only_below_threshold(tmp_path: Path) -> None:
    shape = ExpectedEvidenceShape(
        required_fields=[],   # nothing required
        unexpected_fields_threshold=10,
        divergence_threshold=0.5,
    )
    firer, binder, session_dir = _make_firer(tmp_path, {"c1": shape})
    firer.crumb_tracker.set_active("c1")
    binder.put(b'{"plugins": []}', source="tool:curl", source_metadata={},
                summary="s", activation_id="a")
    assert firer.stats.sub_threshold == 1
    assert firer.stats.fired == 0
    # Telemetry log written
    tel = session_dir / "divergence_telemetry.jsonl"
    assert tel.exists()
    line = json.loads(tel.read_text().splitlines()[0])
    assert line["crumb_id"] == "c1"
    assert "raw_score" in line
    # Anomaly NOT created
    from ara.runtime.anomaly.record import AnomalyStore
    store = AnomalyStore(session_dir)
    assert store.all() == []


# ---------- auto-firer: crash safety ----------

def test_compute_divergence_crash_audited_and_skipped(tmp_path: Path) -> None:
    """Force a divergence-compute crash by injecting a shape whose
    line_matcher has a regex that triggers a runtime error."""
    shape = ExpectedEvidenceShape(divergence_threshold=0.5)
    firer, binder, session_dir = _make_firer(tmp_path, {"c1": shape})
    firer.crumb_tracker.set_active("c1")

    # Monkey-patch compute_divergence to raise once
    from ara.runtime.anomaly import autofire
    original = autofire.compute_divergence

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic compute crash")

    autofire.compute_divergence = boom  # type: ignore[assignment]
    try:
        binder.put(b"x", source="tool:curl", source_metadata={},
                    summary="s", activation_id="a")
    finally:
        autofire.compute_divergence = original  # type: ignore[assignment]

    assert firer.stats.errors == 1
    assert firer.stats.fired == 0
    audit = session_dir / "events.jsonl"
    line = json.loads(audit.read_text().splitlines()[0])
    assert line["kind"] == "auto_firer.error"
    assert "synthetic compute crash" in line["body"]["reason"]


# ---------- bootstrap integration ----------

def test_install_engagement_returns_wiring(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    (weapon_dir / "weapon.json").write_text(json.dumps({
        "crumbs": [{
            "crumb_id": "c1",
            "expected_evidence_shape": {
                "required_fields": [{"name": "$.plugins", "cardinality": "one_or_more"}],
                "divergence_threshold": 0.1,
            },
        }],
    }))
    wiring = install_engagement(
        sessions_root=sessions_root,
        engagement_id="eng-1",
        weapon_dir=weapon_dir,
    )
    assert wiring.crumb_tracker is not None
    assert wiring.autofirer is not None
    assert wiring.weapon_shapes is not None
    assert wiring.weapon_shapes.has("c1")
    assert "fact.assert" in wiring.channels
    assert "anomaly.emit" in wiring.channels


def test_e2e_via_install_engagement(tmp_path: Path) -> None:
    """End-to-end: install_engagement wires everything, then setting
    the active crumb + capturing diverging evidence fires anomaly.emit."""
    sessions_root = tmp_path / "sessions"
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    (weapon_dir / "weapon.json").write_text(json.dumps({
        "crumbs": [{
            "crumb_id": "c_probe",
            "expected_evidence_shape": {
                "required_fields": [{"name": "$.plugins", "cardinality": "one_or_more"}],
                "divergence_threshold": 0.1,
            },
        }],
    }))
    wiring = install_engagement(
        sessions_root=sessions_root,
        engagement_id="eng-1",
        weapon_dir=weapon_dir,
    )
    wiring.crumb_tracker.set_active("c_probe")
    binder = EvidenceBinder(sessions_root / "eng-1")
    binder.put(b'{"users": []}', source="tool:curl", source_metadata={},
                summary="s", activation_id="a-1")
    assert wiring.autofirer.stats.fired == 1


def test_install_engagement_channels_optional_autofirer(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    chans = install_engagement_channels(
        sessions_root=sessions_root,
        engagement_id="eng-1",
        install_anomaly_autofirer=True,
    )
    # Channels installed; auto-firer registered as a hook
    assert "anomaly.emit" in chans
    eng_dir = sessions_root / "eng-1"
    assert af.hooks_for(eng_dir)


def test_install_anomaly_autofirer_requires_engagement_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="engagement_id"):
        install_engagement_channels(
            sessions_root=tmp_path / "sessions",
            install_anomaly_autofirer=True,
        )


# ---------- crumb-swap mid-engagement changes the lookup ----------

def test_crumb_swap_routes_evidence_to_new_shape(tmp_path: Path) -> None:
    shape_a = ExpectedEvidenceShape(
        required_fields=[FieldRequirement(name="$.plugins", cardinality="one_or_more")],
        divergence_threshold=0.1,
    )
    shape_b = ExpectedEvidenceShape(
        required_fields=[],   # tolerant
        unexpected_fields_threshold=10,
        divergence_threshold=0.5,
    )
    firer, binder, _ = _make_firer(tmp_path, {"a": shape_a, "b": shape_b})

    firer.crumb_tracker.set_active("a")
    binder.put(b'{"x": 1}', source="tool:curl", source_metadata={},
                summary="s", activation_id="a-1")
    assert firer.stats.fired == 1

    firer.crumb_tracker.set_active("b")
    binder.put(b'{"plugins": []}', source="tool:curl", source_metadata={},
                summary="s", activation_id="a-2")
    # Below threshold under shape_b → telemetry, not fire
    assert firer.stats.fired == 1
    assert firer.stats.sub_threshold == 1
