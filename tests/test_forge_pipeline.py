"""End-to-end forge pipeline test: deposit → manifest → exhaust → synthesize → seal.

Exercises the orchestrator without any item-specific channel
implementations (extractions are recorded directly via
:func:`record_extraction`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ara.audit import verify_chain
from ara.forge import state as fstate
from ara.forge.manifest.element import read_manifest
from ara.forge.phases import deposit, exhaust, manifest, seal, synthesize
from ara.forge.session import ForgeConfig, ForgePhase, ForgeSession
from ara.runtime import audit as raudit


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()
    from ara.runtime.channels import registry
    registry.unregister_for_tests()


def _deposit_some_mats(state_root: Path, sess: ForgeSession,
                       sources_dir: Path) -> None:
    md = sources_dir / "report.md"
    md.write_text(
        "# Findings\n\n## Finding A\nXSS in /search\n\n## Finding B\nSQLi in /login\n"
    )
    code = sources_dir / "snippet.py"
    code.write_text("def sanitize(x):\n    return x\n")
    deposit.deposit_mat(sess, md)
    deposit.deposit_mat(sess, code)


def test_full_happy_path(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()

    sess = ForgeSession.new(state_root, weapon_target_id="wstg-baseline-v1",
                            operator_id="op-1",
                            config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    assert sess.phase == ForgePhase.MANIFEST

    elements = manifest.run_manifest(sess)
    assert len(elements) >= 3  # 2 findings + 1 code element
    assert sess.phase == ForgePhase.REVIEW  # auto-advanced

    manifest.approve_manifest(sess)
    assert sess.phase == ForgePhase.EXHAUST

    # Record one extraction per element so coverage = 100%
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess,
            manifest_element_id=el.id,
            channel="extract.proc",
            body={"crumb_id": f"c_{i:03d}", "name": f"crumb {i}",
                  "requires": [], "action": "noop", "emits": []},
            extraction_id=f"x_{i:03d}",
        )

    cov = exhaust.advance_to_review(sess)
    assert sess.phase == ForgePhase.REVIEW
    assert cov.fraction == 1.0

    sess.transition(ForgePhase.SYNTHESIZE, reason="extractions approved")
    draft = synthesize.synthesize(sess)
    assert len(draft.crumbs) == len(elements)

    sess.transition(ForgePhase.REVIEW, reason="synth done")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="weapon-test-1")

    assert result.ok, f"seal failed: {result.failures}"
    assert result.weapon_id == "weapon-test-1"
    assert result.weapon_hash and len(result.weapon_hash) == 64
    weapon_dir = fstate.weapon_dir(state_root, "weapon-test-1")
    assert (weapon_dir / "weapon.json").exists()
    assert (weapon_dir / "seal.json").exists()
    assert sess.phase == ForgePhase.SEALED


def test_resource_extractions_are_sealed_into_weapon(tmp_path: Path) -> None:
    import json as _json

    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(
        state_root,
        weapon_target_id="webapp-recon",
        operator_id="op-1",
        config=ForgeConfig(alphabet_pack=""),
    )
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)

    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess,
            manifest_element_id=el.id,
            channel="extract.proc",
            body={"crumb_id": f"c_{i:03d}", "name": f"crumb {i}",
                  "requires": [], "action": "noop", "emits": []},
            extraction_id=f"x_{i:03d}",
        )
    exhaust.record_extraction(
        sess,
        manifest_element_id=elements[0].id,
        channel="extract.resource",
        body={
            "id": "res_github_wordlists",
            "class": "wordlist",
            "name": "SecLists wordlists",
            "locator": "https://github.com/danielmiessler/SecLists",
            "allowed_uses": ["reference", "fetch"],
            "requires_approval_for": ["execute"],
            "rationale": "Web app recon may need public wordlists.",
        },
        extraction_id="x_resource_001",
    )

    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="extractions approved")
    draft = synthesize.synthesize(sess)

    assert len(draft.resources) == 1
    resource = draft.resources[0]
    assert resource["id"] == "res_github_wordlists"
    assert resource["class"] == "wordlist"
    assert resource["status"] == "resolved"
    assert resource["locator"] == "https://github.com/danielmiessler/SecLists"
    assert resource["resolution"]["source"] == "mat_locator"
    assert resource["allowed_uses"] == ["reference", "fetch"]
    assert resource["requires_approval_for"] == ["execute"]
    assert resource["rationale"] == "Web app recon may need public wordlists."
    assert resource["citation"] == {"manifest_element_id": elements[0].id}
    assert resource["extraction_id"] == "x_resource_001"

    sess.transition(ForgePhase.REVIEW, reason="synth done")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="weapon-webapp-recon")
    assert result.ok, result.failures
    weapon_json = _json.loads(
        (fstate.weapon_dir(state_root, "weapon-webapp-recon") / "weapon.json").read_text()
    )
    assert weapon_json["resources"][0]["locator"] == (
        "https://github.com/danielmiessler/SecLists"
    )
    assert weapon_json["resources"][0]["allowed_uses"] == ["reference", "fetch"]
    bulwark_text = (
        fstate.weapon_dir(state_root, "weapon-webapp-recon")
        / "policy"
        / "bulwark.md"
    ).read_text()
    assert "allow    net   github.com:443" in bulwark_text
    assert "allow    url   https://github.com/danielmiessler/SecLists" in bulwark_text
    assert "resource:res_github_wordlists" in bulwark_text


def test_unresolved_resource_is_sealed_without_policy_allow(
    tmp_path: Path,
) -> None:
    import json as _json

    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(
        state_root,
        weapon_target_id="webapp-recon",
        operator_id="op-1",
        config=ForgeConfig(alphabet_pack=""),
    )
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)

    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess,
            manifest_element_id=el.id,
            channel="extract.proc",
            body={"crumb_id": f"c_{i:03d}", "name": f"crumb {i}",
                  "requires": [], "action": "noop", "emits": []},
            extraction_id=f"x_{i:03d}",
        )
    exhaust.record_extraction(
        sess,
        manifest_element_id=elements[0].id,
        channel="extract.resource",
        body={
            "id": "res_payloads",
            "class": "wordlist",
            "name": "Payload list",
            "hints": ["payload list", "public repo"],
        },
        extraction_id="x_resource_unresolved",
    )

    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="extractions approved")
    draft = synthesize.synthesize(sess)
    assert draft.resources[0]["status"] == "needs_resolution"

    sess.transition(ForgePhase.REVIEW, reason="synth done")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="weapon-needs-resource")
    assert result.ok, result.failures
    weapon_dir = fstate.weapon_dir(state_root, "weapon-needs-resource")
    weapon_json = _json.loads((weapon_dir / "weapon.json").read_text())
    assert weapon_json["resources"][0]["id"] == "res_payloads"
    assert weapon_json["resources"][0]["status"] == "needs_resolution"
    bulwark_path = weapon_dir / "policy" / "bulwark.md"
    bulwark_text = bulwark_path.read_text() if bulwark_path.is_file() else ""
    assert "res_payloads" not in bulwark_text
    assert "allow    net" not in bulwark_text


def test_seal_copies_calibration_queries_when_present(tmp_path: Path) -> None:
    """Operators authoring calibration queries in the forge dir should
    have them carried across into the sealed weapon so
    ``ara weapon calibrate`` works without manual copy."""
    import json as _json
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="wcal",
                             operator_id="op",
                             config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess, manifest_element_id=el.id, channel="extract.proc",
            body={"crumb_id": f"c_{i}", "name": "x", "requires": [],
                  "action": "noop", "emits": []},
            extraction_id=f"x_{i}",
        )
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW, reason="ok")

    # Drop calibration queries into the forge dir before sealing.
    forge_dir = fstate.forge_dir(state_root, sess.forge_id)
    queries_blob = {"queries": [
        {"query": "xss", "expected_top_ids": ["c:any"],
         "min_precision_at_5": 0.2},
    ]}
    (forge_dir / "calibration_queries.json").write_text(
        _json.dumps(queries_blob)
    )

    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="wcal-1")
    assert result.ok
    weapon_dir = fstate.weapon_dir(state_root, "wcal-1")
    cal_path = weapon_dir / "calibration_queries.json"
    assert cal_path.is_file()
    assert _json.loads(cal_path.read_text()) == queries_blob


def test_session_save_persists_config_changes(tmp_path: Path) -> None:
    """ForgeSession.save() exposes _persist for in-place config edits."""
    state_root = tmp_path / "state"
    sess = ForgeSession.new(state_root, weapon_target_id="x", operator_id="op",
                             config=ForgeConfig(alphabet_pack=""))
    sess.config.allow_starter_alphabets = True
    sess.config.coverage_threshold = 0.0
    sess.save()
    sess2 = ForgeSession.load(state_root, sess.forge_id)
    assert sess2.config.allow_starter_alphabets is True
    assert sess2.config.coverage_threshold == 0.0


def test_seal_force_bypasses_coverage_gate(tmp_path: Path) -> None:
    """seal --force downgrades manifest_coverage failures to warnings."""
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="wforce",
                             operator_id="op",
                             config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    # Cover only the FIRST element — coverage will be < 100%
    exhaust.record_extraction(
        sess, manifest_element_id=elements[0].id, channel="extract.proc",
        body={"crumb_id": "c0", "name": "x", "requires": [],
              "action": "noop", "emits": []},
        extraction_id="x_0",
    )
    cov = exhaust.advance_to_review(sess, operator_force=True)
    assert cov.fraction < 1.0
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW, reason="ok")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="wforce-1", force=True)
    assert result.ok
    assert any("[forced past]" in w for w in result.warnings)
    assert any("manifest coverage" in w for w in result.warnings)


def test_seal_without_force_still_blocks_on_coverage(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="wblock",
                             operator_id="op",
                             config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    exhaust.record_extraction(
        sess, manifest_element_id=elements[0].id, channel="extract.proc",
        body={"crumb_id": "c0", "name": "x", "requires": [],
              "action": "noop", "emits": []},
        extraction_id="x_0",
    )
    exhaust.advance_to_review(sess, operator_force=True)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW, reason="ok")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="wblock-1", force=False)
    assert not result.ok
    assert any("manifest coverage" in f for f in result.failures)


def test_seal_force_does_not_bypass_hard_validators(tmp_path: Path) -> None:
    """A blank weapon_target_id is a hard correctness defect; --force
    must NOT let it through."""
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="ok-now",
                             operator_id="op",
                             config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess, manifest_element_id=el.id, channel="extract.proc",
            body={"crumb_id": f"c{i}", "name": "x", "requires": [],
                  "action": "noop", "emits": []},
            extraction_id=f"x_{i}",
        )
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    synthesize.synthesize(sess)
    # Blank out weapon_target_id directly to trigger the hard validator
    sess.weapon_target_id = ""
    sess.save()
    sess.transition(ForgePhase.REVIEW, reason="ok")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="x-1", force=True)
    assert not result.ok
    assert any("weapon_target_id" in f for f in result.failures)


def test_seal_without_calibration_queries(tmp_path: Path) -> None:
    """Seal must work when no calibration queries have been authored."""
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="wnocal",
                             operator_id="op",
                             config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess, manifest_element_id=el.id, channel="extract.proc",
            body={"crumb_id": f"c_{i}", "name": "x", "requires": [],
                  "action": "noop", "emits": []},
            extraction_id=f"x_{i}",
        )
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW, reason="ok")
    seal.request_seal(sess)
    result = seal.seal(sess, weapon_id="wnocal-1")
    assert result.ok
    weapon_dir = fstate.weapon_dir(state_root, "wnocal-1")
    assert not (weapon_dir / "calibration_queries.json").exists()


def test_coverage_gate_blocks_advance(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="w1", operator_id="op", config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    # Only cover 1 of N elements
    exhaust.record_extraction(
        sess, manifest_element_id=elements[0].id, channel="extract.proc",
        body={}, extraction_id="x_0",
    )
    with pytest.raises(exhaust.CoverageError):
        exhaust.advance_to_review(sess)
    # Operator force succeeds
    cov = exhaust.advance_to_review(sess, operator_force=True)
    assert cov.fraction < 1.0
    assert sess.phase == ForgePhase.REVIEW


def test_extraction_invalid_element_id_rejected(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="w1", operator_id="op", config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    with pytest.raises(ValueError, match="not in manifest"):
        exhaust.record_extraction(
            sess, manifest_element_id="elem_bogus0000",
            channel="extract.proc", body={}, extraction_id="x_0",
        )


def test_unknown_channel_rejected(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="w1", operator_id="op", config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    with pytest.raises(ValueError, match="unknown extraction channel"):
        exhaust.record_extraction(
            sess, manifest_element_id=elements[0].id,
            channel="bogus.channel", body={}, extraction_id="x_0",
        )


def test_re_manifest_preserves_drops(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="w1", operator_id="op", config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.drop_element(sess, elements[0].id)
    after = read_manifest(fstate.forge_manifest_path(state_root, sess.forge_id))
    assert after[0].dropped_by_operator is True
    # Re-running manifest preserves the drop
    manifest.run_manifest(sess)
    after2 = read_manifest(fstate.forge_manifest_path(state_root, sess.forge_id))
    drops = [e for e in after2 if e.dropped_by_operator]
    assert len(drops) == 1 and drops[0].id == elements[0].id


def test_seal_blocks_on_empty_weapon_target_id(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="ok", operator_id="op", config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess, manifest_element_id=el.id, channel="extract.proc",
            body={}, extraction_id=f"x_{i}",
        )
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE)
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW)
    # Force empty target
    sess.weapon_target_id = ""
    seal.request_seal(sess)
    result = seal.seal(sess)
    assert not result.ok
    assert any("empty weapon_target_id" in f for f in result.failures)
    # Forge returned to REVIEW
    assert sess.phase == ForgePhase.REVIEW


def test_seal_audit_chain(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="w-good", operator_id="op", config=ForgeConfig(alphabet_pack=""))
    _deposit_some_mats(state_root, sess, sources)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess, manifest_element_id=el.id, channel="extract.proc",
            body={"i": i}, extraction_id=f"x_{i}",
        )
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE)
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW)
    seal.request_seal(sess)
    seal.seal(sess, weapon_id="w-good")
    sink = fstate.forge_audit_path(state_root, sess.forge_id)
    ok, last, err = verify_chain(sink)
    assert ok, err
    assert last >= 6
