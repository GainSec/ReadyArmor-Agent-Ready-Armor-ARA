from pathlib import Path

from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind
from ara.forge.phases import synthesize
from ara.forge.pack_draft import PackDraft
from ara.armory.forge_v1_views import render_pack_review
from ara.armory.forge_actions import handle_forge_pack_toggle, handle_forge_create


def _session_with_draft(tmp_path: Path):
    s = ForgeSession.new(tmp_path, weapon_target_id="dom", operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    d = PackDraft(name="dom")
    d.add_alphabet({"name": "category", "shape": "categorical",
                    "fields": [{"name": "id", "type": "str"}]})
    d.add_entry("category", {"id": "x"})
    synthesize.save_pack_draft(tmp_path, s.forge_id, d)
    return s


def test_render_pack_review_lists_groups_and_toggles(tmp_path: Path):
    s = _session_with_draft(tmp_path)
    html = render_pack_review(tmp_path, s.forge_id)
    assert "Pack draft: dom" in html
    # batch-save form: one keep checkbox per draft item (1 alphabet + 1 entry)
    assert 'name="keep_alphabets_0"' in html
    assert 'name="keep_entries_0"' in html
    assert html.count('type="checkbox"') == 2
    # per-group add-custom inputs are present
    assert 'name="add_entries"' in html
    assert 'name="save"' in html and 'name="seal"' in html
    assert "Seal pack" in html


def test_toggle_action_flips_keep(tmp_path: Path):
    s = _session_with_draft(tmp_path)
    ok, msg = handle_forge_pack_toggle(tmp_path, s.forge_id, "entries", 0, False)
    assert ok
    d = synthesize.load_pack_draft(tmp_path, s.forge_id)
    assert d.items_for("entries")[0]["keep"] is False


def test_armory_create_pack_kind(tmp_path: Path):
    ok, msg, fid = handle_forge_create(str(tmp_path), {"weapon_target_id": "newdom", "kind": "pack"})
    assert ok, msg
    assert ForgeSession.load(tmp_path, fid).config.kind == "pack"
