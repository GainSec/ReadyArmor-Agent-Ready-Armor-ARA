import json
from pathlib import Path

from ara.forge import loop_driver
from ara.forge import state as fstate
from ara.forge.phases import deposit, exhaust, manifest
from ara.forge.phases.exhaust import record_extraction
from ara.forge.session import ForgeConfig, ForgeKind, ForgeSession


def _pack_at_exhaust(tmp_path):
    state_root = tmp_path / "state"
    (tmp_path / "src").mkdir()
    sess = ForgeSession.new(state_root, weapon_target_id="p", operator_id="op",
                            config=ForgeConfig(kind=ForgeKind.PACK.value))
    md = tmp_path / "src" / "r.md"
    md.write_text("# T\n\n## A\nauth default creds and sqli\n")
    deposit.deposit_mat(sess, md)
    deposit.finalize_deposit(sess)
    manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    return sess


def test_stateless_sink_candidates_never_touches_draft(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)

    def fake_model(system, user, **kw):
        return json.dumps([{"kind": "alphabet", "body": {
            "name": "cat", "shape": "categorical",
            "fields": [{"name": "id", "type": "str"}]}}])

    loop_driver.run_pack_exhaust_stateless(
        sess.state_root, sess.forge_id, sink="candidates",
        model_invoke=fake_model)

    # candidates went to the pool, NOT extractions.jsonl (the draft)
    pool = fstate.forge_candidates_path(sess.state_root, sess.forge_id)
    assert pool.exists() and pool.read_text().strip()
    assert not exhaust.read_extractions(sess.state_root, sess.forge_id)


def test_review_keeps_only_what_agent_re_emits(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)
    mid = manifest.read_manifest(
        fstate.forge_manifest_path(sess.state_root, sess.forge_id))[0].id
    # seed a candidate pool with two candidates
    pool = fstate.forge_candidates_path(sess.state_root, sess.forge_id)
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text(
        json.dumps({"manifest_element_id": mid, "channel": "propose.alphabet",
                    "body": {"name": "keep_me", "shape": "categorical",
                             "fields": [{"name": "id", "type": "str"}]}}) + "\n" +
        json.dumps({"manifest_element_id": mid, "channel": "propose.alphabet",
                    "body": {"name": "junk", "shape": "categorical",
                             "fields": [{"name": "id", "type": "str"}]}}) + "\n")

    def fake_agent(state_root, prompt):
        # agent keeps only 'keep_me', skips 'junk'
        s = ForgeSession.load(state_root, sess.forge_id)
        record_extraction(s, manifest_element_id=mid, channel="propose.alphabet",
                          body={"name": "keep_me", "shape": "categorical",
                                "fields": [{"name": "id", "type": "str"}]},
                          extraction_id="k1")
        return 0

    res = loop_driver.run_pack_review(sess.state_root, sess.forge_id,
                                      agent_invoke=fake_agent)
    assert res == {"candidates": 2, "kept": 1}
    names = {x.body["name"] for x in
             exhaust.read_extractions(sess.state_root, sess.forge_id)
             if x.channel == "propose.alphabet"}
    assert names == {"keep_me"}          # junk was skipped, never entered draft


def test_review_no_pool_is_noop(tmp_path: Path):
    sess = _pack_at_exhaust(tmp_path)
    res = loop_driver.run_pack_review(sess.state_root, sess.forge_id,
                                      agent_invoke=lambda s, p: 0)
    assert res == {"candidates": 0, "kept": 0}
