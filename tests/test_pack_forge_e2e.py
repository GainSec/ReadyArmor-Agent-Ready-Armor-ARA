"""End-to-end pack-forge: a deterministic stub (directly-written
extraction events, no LLM) drives synthesize -> seal -> a valid pack.json,
then a weapon-forge session resolves the freshly-forged pack."""
import json
from pathlib import Path

from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind, ForgePhase
from ara.forge.phases import synthesize, seal as seal_mod
from ara.forge import state as fstate
from ara.forge.alphabets import pack as pack_mod


# What the `pen` agent would emit over its channels, as a fixed script.
_STUB_EVENTS = [
    {"channel": "propose.alphabet",
     "body": {"name": "category", "shape": "categorical",
              "fields": [{"name": "id", "type": "str"},
                         {"name": "label", "type": "str?"}]}},
    {"channel": "extract.conc",
     "body": {"alphabet_name": "category", "entry": {"id": "authz.bypass", "label": "AuthZ bypass"}}},
    {"channel": "extract.conc",
     "body": {"alphabet_name": "category", "entry": {"id": "info.leak", "label": "Info leak"}}},
    {"channel": "propose.binding",
     "body": {"field": "category", "rule": "FIELD_IN_ALPHABET", "alphabet": "category"}},
    {"channel": "propose.detection", "body": {"keyword": "weakness"}},
    {"channel": "extract.bulwark_rule",
     "body": {"action": "deny", "layer": "exec", "match": "rm"}},
]


def _write_stub_extractions(state_root, forge_id):
    path = fstate.forge_extractions_path(state_root, forge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for i, ev in enumerate(_STUB_EVENTS):
            f.write(json.dumps({"ts": 0.0, "manifest_element_id": "m1",
                                "channel": ev["channel"], "body": ev["body"],
                                "extraction_id": f"e{i}", "citation": {}}) + "\n")


def test_forge_a_pack_then_weapon_forge_resolves_it(tmp_path: Path):
    # ---- pack-forge: exhaust (stub) -> synthesize -> seal ----
    ps = ForgeSession.new(tmp_path, weapon_target_id="mydomain", operator_id="op",
                          config=ForgeConfig(kind=ForgeKind.PACK.value))
    _write_stub_extractions(tmp_path, ps.forge_id)

    draft = synthesize.synthesize_pack(ps)
    # vocabulary came through; chunk-graph + policy seeded from baselines
    assert any(a["data"]["name"] == "category" for a in draft.items_for("alphabets"))
    assert len(draft.items_for("entries")) == 2
    assert draft.items_for("chunks") and draft.items_for("chunks")[0]["source"] == "baseline"
    assert draft.items_for("policy")  # bulwark proposed + rhema/walls baseline

    ps.phase = ForgePhase.SEAL
    res = seal_mod.seal_pack(ps)
    assert res.ok, res.failures

    # ---- the sealed pack is valid + resolvable ----
    out = pack_mod.runtime_packs_dir(tmp_path) / "mydomain.json"
    assert out.exists()
    forged = pack_mod.load_pack(out)
    assert forged.name == "mydomain"
    cat = forged.get("category")
    assert {e["id"] for e in cat.starter} == {"authz.bypass", "info.leak"}
    assert forged.validator_bindings[0].alphabet == "category"
    assert "weakness" in forged.manifest_detection.report_section_keywords
    assert forged.chunk_templates  # offsec baseline graph
    assert "bulwark" in forged.policy_defaults

    # ---- weapon-forge can now target the fresh pack ----
    assert "mydomain" in pack_mod.list_packs(tmp_path)
    resolved = pack_mod.resolve_pack("mydomain", tmp_path)
    assert resolved.name == "mydomain"
    # a weapon session configured for it resolves the runtime pack (not built-in)
    ws = ForgeSession.new(tmp_path, weapon_target_id="w1", operator_id="op",
                          config=ForgeConfig(kind=ForgeKind.WEAPON.value, alphabet_pack="mydomain"))
    assert pack_mod.resolve_pack(ws.config.alphabet_pack, tmp_path).name == "mydomain"
