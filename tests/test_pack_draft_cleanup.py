from ara.forge.pack_draft import build_pack_draft


def _alpha(name):
    return {"channel": "propose.alphabet", "manifest_element_id": "m1",
            "body": {"name": name, "shape": "categorical",
                     "fields": [{"name": "id", "type": "str"}]}}


def _entry(alpha, eid):
    return {"channel": "extract.conc", "manifest_element_id": "m1",
            "body": {"alphabet_name": alpha, "entry": {"id": eid, "label": eid}}}


def test_thin_alphabets_pre_unchecked():
    events = [_alpha("vuln_class"), _entry("vuln_class", "a"), _entry("vuln_class", "b"),
              _entry("vuln_class", "c"),
              _alpha("lonely"), _entry("lonely", "x")]      # 1 entry -> thin
    d = build_pack_draft(events, "p")
    keep = {a["data"]["name"]: a["keep"] for a in d.items_for("alphabets")}
    assert keep["vuln_class"] is True
    assert keep["lonely"] is False        # pre-unchecked (operator opts in)


def test_formatting_variant_merge():
    events = [_alpha("Test Category"), _entry("Test Category", "auth"),
              _alpha("test_category"), _entry("test_category", "inj")]
    d = build_pack_draft(events, "p")
    names = [a["data"]["name"] for a in d.items_for("alphabets")]
    assert len(names) == 1                 # merged the formatting variant
    # both entries now point at the surviving canonical name
    ans = {e["data"]["alphabet_name"] for e in d.items_for("entries")}
    assert ans == {names[0]}


def test_semantic_synonym_NOT_merged():
    # different tokens -> left to the prompt, never fuzzy-merged
    events = [_alpha("test_category"), _entry("test_category", "a"), _entry("test_category","b"),
              _alpha("wstg_test_category"), _entry("wstg_test_category", "c"), _entry("wstg_test_category","d")]
    d = build_pack_draft(events, "p")
    assert len(d.items_for("alphabets")) == 2
