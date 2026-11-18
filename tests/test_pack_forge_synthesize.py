import json
from pathlib import Path

from ara.forge.pack_draft import PackDraft, build_pack_draft
from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind
from ara.forge import state as fstate
from ara.forge.phases.synthesize import synthesize_pack, load_pack_draft


def _write_extractions(state_root, forge_id, events):
    path = fstate.forge_extractions_path(state_root, forge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for i, ev in enumerate(events):
            rec = {"ts": 0.0, "manifest_element_id": "m1", "channel": ev["channel"],
                   "body": ev["body"], "extraction_id": f"e{i}", "citation": {}}
            f.write(json.dumps(rec) + "\n")


def test_synthesize_pack_writes_draft_with_baselines(tmp_path: Path):
    s = ForgeSession.new(tmp_path, weapon_target_id="tinydom", operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    _write_extractions(tmp_path, s.forge_id, [
        {"channel": "propose.alphabet",
         "body": {"name": "category", "shape": "categorical",
                  "fields": [{"name": "id", "type": "str"}]}},
        {"channel": "extract.conc",
         "body": {"alphabet_name": "category", "entry": {"id": "sqli"}}},
        {"channel": "extract.conc",
         "body": {"alphabet_name": "category", "entry": {"id": "xss"}}},
        {"channel": "propose.binding",
         "body": {"field": "category", "rule": "FIELD_IN_ALPHABET", "alphabet": "category"}},
    ])
    draft = synthesize_pack(s)
    # persisted + reloadable
    reloaded = load_pack_draft(tmp_path, s.forge_id)
    assert reloaded is not None
    pack = reloaded.to_pack()
    assert pack.name == "tinydom"
    assert pack.get("category") is not None
    # no chunks proposed -> offsec baseline seeded (source=baseline)
    assert draft.chunks and draft.chunks[0]["source"] == "baseline"
    # no policy proposed -> hermes-default baseline seeded
    assert draft.policy and draft.policy[0]["source"] == "baseline"


def test_pack_draft_assembles_and_materializes():
    d = PackDraft(name="mydom")
    d.add_alphabet({"name": "category", "shape": "categorical",
                    "fields": [{"name": "id", "type": "str"}]})
    d.add_entry("category", {"id": "x.y"})
    d.add_binding({"field": "category", "rule": "FIELD_IN_ALPHABET", "alphabet": "category"})
    pack = d.to_pack()
    assert pack.get("category") is not None
    assert [b.alphabet for b in pack.validator_bindings] == ["category"]
    # the entry became a starter on the alphabet
    cat = pack.get("category")
    assert any(e.get("id") == "x.y" for e in cat.starter)


def test_dropped_items_excluded():
    d = PackDraft(name="m")
    d.add_alphabet({"name": "a", "shape": "categorical",
                    "fields": [{"name": "id", "type": "str"}]})
    d.items_for("alphabets")[0]["keep"] = False
    assert d.to_pack().alphabets == []


def test_build_from_extractions_buckets_by_channel():
    events = [
        {"channel": "propose.alphabet",
         "body": {"name": "category", "shape": "categorical",
                  "fields": [{"name": "id", "type": "str"}]}},
        {"channel": "extract.conc",
         "body": {"alphabet_name": "category", "entry": {"id": "sqli"}}},
        {"channel": "extract.conc",
         "body": {"alphabet_name": "category", "entry": {"id": "xss"}}},
        {"channel": "propose.binding",
         "body": {"field": "category", "rule": "FIELD_IN_ALPHABET", "alphabet": "category"}},
        {"channel": "propose.detection", "body": {"keyword": "finding"}},
        {"channel": "extract.bulwark_rule",
         "body": {"action": "deny", "layer": "exec", "match": "rm"}},
    ]
    d = build_pack_draft(events, name="mydom")
    pack = d.to_pack()
    assert pack.get("category") is not None
    assert any(e["id"] == "sqli" for e in pack.get("category").starter)
    assert pack.validator_bindings[0].field == "category"
    assert "finding" in pack.manifest_detection.report_section_keywords
    assert "bulwark" in pack.policy_defaults


def test_roundtrip_jsonable():
    d = PackDraft(name="m")
    d.add_alphabet({"name": "a", "shape": "categorical", "fields": [{"name": "id", "type": "str"}]})
    d2 = PackDraft.from_jsonable(d.to_jsonable())
    assert d2.name == "m"
    assert d2.to_pack().get("a") is not None
