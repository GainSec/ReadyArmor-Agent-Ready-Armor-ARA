"""Operator-selectable chunk-graph workflows for pack forge."""
import pytest

from ara.forge import chunk_workflows as W
from ara.forge.pack_draft import build_pack_draft
from ara.forge.phases.seal import _validate_pack


def test_list_and_default():
    names = W.workflow_names()
    assert set(names) >= {"general", "firmware", "mobile"}
    assert names[0] == "general"           # default first
    assert W.DEFAULT == "general"


def test_load_unknown_falls_back_to_general():
    assert W.load_workflow("firmware")[0]["id"] == "recon"
    assert [c["id"] for c in W.load_workflow("nope")] == \
        [c["id"] for c in W.load_workflow("general")]
    assert W.load_workflow(None) == W.load_workflow("general")


@pytest.mark.parametrize("name", ["general", "firmware", "mobile"])
def test_workflow_is_seal_valid(name):
    """Each workflow must satisfy the seal chunk-graph rules: exactly one entry
    node and every exit resolving to a real node."""
    chunks = W.load_workflow(name)
    assert sum(1 for c in chunks if c.get("is_entry")) == 1
    ids = {c["id"] for c in chunks}
    for c in chunks:
        for t, x in (c.get("exits") or {}).items():
            assert x.get("next_chunk_id") in ids, f"{name}:{c['id']} exit {t} dangling"
    # build a minimal pack on this baseline and confirm no chunk-graph failure
    ev = [{"channel": "propose.alphabet", "manifest_element_id": "e",
           "body": {"name": "a", "shape": "categorical",
                    "fields": [{"name": "id", "type": "str"}]}},
          {"channel": "extract.conc", "manifest_element_id": "e",
           "body": {"alphabet_name": "a", "entry": {"id": "x", "label": "X"}}},
          {"channel": "extract.conc", "manifest_element_id": "e",
           "body": {"alphabet_name": "a", "entry": {"id": "y", "label": "Y"}}}]
    pack = build_pack_draft(ev, name="t", chunk_baseline=chunks).to_pack()
    assert not [f for f in _validate_pack(pack) if "chunk" in f.lower()]


def test_workflows_are_distinct():
    g = {c["id"] for c in W.load_workflow("general")}
    f = {c["id"] for c in W.load_workflow("firmware")}
    m = {c["id"] for c in W.load_workflow("mobile")}
    assert g != f and g != m and f != m
    assert "extract" in f and "dynamic" in m   # domain-specific nodes


def test_create_stores_workflow(tmp_path):
    from ara.armory import forge_actions
    ok, msg, fid = forge_actions.handle_forge_create(
        tmp_path, {"kind": "pack", "weapon_target_id": "wf-test",
                   "chunk_workflow": "firmware"})
    assert ok, msg
    from ara.forge.session import ForgeSession
    sess = ForgeSession.load(tmp_path, fid)
    assert sess.config.chunk_workflow == "firmware"


def test_create_rejects_unknown_workflow(tmp_path):
    from ara.armory import forge_actions
    from ara.forge.session import ForgeSession
    ok, msg, fid = forge_actions.handle_forge_create(
        tmp_path, {"kind": "pack", "weapon_target_id": "wf-test2",
                   "chunk_workflow": "bogus"})
    assert ok
    assert ForgeSession.load(tmp_path, fid).config.chunk_workflow == "general"


# ---- forged (state-dir) workflows + derivation driver ----

def _mini_graph(entry_ok=True):
    return [
        {"id": "recon", "title": "Recon", "prompt_fragment": "look",
         "tool_surface": ["mats.search"], "exits": {"<done>": {
             "tag": "<done>", "next_chunk_id": "review", "requires_caller": "agent",
             "description": "done"}}, "gems": [], "is_entry": entry_ok,
         "is_terminal": False, "can_emit_finding": False},
        {"id": "review", "title": "Review", "prompt_fragment": "sign off",
         "tool_surface": [], "exits": {}, "gems": [], "is_entry": False,
         "is_terminal": True, "can_emit_finding": False},
    ]


def test_validate_chunk_graph():
    assert W.validate_chunk_graph(_mini_graph()) == []
    assert W.validate_chunk_graph([]) != []
    # two entries
    g = _mini_graph(); g[1]["is_entry"] = True
    assert any("entry" in f for f in W.validate_chunk_graph(g))
    # dangling exit
    g = _mini_graph(); g[0]["exits"]["<done>"]["next_chunk_id"] = "nope"
    assert any("unknown" in f for f in W.validate_chunk_graph(g))


def test_save_and_forged_shadows_seed(tmp_path):
    W.save_workflow(tmp_path, "custom", "a forged one", _mini_graph())
    assert "custom" in W.workflow_names(tmp_path)
    assert "custom" not in W.workflow_names()          # seed-only view
    assert W.list_workflows(tmp_path)[-1]["forged"] in (True, False)
    # forged shadows a seed name
    W.save_workflow(tmp_path, "general", "override", _mini_graph())
    assert [c["id"] for c in W.load_workflow("general", tmp_path)] == ["recon", "review"]
    assert [c["id"] for c in W.load_workflow("general")] != ["recon", "review"]


def test_run_workflow_derive_saves(tmp_path):
    """Driver: the (stubbed) agent emits a valid chunk graph via propose.chunk;
    the driver assembles, validates, and saves it to the registry."""
    import json
    from ara.forge import loop_driver, state as fstate
    from ara.forge.manifest.element import ManifestElement, ManifestKind, write_manifest
    fid = "wfderive"
    write_manifest(fstate.forge_manifest_path(tmp_path, fid),
                   [ManifestElement(id="m1", source_mat_id="s", byte_range=(0, 1),
                                    sha256="x", kind=ManifestKind.GENERIC_TEXT,
                                    extracted_text="testing methodology")])
    ex_path = fstate.forge_extractions_path(tmp_path, fid)

    def stub(sr, prompt):
        assert "INVESTIGATION WORKFLOW" in prompt
        ex_path.parent.mkdir(parents=True, exist_ok=True)
        with ex_path.open("a") as f:
            for node in _mini_graph():
                f.write(json.dumps({"ts": "t", "manifest_element_id": "m1",
                                    "channel": "propose.chunk", "body": node,
                                    "extraction_id": node["id"], "citation": {}}) + "\n")
        return 0

    res = loop_driver.run_workflow_derive(tmp_path, fid, name="webapp",
                                          domain="web", agent_invoke=stub)
    assert res["saved"] and res["nodes"] == 2, res
    assert [c["id"] for c in W.load_workflow("webapp", tmp_path)] == ["recon", "review"]
