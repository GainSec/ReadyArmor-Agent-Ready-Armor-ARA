"""End-to-end tests for the overlay-append policy hot-swap.

ARA is already running with the operator's standing config. Weapons
layer ON TOP via sentinel-bracketed overlay sections — equip appends,
unequip strips between markers, operator's surrounding text untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara import weapons as weapons_mod
from ara.forge.alphabets.pack import load_builtin_pack
from ara.forge.phases import deposit, exhaust, manifest, seal, synthesize
from ara.forge.session import ForgeConfig, ForgePhase, ForgeSession
from ara.runtime import audit as raudit
from ara.runtime.channels import registry


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()
    yield
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


# ---------- pack policy_defaults ----------

def test_offsec_pack_ships_policy_defaults() -> None:
    p = load_builtin_pack("offsec")
    assert "deny_path" in p.policy_defaults["walls"]
    assert "bulwark" in p.policy_defaults
    assert "rhema" in p.policy_defaults


# ---------- pack → synthesize → seal ----------

def _build_forge(tmp_path: Path) -> ForgeSession:
    state_root = tmp_path / "state"
    src = tmp_path / "src"
    src.mkdir()
    mat = src / "report.md"
    mat.write_text(
        "# Findings\n\n## Finding A — XSS\nReflected.\n## Remediation\nEncode.\n"
    )
    sess = ForgeSession.new(
        state_root, weapon_target_id="w-overlay",
        operator_id="op",
        config=ForgeConfig(alphabet_pack="offsec"),
    )
    deposit.deposit_mat(sess, mat)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    for i, el in enumerate(elements):
        exhaust.record_extraction(
            sess, manifest_element_id=el.id, channel="extract.proc",
            body={"crumb_id": f"c_{i}", "name": "x"},
            extraction_id=f"x_proc_{i}",
        )
    exhaust.record_extraction(
        sess, manifest_element_id=elements[0].id, channel="extract.conc",
        body={"alphabet_name": "category",
              "entry": {"id": "xss.reflected", "label": "X"}},
        extraction_id="x_cat",
    )
    exhaust.record_extraction(
        sess, manifest_element_id=elements[0].id, channel="extract.conc",
        body={"alphabet_name": "severity_rubric",
              "coords": {"data_class": "PII", "access": "read"},
              "cell": {"score": 6.0, "bucket": "medium"}},
        extraction_id="x_rubric",
    )
    exhaust.record_extraction(
        sess, manifest_element_id=elements[0].id, channel="extract.conc",
        body={"alphabet_name": "fix_library",
              "entry": {"id": "fix.xss.encode", "title": "Encode",
                        "applicable_categories": ["xss.reflected"],
                        "body": "encode"}},
        extraction_id="x_fix",
    )
    return sess


def test_synthesize_copies_pack_policy_into_draft(tmp_path: Path) -> None:
    sess = _build_forge(tmp_path)
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    draft = synthesize.synthesize(sess)
    assert "walls" in draft.policy
    assert "bulwark" in draft.policy
    assert "rhema" in draft.policy


def test_seal_writes_policy_files(tmp_path: Path) -> None:
    sess = _build_forge(tmp_path)
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW, reason="synth done")
    seal.request_seal(sess)
    seal.seal(sess, weapon_id="w-overlay")
    pdir = weapons_mod.policy_dir_for(sess.state_root, "w-overlay")
    assert (pdir / "WALLS.md").exists()
    assert (pdir / "bulwark.md").exists()


# ---------- overlay equip/unequip semantics ----------

def _seal_weapon_with_policy(state_root: Path, weapon_id: str, *,
                              walls_text: str | None = None,
                              bulwark_text: str | None = None,
                              rhema_text: str | None = None) -> Path:
    wdir = weapons_mod.weapon_dir(state_root, weapon_id)
    pdir = wdir / "policy"
    pdir.mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({"weapon_target_id": weapon_id}))
    (wdir / "seal.json").write_text(json.dumps({"weapon_id": weapon_id}))
    if walls_text is not None:
        (pdir / "WALLS.md").write_text(walls_text)
    if bulwark_text is not None:
        (pdir / "bulwark.md").write_text(bulwark_text)
    if rhema_text is not None:
        (pdir / "rhema.md").write_text(rhema_text)
    return wdir


def test_equip_appends_overlay_below_operator_walls(tmp_path: Path) -> None:
    operator_text = (
        "# Operator's standing WALLS\n\n## deny_path\n~/.ssh/**\n~\n"
    )
    (tmp_path / "WALLS.md").write_text(operator_text)
    _seal_weapon_with_policy(
        tmp_path, "w1", walls_text="## deny_path\n~/.aws/**\n",
    )
    summary = weapons_mod.equip_policy(tmp_path, "w1")
    assert "WALLS.md" in summary["overlaid"]
    final = (tmp_path / "WALLS.md").read_text()
    assert final.startswith(operator_text)
    assert "# ARA-WEAPON-OVERLAY-BEGIN w1" in final
    assert "# ARA-WEAPON-OVERLAY-END" in final
    assert "~/.aws/**" in final
    assert final.index("~/.ssh/**") < final.index("~/.aws/**")


def test_unequip_strips_overlay_preserves_operator(tmp_path: Path) -> None:
    operator_text = "# Operator's standing WALLS\n\n## deny_path\n~/.ssh/**\n"
    (tmp_path / "WALLS.md").write_text(operator_text)
    _seal_weapon_with_policy(
        tmp_path, "w1", walls_text="## deny_path\n~/.aws/**\n",
    )
    weapons_mod.equip_policy(tmp_path, "w1")
    summary = weapons_mod.unequip_policy(tmp_path)
    assert "WALLS.md" in summary["stripped"]
    after = (tmp_path / "WALLS.md").read_text()
    assert after.rstrip("\n") == operator_text.rstrip("\n")
    assert "ARA-WEAPON-OVERLAY" not in after


def test_re_equip_replaces_prior_overlay(tmp_path: Path) -> None:
    operator_text = "# operator\n\n## deny_path\n~/.ssh/**\n"
    (tmp_path / "WALLS.md").write_text(operator_text)
    # Use unambiguous body lines so substring checks below don't
    # collide with the `# ARA-WEAPON-OVERLAY-BEGIN` marker prefix.
    _seal_weapon_with_policy(tmp_path, "wA",
                             walls_text="## deny_path\n# weapon-alpha\n")
    _seal_weapon_with_policy(tmp_path, "wB",
                             walls_text="## deny_path\n# weapon-beta\n")
    weapons_mod.equip_policy(tmp_path, "wA")
    weapons_mod.equip_policy(tmp_path, "wB")
    text = (tmp_path / "WALLS.md").read_text()
    assert text.count("# operator") == 1
    assert "BEGIN wB" in text
    assert "BEGIN wA" not in text
    assert "# weapon-beta" in text
    assert "# weapon-alpha" not in text


def test_re_equip_same_weapon_idempotent(tmp_path: Path) -> None:
    (tmp_path / "WALLS.md").write_text("# op\n")
    _seal_weapon_with_policy(tmp_path, "w1", walls_text="# weapon\n")
    weapons_mod.equip_policy(tmp_path, "w1")
    first = (tmp_path / "WALLS.md").read_text()
    weapons_mod.equip_policy(tmp_path, "w1")
    second = (tmp_path / "WALLS.md").read_text()
    assert first == second
    assert second.count("BEGIN w1") == 1


def test_equip_with_no_existing_walls_creates_overlay_only_file(tmp_path: Path) -> None:
    _seal_weapon_with_policy(tmp_path, "w1", walls_text="## deny_path\n*\n")
    weapons_mod.equip_policy(tmp_path, "w1")
    text = (tmp_path / "WALLS.md").read_text()
    # Empty operator base → file starts cleanly with the begin marker
    assert text.startswith(weapons_mod._OVERLAY_BEGIN_PREFIX)
    assert "BEGIN w1" in text
    assert "OVERLAY-END" in text


def test_unequip_with_no_overlay_is_noop(tmp_path: Path) -> None:
    text = "# operator only\n## deny_path\n*\n"
    (tmp_path / "WALLS.md").write_text(text)
    summary = weapons_mod.unequip_policy(tmp_path)
    assert summary["stripped"] == []
    assert "WALLS.md" in summary["untouched"]
    assert (tmp_path / "WALLS.md").read_text() == text


def test_equip_skips_kinds_weapon_doesnt_ship(tmp_path: Path) -> None:
    (tmp_path / "bulwark.md").write_text("# operator bulwark\n")
    _seal_weapon_with_policy(tmp_path, "w1", walls_text="# w1\n")
    summary = weapons_mod.equip_policy(tmp_path, "w1")
    assert "bulwark.md" in summary["kept"]
    assert (tmp_path / "bulwark.md").read_text() == "# operator bulwark\n"


def test_strip_overlay_handles_multiple_blocks() -> None:
    text = (
        "# op\n\n"
        "<!-- ARA-WEAPON-OVERLAY-BEGIN wA -->\n# a\n<!-- ARA-WEAPON-OVERLAY-END -->\n"
        "operator middle\n\n"
        "<!-- ARA-WEAPON-OVERLAY-BEGIN wB -->\n# b\n<!-- ARA-WEAPON-OVERLAY-END -->\n"
        "operator tail\n"
    )
    out = weapons_mod._strip_overlay(text)
    assert "wA" not in out and "wB" not in out
    assert "operator middle" in out
    assert "operator tail" in out


def test_strip_overlay_handles_missing_end() -> None:
    text = "# op\n<!-- ARA-WEAPON-OVERLAY-BEGIN w1 -->\n# half\n"
    out = weapons_mod._strip_overlay(text)
    assert "OVERLAY" not in out
    assert out.startswith("# op")


def test_equip_writes_bulwark_native_marker_style(tmp_path: Path) -> None:
    """Current-style markers are bulwark-comment friendly (start with
    ``# ``, no ``<!-- -->``) so the bulwark parser ignores them."""
    _seal_weapon_with_policy(tmp_path, "w1", walls_text="# weapon\n")
    weapons_mod.equip_policy(tmp_path, "w1")
    text = (tmp_path / "WALLS.md").read_text()
    assert "# ARA-WEAPON-OVERLAY-BEGIN w1" in text
    assert "# ARA-WEAPON-OVERLAY-END" in text
    # No HTML markers in fresh writes
    assert "<!--" not in text


def test_equip_produces_bulwark_md_that_parses_cleanly(tmp_path: Path) -> None:
    """Regression: pre-fix HTML markers caused
    ``bulwark.parse_errors`` to surface two ``unknown action '<!--'``
    entries on every weapon equip."""
    from ara import bulwark
    operator = "## global\ndeny fs **/.ssh/**\n"
    (tmp_path / "bulwark.md").write_text(operator)
    _seal_weapon_with_policy(
        tmp_path, "w1",
        bulwark_text="## global\ndeny net api.evil.example\n",
    )
    weapons_mod.equip_policy(tmp_path, "w1")
    bw = bulwark.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()
    assert len(bw.rules) == 2  # operator deny + weapon deny
    layers = sorted(r.layer for r in bw.rules)
    assert layers == ["fs", "net"]


def test_strip_overlay_handles_legacy_html_markers() -> None:
    """Backward compat: operator state with the old HTML-style
    markers gets cleaned up on the next equip cycle."""
    legacy = (
        "# operator\n\n"
        "<!-- ARA-WEAPON-OVERLAY-BEGIN wOld -->\n"
        "# old weapon body\n"
        "<!-- ARA-WEAPON-OVERLAY-END -->\n"
        "# operator tail\n"
    )
    out = weapons_mod._strip_overlay(legacy)
    assert "wOld" not in out
    assert "old weapon body" not in out
    assert "operator" in out
    assert "operator tail" in out


def test_re_equip_replaces_legacy_html_overlay_with_native(tmp_path: Path) -> None:
    """An operator upgrading from a build that wrote HTML markers
    finds them stripped + replaced with native markers on first
    re-equip — no manual migration step."""
    legacy_state = (
        "# operator\n\n"
        "<!-- ARA-WEAPON-OVERLAY-BEGIN wOld -->\n"
        "# old weapon body\n"
        "<!-- ARA-WEAPON-OVERLAY-END -->\n"
    )
    (tmp_path / "WALLS.md").write_text(legacy_state)
    _seal_weapon_with_policy(tmp_path, "wNew", walls_text="# new weapon\n")
    weapons_mod.equip_policy(tmp_path, "wNew")
    text = (tmp_path / "WALLS.md").read_text()
    assert "<!--" not in text
    assert "wOld" not in text
    assert "# ARA-WEAPON-OVERLAY-BEGIN wNew" in text
    assert "# new weapon" in text
    assert "# operator" in text


# ---------- supervisor watcher sees the merged file ----------

def test_walls_parser_reads_both_operator_and_overlay(tmp_path: Path) -> None:
    operator_text = "## deny_path\n~/.ssh/**\n"
    (tmp_path / "WALLS.md").write_text(operator_text)
    _seal_weapon_with_policy(
        tmp_path, "w1", walls_text="## deny_path\n~/.aws/**\n",
    )
    weapons_mod.equip_policy(tmp_path, "w1")
    from ara.walls import parse_file
    walls = parse_file(tmp_path / "WALLS.md")
    patterns = [r.pattern for r in walls.path_rules]
    raw = [r.raw for r in walls.path_rules]
    assert any("~/.ssh" in p or "~/.ssh" in r for p, r in zip(patterns, raw))
    assert any("~/.aws" in p or "~/.aws" in r for p, r in zip(patterns, raw))


# ---------- CLI ----------

def test_cli_equip_surfaces_overlay_summary(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    _seal_weapon_with_policy(
        tmp_path, "w1",
        walls_text="# w1 walls\n",
        bulwark_text="# w1 bulwark\n",
    )
    rc = main(["weapon", "--state-root", str(tmp_path), "equip", "w1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["equipped_weapon"] == "w1"
    assert "WALLS.md" in out["policy_overlaid"]
    assert "bulwark.md" in out["policy_overlaid"]
    assert "rhema.md" in out["policy_kept"]


def test_cli_unequip_surfaces_strip_summary(tmp_path: Path, capsys) -> None:
    from ara.cli import main
    (tmp_path / "WALLS.md").write_text("# op\n")
    _seal_weapon_with_policy(tmp_path, "w1", walls_text="# w1\n")
    main(["weapon", "--state-root", str(tmp_path), "equip", "w1"])
    capsys.readouterr()
    rc = main(["weapon", "--state-root", str(tmp_path), "unequip"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["equipped_weapon"] is None
    assert "WALLS.md" in out["policy_stripped"]
    assert (tmp_path / "WALLS.md").read_text().rstrip("\n") == "# op"


# ---------- end-to-end pack→equipped layered onto operator ----------

def test_full_pipeline_overlays_offsec_on_operator_base(tmp_path: Path) -> None:
    sess = _build_forge(tmp_path)
    exhaust.advance_to_review(sess)
    sess.transition(ForgePhase.SYNTHESIZE, reason="ok")
    synthesize.synthesize(sess)
    sess.transition(ForgePhase.REVIEW, reason="synth done")
    seal.request_seal(sess)
    seal.seal(sess, weapon_id="w-overlay")

    state = sess.state_root
    (state / "WALLS.md").write_text("# op standing\n## deny_path\n*.env\n")

    weapons_mod.set_equipped_weapon(state, "w-overlay")
    summary = weapons_mod.equip_policy(state, "w-overlay")
    assert "WALLS.md" in summary["overlaid"]
    walls = (state / "WALLS.md").read_text()
    # Operator's *.env preserved
    assert "*.env" in walls
    # Offsec overlay layered on top
    assert "BEGIN w-overlay" in walls
    assert "~/.ssh/**" in walls
