"""Reconciliation detection + apply ops (the reduce-step fix)."""
from ara.forge.pack_draft import PackDraft, build_pack_draft
from ara.forge import reconcile as R
from ara.forge.phases.seal import _validate_pack


def _matrix_events(rows, name="sev", axes=("label",), cf="weight"):
    ev = [{"channel": "propose.alphabet", "manifest_element_id": "d1",
           "body": {"name": name, "shape": "matrix", "axes": list(axes),
                    "cell_fields": [{"name": cf, "type": "int"}]}}]
    for i, (coords, cell, eid) in enumerate(rows):
        ev.append({"channel": "extract.conc", "manifest_element_id": eid,
                   "body": {"alphabet_name": name, "coords": coords,
                            "cell": cell}})
    return ev


def test_detect_contradiction():
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "mA"),
        ({"label": "CRITICAL"}, {"weight": 4}, "mB"),   # conflict
        ({"label": "LOW"}, {"weight": 4}, "mA"),
    ]), name="t")
    groups = R.detect_contradictions(draft)
    assert len(groups) == 1
    g = groups[0]
    assert g["alphabet"] == "sev"
    assert g["conflicts"][0]["coords"] == {"label": "CRITICAL"}
    assert {v["cell"]["weight"] for v in g["conflicts"][0]["values"]} == {10, 4}


def test_no_contradiction_when_values_agree():
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "mA"),
        ({"label": "CRITICAL"}, {"weight": 10}, "mB"),   # same value: fine
    ]), name="t")
    assert R.detect_contradictions(draft) == []


def test_detect_duplicates_by_name_tokens():
    ev = [
        {"channel": "propose.alphabet", "manifest_element_id": "d1",
         "body": {"name": "cvss_base_weights", "shape": "matrix",
                  "axes": ["metric", "value"],
                  "cell_fields": [{"name": "w", "type": "float"}]}},
        {"channel": "propose.alphabet", "manifest_element_id": "d2",
         "body": {"name": "cvss_base_metric_weights", "shape": "matrix",
                  "axes": ["metric", "value"],
                  "cell_fields": [{"name": "w", "type": "float"}]}},
        {"channel": "propose.alphabet", "manifest_element_id": "d3",
         "body": {"name": "totally_unrelated", "shape": "matrix",
                  "axes": ["a", "b"], "cell_fields": [{"name": "w", "type": "float"}]}},
    ]
    # give each a couple cells so they aren't thin-dropped
    for nm in ("cvss_base_weights", "cvss_base_metric_weights", "totally_unrelated"):
        for v in ("x", "y"):
            ev.append({"channel": "extract.conc", "manifest_element_id": "e",
                       "body": {"alphabet_name": nm,
                                "coords": {"metric": v, "value": v},
                                "cell": {"w": 1.0}}})
    groups = R.detect_duplicates(build_pack_draft(ev, name="t"))
    assert len(groups) == 1
    assert set(groups[0]["alphabets"]) == {"cvss_base_weights",
                                           "cvss_base_metric_weights"}


def test_apply_resolve_cell_fixes_contradiction():
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "mA"),
        ({"label": "CRITICAL"}, {"weight": 4}, "mB"),
        ({"label": "LOW"}, {"weight": 4}, "mA"),
    ]), name="t")
    R.apply_resolve_cell(draft, "sev", {"label": "CRITICAL"}, {"weight": 10})
    assert R.detect_contradictions(draft) == []
    # the kept CRITICAL cell is 10, LOW untouched
    kept = [(it["data"]["entry"]["coords"], it["data"]["entry"]["cell"])
            for it in draft.entries if it.get("keep", True)]
    assert ({"label": "CRITICAL"}, {"weight": 10}) in kept
    assert ({"label": "CRITICAL"}, {"weight": 4}) not in kept


def test_apply_merge_repoints_entries_and_bindings():
    ev = _matrix_events([({"metric": "AV", "value": "N"}, {"w": 0.85}, "e1"),
                         ({"metric": "AV", "value": "A"}, {"w": 0.62}, "e1")],
                        name="cvss_base_weights", axes=("metric", "value"), cf="w")
    ev += _matrix_events([({"metric": "AV", "value": "L"}, {"w": 0.55}, "e2"),
                          ({"metric": "AV", "value": "P"}, {"w": 0.2}, "e2")],
                         name="cvss_base_metric_weights", axes=("metric", "value"), cf="w")
    ev.append({"channel": "propose.binding", "manifest_element_id": "b",
               "body": {"field": "sev", "rule": "FIELD_IN_MATRIX",
                        "alphabet": "cvss_base_metric_weights"}})
    draft = build_pack_draft(ev, name="t")
    R.apply_merge(draft, ["cvss_base_weights", "cvss_base_metric_weights"],
                  into="cvss_base_weights")
    live_alpha = [it["data"]["name"] for it in draft.alphabets if it.get("keep", True)]
    assert "cvss_base_metric_weights" not in live_alpha
    assert "cvss_base_weights" in live_alpha
    # both cells now under the merged name
    live_entry_alpha = {it["data"]["alphabet_name"]
                        for it in draft.entries if it.get("keep", True)}
    assert live_entry_alpha == {"cvss_base_weights"}
    # binding re-pointed
    bnd = [it["data"]["alphabet"] for it in draft.bindings if it.get("keep", True)]
    assert bnd == ["cvss_base_weights"]


def test_apply_split_by_source_element():
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "gainsec"),
        ({"label": "LOW"}, {"weight": 4}, "gainsec"),
        ({"label": "CRITICAL"}, {"weight": 4}, "hardware"),
        ({"label": "HIGH"}, {"weight": 1}, "hardware"),
    ]), name="sev")
    R.apply_split(draft, "sev", [
        {"new_name": "sev__gainsec", "element_ids": ["gainsec"]},
        {"new_name": "sev__hardware", "element_ids": ["hardware"]},
    ])
    live = {it["data"]["name"] for it in draft.alphabets if it.get("keep", True)}
    assert live == {"sev__gainsec", "sev__hardware"}
    per = {}
    for it in draft.entries:
        if it.get("keep", True):
            per.setdefault(it["data"]["alphabet_name"], []).append(
                it["data"]["entry"]["coords"]["label"])
    assert set(per["sev__gainsec"]) == {"CRITICAL", "LOW"}
    assert set(per["sev__hardware"]) == {"CRITICAL", "HIGH"}
    # each split alphabet is now internally consistent
    assert R.detect_contradictions(draft) == []


def test_apply_drop():
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "mA"),
        ({"label": "LOW"}, {"weight": 4}, "mA"),
    ]), name="sev")
    R.apply_drop(draft, "sev")
    assert all(not it.get("keep", True) for it in draft.alphabets
               if it["data"].get("name") == "sev")
    assert all(not it.get("keep", True) for it in draft.entries)


def test_apply_decision_dispatch():
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "mA"),
        ({"label": "CRITICAL"}, {"weight": 4}, "mB"),
        ({"label": "LOW"}, {"weight": 4}, "mA"),
    ]), name="sev")
    R.apply_decision(draft, {"op": "resolve_cell", "alphabet": "sev",
                             "coords": {"label": "CRITICAL"},
                             "keep_cell": {"weight": 10}})
    pack = draft.to_pack()
    assert not any("conflicting" in f for f in _validate_pack(pack))


def _write_manifest(state_root, fid, ids_sources):
    from ara.forge.manifest.element import ManifestElement, ManifestKind, write_manifest
    from ara.forge import state as fstate
    els = [ManifestElement(id=i, source_mat_id=s, byte_range=(0, 1),
                           sha256="x", kind=ManifestKind.GENERIC_TEXT,
                           extracted_text="")
           for i, s in ids_sources]
    write_manifest(fstate.forge_manifest_path(state_root, fid), els)


def test_run_reconcile_applies_agent_decision(tmp_path):
    """End-to-end driver: a contradiction is detected, the (stubbed) agent
    emits a resolve_cell decision, the driver applies it, and the draft is
    conflict-free — no real contained agent needed."""
    import json
    from ara.forge.phases import synthesize as syn
    from ara.forge import state as fstate, loop_driver
    fid = "recontest"
    _write_manifest(tmp_path, fid, [("mA", "gainsec"), ("mB", "hardware")])
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "mA"),
        ({"label": "CRITICAL"}, {"weight": 4}, "mB"),
        ({"label": "LOW"}, {"weight": 4}, "mA"),
    ]), name="sev")
    syn.save_pack_draft(tmp_path, fid, draft)

    dec_path = fstate.forge_reconcile_decisions_path(tmp_path, fid)

    def stub_agent(sr, prompt):
        # the "agent" sees the conflict and keeps the CVSS-authoritative value
        assert "CONFLICTING" in prompt and "CRITICAL" in prompt
        dec_path.parent.mkdir(parents=True, exist_ok=True)
        with dec_path.open("a") as f:
            f.write(json.dumps({"op": "resolve_cell", "alphabet": "sev",
                                "coords": {"label": "CRITICAL"},
                                "keep_cell": {"weight": 10}}) + "\n")
        return 0

    result = loop_driver.run_reconcile(tmp_path, fid, agent_invoke=stub_agent)
    assert result["decisions"] == 1
    assert result["unresolved_dropped"] == 0
    final = syn.load_pack_draft(tmp_path, fid).to_pack()
    from ara.forge.phases.seal import _validate_pack
    assert not any("conflicting" in f for f in _validate_pack(final))


def test_run_reconcile_drops_unresolved(tmp_path):
    """If the agent does nothing, the unresolved conflict is dropped so seal
    still converges (never an infinite loop, never a silent wrong seal)."""
    from ara.forge.phases import synthesize as syn
    from ara.forge import loop_driver
    fid = "recontest2"
    _write_manifest(tmp_path, fid, [("mA", "s1"), ("mB", "s2")])
    draft = build_pack_draft(_matrix_events([
        ({"label": "CRITICAL"}, {"weight": 10}, "mA"),
        ({"label": "CRITICAL"}, {"weight": 4}, "mB"),
        ({"label": "LOW"}, {"weight": 4}, "mA"),
    ]), name="sev")
    syn.save_pack_draft(tmp_path, fid, draft)
    result = loop_driver.run_reconcile(tmp_path, fid,
                                       agent_invoke=lambda sr, p: 0)
    assert result["unresolved_dropped"] == 1
    final = syn.load_pack_draft(tmp_path, fid).to_pack()
    assert "sev" not in {a.name for a in final.alphabets}


def test_split_siblings_not_flagged_as_duplicates():
    """After a split, base__x and base__y are intentionally similar names and
    must NOT be re-detected as duplicates (which would undo the split)."""
    ev = _matrix_events([({"label": "CRITICAL"}, {"weight": 10}, "e1"),
                         ({"label": "LOW"}, {"weight": 4}, "e1")],
                        name="severity__gainsec")
    ev += _matrix_events([({"label": "CRITICAL"}, {"weight": 4}, "e2"),
                          ({"label": "HIGH"}, {"weight": 1}, "e2")],
                         name="severity__hardware")
    draft = build_pack_draft(ev, name="t")
    assert R.detect_duplicates(draft) == [], "split siblings wrongly flagged as dups"
