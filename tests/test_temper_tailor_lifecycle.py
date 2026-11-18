from __future__ import annotations

import json
from pathlib import Path

from ara import armors as armors_mod
from ara import bulwark as bulwark_mod
from ara import weapons as weapons_mod
from ara.seed import ensure_seed


def _seed_prior_armor(state: Path, name: str) -> None:
    armors_mod.create_armor(state, name, description=f"{name} armor")
    armors_mod.set_active_armor(state, name)


def _seed_prior_weapon(state: Path, weapon_id: str) -> None:
    pdir = state / "weapons" / weapon_id / "policy"
    pdir.mkdir(parents=True, exist_ok=True)
    (state / "weapons" / weapon_id / "seal.json").write_text(json.dumps({
        "id": weapon_id,
        "weapon_target_id": weapon_id,
    }))
    (pdir / "bulwark.md").write_text(
        "## global\nallow url https://prior.example/**\n"
    )
    weapons_mod.set_equipped_weapon(state, weapon_id)
    weapons_mod.equip_policy(state, weapon_id)


def test_equip_tailor_materializes_seeds_and_captures_prior_loadout(
    tmp_path: Path,
) -> None:
    from ara import temper_lifecycle as tl

    _seed_prior_armor(tmp_path, "hermes-default")
    _seed_prior_weapon(tmp_path, "prior-tool")

    report = tl.equip_tailor(tmp_path)

    assert report["armor"] == "tailor-robes"
    assert report["weapon"] == "needle"
    assert report["prior_armor"] == "hermes-default"
    assert report["prior_weapon"] == "prior-tool"
    assert "tailor-robes" in report["seeded_armors"]
    assert "needle" in report["seeded_weapons"]
    assert "bulwark.md" in report["policy_overlaid"]

    assert armors_mod.active_armor_name(tmp_path) == "tailor-robes"
    assert weapons_mod.equipped_weapon_id(tmp_path) == "needle"

    text = (tmp_path / "bulwark.md").read_text()
    assert "# ARA-ARMOR-SCOPE-BEGIN tailor-robes" in text
    assert "# ARA-WEAPON-OVERLAY-BEGIN needle" in text
    assert "# ARA-WEAPON-OVERLAY-BEGIN prior-tool" not in text

    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()


def test_restore_tailor_strips_needle_overlay_and_restores_prior_loadout(
    tmp_path: Path,
) -> None:
    from ara import temper_lifecycle as tl

    _seed_prior_armor(tmp_path, "hermes-default")
    _seed_prior_weapon(tmp_path, "prior-tool")
    report = tl.equip_tailor(tmp_path)

    restored = tl.restore_tailor(
        tmp_path,
        prior_armor=report["prior_armor"],
        prior_weapon=report["prior_weapon"],
    )

    assert restored == {
        "restored_armor": "hermes-default",
        "restored_weapon": "prior-tool",
    }
    assert armors_mod.active_armor_name(tmp_path) == "hermes-default"
    assert weapons_mod.equipped_weapon_id(tmp_path) == "prior-tool"

    text = (tmp_path / "bulwark.md").read_text()
    assert "# ARA-WEAPON-OVERLAY-BEGIN needle" not in text
    assert "# ARA-WEAPON-OVERLAY-BEGIN prior-tool" in text
    assert "# ARA-ARMOR-SCOPE-BEGIN tailor-robes" in text

    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()


def test_equip_tailor_does_not_capture_itself_as_prior(tmp_path: Path) -> None:
    from ara import temper_lifecycle as tl

    ensure_seed(tmp_path, armors=("tailor-robes",), weapons=("needle",))
    armors_mod.set_active_armor(tmp_path, "tailor-robes")
    weapons_mod.set_equipped_weapon(tmp_path, "needle")
    weapons_mod.equip_policy(tmp_path, "needle")

    report = tl.equip_tailor(tmp_path)

    assert report["prior_armor"] is None
    assert report["prior_weapon"] is None

    restored = tl.restore_tailor(
        tmp_path,
        prior_armor=report["prior_armor"],
        prior_weapon=report["prior_weapon"],
    )
    assert restored == {"restored_armor": None, "restored_weapon": None}
    assert armors_mod.active_armor_name(tmp_path) is None
    assert weapons_mod.equipped_weapon_id(tmp_path) is None
    assert "# ARA-WEAPON-OVERLAY-BEGIN needle" not in (
        tmp_path / "bulwark.md"
    ).read_text()


def test_start_tailor_session_persists_running_record(tmp_path: Path) -> None:
    from ara import temper_lifecycle as tl

    _seed_prior_armor(tmp_path, "hermes-default")
    _seed_prior_weapon(tmp_path, "prior-tool")

    rec = tl.start_tailor_session(
        tmp_path,
        source_request={
            "armor": "hermes-default",
            "source_weapon": "flocker-v03",
            "mode": "full_temper",
        },
        session_id="tailor-test-1",
    )

    assert rec["session_id"] == "tailor-test-1"
    assert rec["status"] == "running"
    assert rec["prior_armor"] == "hermes-default"
    assert rec["prior_weapon"] == "prior-tool"
    assert rec["source_request"]["source_weapon"] == "flocker-v03"
    assert rec["equipped"]["armor"] == "tailor-robes"
    assert rec["equipped"]["weapon"] == "needle"
    assert rec["created_at"] > 0
    assert rec["restored_at"] is None

    path = tl.tailor_session_path(tmp_path)
    assert path == tmp_path / "temper" / "tailor-session.json"
    assert path.is_file()
    loaded = tl.load_tailor_session(tmp_path)
    assert loaded == rec

    assert not (tmp_path / "armors" / "hermes-default" / "temper" / "proposals").exists()


def test_invalid_candidate_is_preserved_for_correction(tmp_path: Path) -> None:
    from ara import temper_lifecycle as tl

    tl.start_tailor_session(tmp_path, session_id="tailor-repair")

    try:
        tl.add_tailor_candidate(tmp_path, {
            "thread_type": "prompt_mediation",
            "destination": "rhema",
            "title": "Redact key material",
            "source": "mat",
            "rationale": "Keep raw key bytes out of replies.",
            "risk": "private key disclosure",
            "default_decision": "keep_for_review",
            "proposed_text": "Rewrite replies that include private keys.",
            "control": {
                "action": "rewrite",
                "layer": "reply",
                "pattern": "regex:PRIVATE KEY",
                "rationale": "Report presence, not raw values.",
            },
            "citations": [{"manifest_element_id": "elem_key"}],
        })
    except tl.TailorCandidateRejected as exc:
        rejection = exc.rejection
    else:
        raise AssertionError("candidate should have been rejected")

    assert rejection["session_id"] == "tailor-repair"
    assert rejection["destination"] == "rhema"
    assert "replacement" in rejection["error"]
    assert "correct and resubmit" in rejection["correction"]["instruction"]
    assert "control.replacement" in rejection["correction"]["rejected_fields"]
    assert "replacement" in rejection["correction"]["expected_control"]["notes"]

    loaded = tl.load_tailor_session(tmp_path)
    assert loaded is not None
    assert loaded["candidates"] == []
    assert loaded["rejected_candidates"] == [rejection]


def test_finish_tailor_session_restores_loadout_exactly_once(tmp_path: Path) -> None:
    from ara import temper_lifecycle as tl

    _seed_prior_armor(tmp_path, "hermes-default")
    _seed_prior_weapon(tmp_path, "prior-tool")
    rec = tl.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-test-2",
    )

    finished = tl.finish_tailor_session(tmp_path)

    assert finished["session_id"] == rec["session_id"]
    assert finished["status"] == "finished"
    assert finished["restored_at"] >= finished["created_at"]
    assert finished["restore_result"] == {
        "restored_armor": "hermes-default",
        "restored_weapon": "prior-tool",
    }
    assert armors_mod.active_armor_name(tmp_path) == "hermes-default"
    assert weapons_mod.equipped_weapon_id(tmp_path) == "prior-tool"
    assert "# ARA-WEAPON-OVERLAY-BEGIN needle" not in (
        tmp_path / "bulwark.md"
    ).read_text()

    _seed_prior_armor(tmp_path, "operator-new")
    _seed_prior_weapon(tmp_path, "operator-new-tool")
    second = tl.finish_tailor_session(tmp_path)

    assert second["status"] == "finished"
    assert second["restore_result"] == finished["restore_result"]
    assert armors_mod.active_armor_name(tmp_path) == "operator-new"
    assert weapons_mod.equipped_weapon_id(tmp_path) == "operator-new-tool"


def test_abort_tailor_session_marks_aborted_and_restores(tmp_path: Path) -> None:
    from ara import temper_lifecycle as tl

    _seed_prior_armor(tmp_path, "hermes-default")
    _seed_prior_weapon(tmp_path, "prior-tool")
    tl.start_tailor_session(tmp_path, session_id="tailor-test-3")

    aborted = tl.abort_tailor_session(tmp_path)

    assert aborted["status"] == "aborted"
    assert aborted["restore_result"] == {
        "restored_armor": "hermes-default",
        "restored_weapon": "prior-tool",
    }
    assert armors_mod.active_armor_name(tmp_path) == "hermes-default"
    assert weapons_mod.equipped_weapon_id(tmp_path) == "prior-tool"
