from __future__ import annotations

import json
from pathlib import Path

from ara import armors as armors_mod
from ara import temper
from ara import weapons as weapons_mod
from ara.armory import forge_actions
from ara.forge import state as fstate
from ara.forge.manifest.element import read_manifest
from ara.forge.phases import exhaust
from ara.forge.session import ForgePhase, ForgeSession


def _cover_first_manifest_element(state: Path, fid: str) -> None:
    sess = ForgeSession.load(state, fid)
    elements = read_manifest(fstate.forge_manifest_path(state, fid))
    assert elements
    exhaust.record_extraction(
        sess,
        manifest_element_id=elements[0].id,
        channel="extract.proc",
        body={"name": "Test procedure", "steps": ["verify"]},
        extraction_id="x_test_proc",
    )


def test_create_forge_session_equips_blacksmith(tmp_path: Path) -> None:
    ok, msg, fid = forge_actions.handle_forge_create(
        tmp_path,
        {
            "weapon_target_id": "web-weapon",
            "operator_id": "tester",
            "allow_starter_alphabets": "on",
        },
    )

    assert ok
    assert fid
    assert "Created forge" in msg
    sess = ForgeSession.load(tmp_path, fid)
    assert sess.weapon_target_id == "web-weapon"
    assert sess.operator_id == "tester"
    assert sess.config.allow_starter_alphabets is True
    assert weapons_mod.equipped_weapon_id(tmp_path) == "blacksmith-hammer"


def test_web_forge_flow_upload_manifest_extract_synthesize_seal(tmp_path: Path) -> None:
    ok, _msg, fid = forge_actions.handle_forge_create(
        tmp_path,
        {
            "weapon_target_id": "web-sqli",
            "operator_id": "tester",
            "allow_starter_alphabets": "on",
        },
    )
    assert ok

    ok, msg = forge_actions.handle_forge_upload_mat(
        tmp_path,
        fid,
        {
            "file": (
                b"Finding: SQL injection\n"
                b"Severity: High\n"
                b"Impact: credentials can be read.\n"
                b"Fix: use parameterized queries.\n"
            ),
            "file_filename": "finding.md",
        },
    )
    assert ok, msg
    assert "Uploaded finding.md" in msg

    for action in (
        forge_actions.handle_forge_finalize_deposit,
        forge_actions.handle_forge_run_manifest,
        forge_actions.handle_forge_approve_manifest,
        forge_actions.handle_forge_auto_extract,
    ):
        ok, msg = action(tmp_path, fid)
        assert ok, msg
    _cover_first_manifest_element(tmp_path, fid)
    ok, msg = forge_actions.handle_forge_advance_review(
        tmp_path, fid, {},
    )
    assert ok, msg
    ok, msg = forge_actions.handle_forge_synthesize(tmp_path, fid)
    assert ok, msg

    ok, msg = forge_actions.handle_forge_seal(
        tmp_path,
        fid,
        {"weapon_id": "web-sqli"},
    )

    assert ok, msg
    assert "Sealed web-sqli" in msg
    assert (tmp_path / "weapons" / "web-sqli" / "seal.json").is_file()
    assert ForgeSession.load(tmp_path, fid).phase == ForgePhase.SEALED
    assert weapons_mod.equipped_weapon_id(tmp_path) is None


def test_web_forge_seal_creates_pending_temper_for_prior_armor(
    tmp_path: Path,
) -> None:
    armors_mod.create_armor(tmp_path, "hermes-default")
    armors_mod.equip_armor(tmp_path, "hermes-default")
    ok, _msg, fid = forge_actions.handle_forge_create(
        tmp_path,
        {
            "weapon_target_id": "web-sqli",
            "operator_id": "tester",
            "allow_starter_alphabets": "on",
        },
    )
    assert ok

    ok, msg = forge_actions.handle_forge_upload_mat(
        tmp_path,
        fid,
        {
            "file": (
                b"Finding: SQL injection\n"
                b"Severity: High\n"
                b"Impact: credentials can be read.\n"
                b"Fix: use parameterized queries.\n"
            ),
            "file_filename": "finding.md",
        },
    )
    assert ok, msg
    for action in (
        forge_actions.handle_forge_finalize_deposit,
        forge_actions.handle_forge_run_manifest,
        forge_actions.handle_forge_approve_manifest,
        forge_actions.handle_forge_auto_extract,
    ):
        ok, msg = action(tmp_path, fid)
        assert ok, msg
    _cover_first_manifest_element(tmp_path, fid)
    ok, msg = forge_actions.handle_forge_advance_review(
        tmp_path, fid, {},
    )
    assert ok, msg
    ok, msg = forge_actions.handle_forge_synthesize(tmp_path, fid)
    assert ok, msg

    ok, msg = forge_actions.handle_forge_seal(
        tmp_path,
        fid,
        {"weapon_id": "web-sqli"},
    )

    assert ok, msg
    proposals = temper.list_proposals(tmp_path, "hermes-default")
    assert len(proposals) == 1
    assert proposals[0].status == "pending"
    assert proposals[0].source_weapon == "web-sqli"
    assert proposals[0].source_forge == fid


def test_web_forge_seal_rejects_force_flag(tmp_path: Path) -> None:
    ok, _msg, fid = forge_actions.handle_forge_create(
        tmp_path,
        {
            "weapon_target_id": "web-sqli",
            "operator_id": "tester",
            "allow_starter_alphabets": "on",
        },
    )
    assert ok

    ok, msg = forge_actions.handle_forge_seal(
        tmp_path,
        fid,
        {"weapon_id": "web-sqli", "force": "on"},
    )

    assert not ok
    assert "force seal" in msg.lower()


def test_upload_mat_rejects_wrong_phase(tmp_path: Path) -> None:
    ok, _msg, fid = forge_actions.handle_forge_create(
        tmp_path,
        {"weapon_target_id": "web-weapon", "operator_id": "tester"},
    )
    assert ok
    ok, msg = forge_actions.handle_forge_finalize_deposit(tmp_path, fid)
    assert not ok
    assert "no mats" in msg.lower()


def test_forge_detail_renders_action_forms(tmp_path: Path) -> None:
    ok, _msg, fid = forge_actions.handle_forge_create(
        tmp_path,
        {"weapon_target_id": "web-weapon", "operator_id": "tester"},
    )
    assert ok

    from ara.armory.forge_v1_views import render_forge, render_forge_detail

    index = render_forge(tmp_path, "tok")
    assert 'action="/forge/create' in index
    # shared create form serves weapon + pack kinds (JS relabels the primary
    # field; "Weapon target" is the static default for the weapon kind)
    assert "Weapon target" in index
    assert 'name="kind"' in index
    # alphabet pack is a dropdown built from the pack list
    assert '<select name="alphabet_pack">' in index

    detail = render_forge_detail(tmp_path, fid, "tok")
    assert f'action="/forge/{fid}/mats/upload' in detail
    assert f'action="/forge/{fid}/deposit/finalize' in detail
    assert "Upload mat" in detail


def test_forge_detail_renders_hermes_exhaust_control(tmp_path: Path) -> None:
    ok, _msg, fid = forge_actions.handle_forge_create(
        tmp_path,
        {"weapon_target_id": "web-weapon", "operator_id": "tester"},
    )
    assert ok
    ok, msg = forge_actions.handle_forge_upload_mat(
        tmp_path,
        fid,
        {"file": b"# notes\nplain text", "file_filename": "notes.md"},
    )
    assert ok, msg
    for action in (
        forge_actions.handle_forge_finalize_deposit,
        forge_actions.handle_forge_run_manifest,
        forge_actions.handle_forge_approve_manifest,
    ):
        ok, msg = action(tmp_path, fid)
        assert ok, msg

    from ara.armory.forge_v1_views import render_forge_detail

    detail = render_forge_detail(tmp_path, fid, "tok")
    assert f'action="/forge/{fid}/exhaust/run' in detail
    assert "Run Hermes exhaust" in detail
    assert "Max iterations" in detail
