import json
from pathlib import Path

from ara.forge import loop_driver
from ara.forge import state as fstate
from ara.forge.phases import deposit, manifest
from ara.forge.phases.exhaust import record_extraction
from ara.forge.session import ForgeConfig, ForgeKind, ForgeSession


def _pack_at_exhaust(tmp_path):
    state_root = tmp_path / "state"
    (tmp_path / "src").mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="p", operator_id="op",
                            config=ForgeConfig(kind=ForgeKind.PACK.value))
    md = tmp_path / "src" / "r.md"
    md.write_text("# T\n\n## A\nauth default creds\n\n## B\nsqli injection\n")
    deposit.deposit_mat(sess, md)
    deposit.finalize_deposit(sess)
    manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    return sess


def test_pack_state_primer_is_compact_and_lists_alphabets(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)
    mid = manifest.read_manifest(
        fstate.forge_manifest_path(sess.state_root, sess.forge_id))[0].id
    record_extraction(sess, manifest_element_id=mid, channel="propose.alphabet",
                      body={"name": "auth_weakness", "shape": "categorical",
                            "description": "authentication weaknesses",
                            "fields": [{"name": "id", "type": "str"}]},
                      extraction_id="a1")
    for i, eid in enumerate(("default_creds", "weak_lockout")):
        record_extraction(sess, manifest_element_id=mid, channel="extract.conc",
                          body={"alphabet_name": "auth_weakness",
                                "entry": {"id": eid, "label": eid}},
                          extraction_id=f"e{i}")

    out = loop_driver._pack_state_primer(sess.state_root, sess.forge_id)
    text = out["pack_state"]
    # names the alphabet, its shape, description, and entry count
    assert "auth_weakness" in text
    assert "categorical" in text
    assert "2" in text                       # entry count
    # COMPACT: does not leak the individual entry ids
    assert "default_creds" not in text
    assert "weak_lockout" not in text


def test_pack_state_primer_empty_first_turn(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)
    out = loop_driver._pack_state_primer(sess.state_root, sess.forge_id)
    assert "none yet" in out["pack_state"].lower()


def test_run_pack_exhaust_primes_prior_alphabets_and_all_shapes(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)
    mid = manifest.read_manifest(
        fstate.forge_manifest_path(sess.state_root, sess.forge_id))[0].id
    record_extraction(sess, manifest_element_id=mid, channel="propose.alphabet",
                      body={"name": "auth_weakness", "shape": "categorical",
                            "description": "auth"}, extraction_id="a1")
    seen = []

    def fake(sr, pr):
        seen.append(pr)
        return 0

    loop_driver.run_pack_exhaust(sess.state_root, sess.forge_id,
                                 agent_invoke=fake, target_coverage=1.0,
                                 max_iterations=1)
    p = seen[0]
    assert "auth_weakness" in p                       # prior alphabet primed
    # all four shapes are taught
    for shape in ("categorical", "matrix", "template_set", "vector"):
        assert shape in p


def test_weapon_prompt_path_unchanged(tmp_path: Path):
    # run_exhaust with no prompt_context_fn renders the weapon template fine
    # (no {pack_state} required) — packs are additive, weapons untouched.
    from ara.forge.loop_driver import _DEFAULT_BATCH_PROMPT_TEMPLATE, _build_prompt
    from ara.forge.phases.exhaust import CoverageState
    cov = CoverageState(total_elements=1, elements_with_extractions=0,
                        pending=["e1"], fraction=0.0)
    out = _build_prompt("f1", 1, cov, [], min_extractions=1,
                        template=_DEFAULT_BATCH_PROMPT_TEMPLATE, extra_fields=None)
    assert "blacksmith-hammer" in out


def test_exhaust_advances_past_unextractable_head_element(tmp_path: Path):
    # 3 elements; the agent extracts NOTHING for whichever is the head of
    # pending. Old behavior retried the head forever (no-progress bail);
    # element-advance must attempt all three exactly once.
    state_root = tmp_path / "state"
    (tmp_path / "src").mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="p", operator_id="op",
                            config=ForgeConfig(kind=ForgeKind.PACK.value))
    md = tmp_path / "src" / "r.md"
    md.write_text("# T\n\n## A\naaa\n\n## B\nbbb\n\n## C\nccc\n")
    deposit.deposit_mat(sess, md)
    deposit.finalize_deposit(sess)
    els = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    ids = [e.id for e in els]
    assert len(ids) >= 2

    seen_batches = []

    def fake(state_root, prompt):
        here = [i for i in ids if i in prompt]
        seen_batches.append(tuple(here))
        return 0   # extract NOTHING for any element

    loop_driver.run_exhaust(
        sess.state_root, sess.forge_id, batch_size=1,
        max_iterations=len(ids) + 5,
        target_coverage=1.0, no_progress_bail=3, agent_invoke=fake)

    attempted = [i for b in seen_batches for i in b]
    # every element attempted exactly once, then the run stops (no re-loop on
    # the un-extractable head)
    assert sorted(attempted) == sorted(ids)


def test_policy_step_present_by_default_and_skippable(tmp_path: Path):
    from ara.forge.session import ForgeConfig, ForgeKind, ForgeSession
    # default: forge_policy True -> policy step in the primer
    s = ForgeSession.new(tmp_path, weapon_target_id="p", operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    out = loop_driver._pack_state_primer(s.state_root, s.forge_id)
    assert "extract.bulwark_rule" in out["policy_step"]
    assert "extract.rhema_rule" in out["policy_step"]
    # skip: forge_policy False -> empty policy step
    s2 = ForgeSession.new(tmp_path / "b", weapon_target_id="p2", operator_id="op",
                          config=ForgeConfig(kind=ForgeKind.PACK.value,
                                             forge_policy=False))
    out2 = loop_driver._pack_state_primer(s2.state_root, s2.forge_id)
    assert out2["policy_step"] == ""


def test_create_skip_policy_flag(tmp_path: Path):
    from ara.armory.forge_actions import handle_forge_create
    from ara.forge.session import ForgeSession
    ok, _m, fid = handle_forge_create(tmp_path, {"weapon_target_id": "d",
                                                 "kind": "pack",
                                                 "skip_policy": "on"})
    assert ok
    assert ForgeSession.load(tmp_path, fid).config.forge_policy is False
    ok2, _m2, fid2 = handle_forge_create(tmp_path, {"weapon_target_id": "d2",
                                                    "kind": "pack"})
    assert ForgeSession.load(tmp_path, fid2).config.forge_policy is True


def _write_status(state_root, forge_id, complete: bool):
    import json
    p = fstate.forge_turn_status_path(state_root, forge_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"batch_complete": complete}))


def test_recycle_cycles_until_agent_says_done(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)   # 2 elements
    mid = manifest.read_manifest(
        fstate.forge_manifest_path(sess.state_root, sess.forge_id))[0].id
    calls = {"n": 0}

    def fake(state_root, prompt):
        if mid not in prompt:
            _write_status(state_root, sess.forge_id, True)  # other element: done
            return 0
        calls["n"] += 1
        # add a NEW proposal each of the first 2 cycles, then declare done
        s = ForgeSession.load(state_root, sess.forge_id)
        record_extraction(s, manifest_element_id=mid, channel="propose.alphabet",
                          body={"name": f"a{calls['n']}", "shape": "categorical",
                                "fields": [{"name": "id", "type": "str"}]},
                          extraction_id=f"x{calls['n']}")
        _write_status(state_root, sess.forge_id, calls["n"] >= 3)  # more, more, done
        return 0

    loop_driver.run_pack_exhaust(sess.state_root, sess.forge_id, agent_invoke=fake,
                                 batch_size=1, max_iterations=12, target_coverage=1.0)
    # the mid element got cycled 3 times (agent kept saying "more") then advanced
    assert calls["n"] == 3


def test_recycle_backstop_advances_on_no_new_progress(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)
    seen = {"n": 0}

    def fake(state_root, prompt):
        seen["n"] += 1
        _write_status(state_root, sess.forge_id, False)  # always claims "more"...
        return 0                                         # ...but adds NOTHING

    res = loop_driver.run_pack_exhaust(sess.state_root, sess.forge_id,
                                       agent_invoke=fake, batch_size=1,
                                       max_iterations=20, target_coverage=1.0)
    # no-new-progress backstop advances instead of looping forever
    assert res.stopped_reason == "all_elements_attempted"
