"""Pack-forge stateless exhaust: the agent proposes the pack vocabulary
(alphabets / entries / bindings) via the propose.* channels, one grounded
model call per manifest element, with the declared-alphabet inventory threaded
through so entries accrete onto existing alphabets. Model is injected."""

from __future__ import annotations

import json
from pathlib import Path

from ara.forge import loop_driver
from ara.forge.phases import deposit, exhaust, manifest, synthesize
from ara.forge.session import ForgeConfig, ForgeKind, ForgePhase, ForgeSession
from ara.armory import forge_actions


def _pack_forge_to_exhaust(tmp_path: Path):
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(
        state_root, weapon_target_id="webapp", operator_id="op",
        config=ForgeConfig(kind=ForgeKind.PACK.value))
    md = sources / "wstg.md"
    md.write_text(
        "# Web Security Testing\n\n"
        "## Authentication Testing\n"
        "Test for default credentials and weak lockout on /login.\n\n"
        "## Injection Testing\n"
        "Probe for SQL injection and command injection in input fields.\n\n"
        "## Session Management\n"
        "Check session token entropy and fixation.\n")
    deposit.deposit_mat(sess, md)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    assert sess.phase == ForgePhase.EXHAUST
    return sess, elements


def test_pack_exhaust_records_proposals_on_propose_channels(tmp_path: Path):
    sess, elements = _pack_forge_to_exhaust(tmp_path)

    def fake(system: str, user: str) -> str:
        return json.dumps([
            {"kind": "alphabet", "body": {
                "name": "test_category", "shape": "categorical",
                "fields": [{"name": "id", "type": "str"},
                           {"name": "label", "type": "str"}]}},
            {"kind": "entry", "body": {
                "alphabet_name": "test_category",
                "entry": {"id": "authn", "label": "Authentication"}}},
            {"kind": "binding", "body": {
                "field": "category", "rule": "FIELD_IN_ALPHABET",
                "alphabet": "test_category"}},
        ])

    res = loop_driver.run_pack_exhaust_stateless(
        sess.state_root, sess.forge_id, model_invoke=fake)

    assert res.elements_examined == len(elements)
    exts = exhaust.read_extractions(sess.state_root, sess.forge_id)
    channels = {x.channel for x in exts}
    assert "propose.alphabet" in channels
    assert "extract.conc" in channels      # entries ride extract.conc
    assert "propose.binding" in channels
    # every proposal is grounded to a real manifest element
    valid = {e.id for e in elements}
    assert all(x.manifest_element_id in valid for x in exts)


def test_pack_exhaust_threads_declared_alphabets_into_prompt(tmp_path: Path):
    sess, elements = _pack_forge_to_exhaust(tmp_path)
    assert len(elements) >= 2
    seen_inventories: list[str] = []

    def fake(system: str, user: str) -> str:
        # capture the "Alphabets so far:" line the runner threads in
        line = next((ln for ln in user.splitlines()
                     if ln.startswith("Alphabets so far:")), "")
        seen_inventories.append(line)
        return json.dumps([{"kind": "alphabet", "body": {
            "name": "vuln_class", "shape": "categorical",
            "fields": [{"name": "id", "type": "str"}]}}])

    loop_driver.run_pack_exhaust_stateless(
        sess.state_root, sess.forge_id, model_invoke=fake)

    # first element sees an empty inventory; a later one sees the declared name
    assert seen_inventories[0] == "Alphabets so far: (none yet)"
    assert any("vuln_class" in inv for inv in seen_inventories[1:])


def test_pack_exhaust_then_synthesize_builds_useful_pack(tmp_path: Path):
    sess, _elements = _pack_forge_to_exhaust(tmp_path)

    def fake(system: str, user: str) -> str:
        return json.dumps([
            {"kind": "alphabet", "body": {
                "name": "test_category", "shape": "categorical",
                "fields": [{"name": "id", "type": "str"},
                           {"name": "label", "type": "str"}]}},
            {"kind": "entry", "body": {
                "alphabet_name": "test_category",
                "entry": {"id": "injection", "label": "Injection"}}},
        ])

    loop_driver.run_pack_exhaust_stateless(
        sess.state_root, sess.forge_id, model_invoke=fake)

    draft = synthesize.synthesize_pack(sess)
    names = [a["data"]["name"] for a in draft.items_for("alphabets")]
    assert "test_category" in names
    entries = [e for e in draft.items_for("entries")]
    assert any(e["data"]["entry"]["id"] == "injection" for e in entries)


def test_gui_handler_routes_pack_session_to_contained_agent(tmp_path: Path, monkeypatch):
    # The GUI pack exhaust routes to the CONTAINED agent (run_pack_exhaust),
    # NOT the stateless path. Stub the agent turn so it runs offline.
    sess, _elements = _pack_forge_to_exhaust(tmp_path)

    from ara.forge import state as fstate
    from ara.forge.phases import manifest as _manifest
    from ara.forge.phases.exhaust import record_extraction
    from ara.forge.session import ForgeSession as _FS

    def fake_agent(state_root, prompt):
        s = _FS.load(state_root, sess.forge_id)
        mid = _manifest.read_manifest(
            fstate.forge_manifest_path(state_root, sess.forge_id))[0].id
        record_extraction(s, manifest_element_id=mid, channel="propose.alphabet",
                          body={"name": "iface", "shape": "categorical",
                                "fields": [{"name": "id", "type": "str"}]},
                          extraction_id="ag1")
        return 0

    # stateless must NOT be the path taken
    def no_stateless(*a, **k):
        raise AssertionError("stateless path taken for a pack session")
    monkeypatch.setattr(loop_driver, "_default_agent_invoke",
                        lambda sr, pr, **kw: fake_agent(sr, pr))
    monkeypatch.setattr(loop_driver, "run_pack_exhaust_stateless", no_stateless)

    ok, msg = forge_actions.handle_forge_run_exhaust(
        sess.state_root, sess.forge_id, {"max_iterations": 1, "target_coverage": 0.01})
    assert ok, msg
    assert "Pack exhaust" in msg
    channels = {x.channel for x in
                exhaust.read_extractions(sess.state_root, sess.forge_id)}
    assert "propose.alphabet" in channels
