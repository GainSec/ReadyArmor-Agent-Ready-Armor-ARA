import json
from pathlib import Path

from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind, ForgePhase
from ara.forge import state as fstate
from ara.forge.phases import synthesize
from ara.armory import forge_actions


def _pack_session_at(tmp_path, phase, name="dom"):
    s = ForgeSession.new(tmp_path, weapon_target_id=name, operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    s.phase = phase
    s._persist()
    return s


def _write_extractions(tmp_path, forge_id):
    path = fstate.forge_extractions_path(tmp_path, forge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"channel": "propose.alphabet",
         "body": {"name": "category", "shape": "categorical",
                  "fields": [{"name": "id", "type": "str"}]}},
        {"channel": "extract.conc",
         "body": {"alphabet_name": "category", "entry": {"id": "sqli"}}},
        {"channel": "propose.binding",
         "body": {"field": "category", "rule": "FIELD_IN_ALPHABET", "alphabet": "category"}},
    ]
    with path.open("w") as f:
        for i, ev in enumerate(events):
            f.write(json.dumps({"ts": 0.0, "manifest_element_id": "m1",
                                "channel": ev["channel"], "body": ev["body"],
                                "extraction_id": f"e{i}", "citation": {}}) + "\n")


# ---- Step 1: handle_forge_synthesize dispatches on kind ----

def test_armory_synthesize_runs_pack_path(tmp_path: Path):
    s = _pack_session_at(tmp_path, ForgePhase.SYNTHESIZE)
    _write_extractions(tmp_path, s.forge_id)
    ok, msg = forge_actions.handle_forge_synthesize(tmp_path, s.forge_id, {})
    assert ok, msg
    assert "Pack draft synthesized" in msg
    draft = synthesize.load_pack_draft(tmp_path, s.forge_id)
    assert draft is not None
    assert any(a["data"]["name"] == "category" for a in draft.items_for("alphabets"))


# ---- Step 2: kind selector in create form ----

def test_create_form_has_kind_selector(tmp_path: Path):
    from ara.armory.forge_v1_views import render_forge
    html = render_forge(tmp_path, "tok")
    assert 'name="kind"' in html
    assert 'value="pack"' in html
    # create round-trips kind=pack (backend already handles it)
    ok, msg, fid = forge_actions.handle_forge_create(str(tmp_path),
                                                     {"weapon_target_id": "d2", "kind": "pack"})
    assert ok and ForgeSession.load(tmp_path, fid).config.kind == "pack"


# ---- Step 3: batch curate ----

def test_pack_curate_applies_keep_and_add(tmp_path: Path):
    s = _pack_session_at(tmp_path, ForgePhase.REVIEW)
    _write_extractions(tmp_path, s.forge_id)
    forge_actions.handle_forge_synthesize(tmp_path, s.forge_id, {})  # builds the draft
    # keep the alphabet, drop the entry (omit its checkbox), add a custom entry
    body = {"keep_alphabets_0": "on",
            "add_entries": json.dumps({"alphabet_name": "category", "entry": {"id": "xss"}})}
    ok, msg = forge_actions.handle_forge_pack_curate(tmp_path, s.forge_id, body)
    assert ok, msg
    d = synthesize.load_pack_draft(tmp_path, s.forge_id)
    assert d.items_for("alphabets")[0]["keep"] is True
    assert d.items_for("entries")[0]["keep"] is False           # dropped (absent)
    assert any(it["data"]["entry"]["id"] == "xss" for it in d.items_for("entries"))  # custom
    assert d.items_for("entries")[-1]["source"] == "custom"


def test_pack_curate_bad_json_rejected(tmp_path: Path):
    s = _pack_session_at(tmp_path, ForgePhase.REVIEW)
    _write_extractions(tmp_path, s.forge_id)
    forge_actions.handle_forge_synthesize(tmp_path, s.forge_id, {})
    ok, msg = forge_actions.handle_forge_pack_curate(
        tmp_path, s.forge_id, {"add_bindings": "{not json"})
    assert not ok and "invalid JSON" in msg


# ---- Step 4: curate panel render ----

def test_pack_review_form_renders(tmp_path: Path):
    from ara.armory.forge_v1_views import render_pack_review, render_forge_detail
    s = _pack_session_at(tmp_path, ForgePhase.REVIEW)
    _write_extractions(tmp_path, s.forge_id)
    forge_actions.handle_forge_synthesize(tmp_path, s.forge_id, {})
    html = render_pack_review(tmp_path, s.forge_id, "tok")
    assert f'action="/forge/{s.forge_id}/pack/curate?t=tok"' in html
    assert 'name="keep_alphabets_0"' in html
    assert 'name="add_entries"' in html
    assert 'name="save"' in html and 'name="seal"' in html
    # the panel is hung on the forge detail page for pack sessions
    detail = render_forge_detail(tmp_path, s.forge_id, "tok")
    assert "pack/curate" in detail


def test_pack_review_no_draft_message(tmp_path: Path):
    from ara.armory.forge_v1_views import render_pack_review
    s = _pack_session_at(tmp_path, ForgePhase.EXHAUST)
    html = render_pack_review(tmp_path, s.forge_id, "tok")
    assert "No pack draft yet" in html


# ---- Step 5: promote handler + pack-list render ----

def _write_runtime_pack(tmp_path, name="dom"):
    from ara.forge.alphabets import pack as pack_mod
    rd = pack_mod.runtime_packs_dir(tmp_path)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{name}.json").write_text(json.dumps({
        "name": name, "version": "0.1.0", "description": "forged",
        "alphabets": [], "validator_bindings": [], "chunk_templates": [],
        "policy_defaults": {}, "manifest_detection": {},
    }))


def test_pack_promote_copies_to_builtin(tmp_path: Path, monkeypatch):
    from ara.forge.alphabets import pack as pack_mod
    _write_runtime_pack(tmp_path, "dom")
    dst_dir = tmp_path / "builtins"
    monkeypatch.setattr(pack_mod, "builtin_pack_path",
                        lambda n: dst_dir / f"{n}.json")
    ok, msg = forge_actions.handle_forge_pack_promote(tmp_path, "dom")
    assert ok, msg
    assert (dst_dir / "dom.json").exists()


def test_pack_promote_missing_rejected(tmp_path: Path):
    ok, msg = forge_actions.handle_forge_pack_promote(tmp_path, "nope")
    assert not ok and "no runtime pack" in msg


def test_pack_exhaust_card_says_contained_agent(tmp_path: Path):
    from ara.armory.forge_v1_views import _render_forge_action_panel
    html = _render_forge_action_panel("fid", "tok", {
        "phase": "exhaust", "weapon_target_id": "p",
        "config": {"kind": "pack"}})
    # the PRIMARY exhaust path is the contained agent
    assert "Codex" in html and "Pen" in html
    assert "contained" in html.lower()
    assert "Propose vocabulary" in html
    # stateless now appears only as the post-forge, agent-reviewed FALLBACK
    assert "Stateless review" in html


def test_pack_detail_resolves_runtime_pack(tmp_path: Path):
    from ara.armory.forge_v1_views import render_pack_detail
    from ara.forge.alphabets import pack as pack_mod
    rd = pack_mod.runtime_packs_dir(tmp_path)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "dom.json").write_text(json.dumps({
        "name": "dom", "version": "1", "description": "forged pack",
        "alphabets": [{"name": "vuln_category", "shape": "categorical",
                       "description": "", "schema": {"fields": [
                           {"name": "id", "type": "str"}]},
                       "starter": [{"id": "sqli"}, {"id": "xss"}]}],
        "validator_bindings": [], "chunk_templates": [],
        "policy_defaults": {}, "manifest_detection": {},
    }))
    html = render_pack_detail(tmp_path, "dom", "tok")
    assert "Pack not found" not in html
    assert "runtime" in html
    # the actual vocabulary is shown, not just a count
    assert "sqli" in html and "xss" in html


def test_pack_list_shows_runtime_with_promote(tmp_path: Path):
    from ara.armory.forge_v1_views import render_packs
    _write_runtime_pack(tmp_path, "dom")
    html = render_packs(tmp_path, "tok")
    assert "dom" in html
    assert 'action="/forge/packs/dom/promote?t=tok"' in html
    assert "runtime" in html
    # a built-in pack is still listed and has no promote button of its own
    assert "offsec" in html
