import json
from types import SimpleNamespace

from ara.forge.pack_draft import PackDraft


def test_cli_toggle_and_add_persist(tmp_path):
    from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind
    from ara.forge.phases import synthesize
    from ara.forge import cli
    s = ForgeSession.new(tmp_path, weapon_target_id="d", operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    d = PackDraft(name="d")
    d.add_alphabet({"name": "a", "shape": "categorical", "fields": [{"name": "id", "type": "str"}]})
    synthesize.save_pack_draft(tmp_path, s.forge_id, d)

    ns = lambda **kw: SimpleNamespace(state_root=str(tmp_path), forge_id=s.forge_id, **kw)
    assert cli._cmd_pack_toggle(ns(group="alphabets", index=0, state="off")) == 0
    assert synthesize.load_pack_draft(tmp_path, s.forge_id).items_for("alphabets")[0]["keep"] is False
    assert cli._cmd_pack_add(ns(group="entries",
                                json=json.dumps({"alphabet_name": "a", "entry": {"id": "z"}}))) == 0
    reloaded = synthesize.load_pack_draft(tmp_path, s.forge_id)
    assert reloaded.items_for("entries")[-1]["data"]["entry"]["id"] == "z"
    assert cli._cmd_pack_review(ns()) == 0


def _draft():
    d = PackDraft(name="m")
    d.add_alphabet({"name": "a", "shape": "categorical", "fields": [{"name": "id", "type": "str"}]})
    d.add_entry("a", {"id": "x"})
    return d


def test_toggle_excludes_from_pack():
    d = _draft()
    d.toggle("entries", 0, False)
    assert d.to_pack().get("a").starter == []
    d.toggle("entries", 0, True)
    assert any(e["id"] == "x" for e in d.to_pack().get("a").starter)


def test_add_custom_entry_included_and_tagged():
    d = _draft()
    d.add_item("entries", {"alphabet_name": "a", "entry": {"id": "custom1"}})
    assert d.items_for("entries")[-1]["source"] == "custom"
    assert any(e["id"] == "custom1" for e in d.to_pack().get("a").starter)


def test_edit_replaces_data():
    d = _draft()
    d.edit("alphabets", 0, {"name": "a", "shape": "categorical",
                            "fields": [{"name": "id", "type": "str"},
                                       {"name": "label", "type": "str?"}]})
    fields = [f.name for f in d.to_pack().get("a").schema.fields]
    assert "label" in fields


def test_review_lines_marks_keep():
    d = _draft()
    d.toggle("entries", 0, False)
    lines = d.review_lines()
    assert any(l.startswith("[x]") and "alphabets[0]" in l for l in lines)
    assert any(l.startswith("[ ]") and "entries[0]" in l for l in lines)
