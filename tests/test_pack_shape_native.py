"""Vector/matrix alphabets must seal with their shape-native structure
(axes / axes+cells), not as empty shells. Regression guard for the gap the
gainsec-offsec forge surfaced: the agent modelled every shape as
categorical fields+rows, so vector axes and matrix cells sealed empty."""
import pytest

from ara.forge.channels import ProposeAlphabetChannel
from ara.forge.pack_draft import build_pack_draft
from ara.forge.phases.seal import _validate_pack
from ara.runtime.channels.base import ChannelRejection


def _validate(inner: dict):
    ch = ProposeAlphabetChannel(state_root="/tmp")
    ch.validate({"manifest_element_id": "e1", "body": inner})


def test_vector_requires_axes():
    with pytest.raises(ChannelRejection):
        _validate({"name": "v", "shape": "vector", "fields": []})
    # valid vector passes
    _validate({"name": "v", "shape": "vector",
               "axes": [{"name": "AV", "min": 0.0, "max": 1.0}]})


def test_matrix_requires_axes_and_cell_fields():
    with pytest.raises(ChannelRejection):
        _validate({"name": "m", "shape": "matrix", "axes": ["metric"]})
    with pytest.raises(ChannelRejection):
        _validate({"name": "m", "shape": "matrix",
                   "cell_fields": [{"name": "w", "type": "float"}]})
    _validate({"name": "m", "shape": "matrix", "axes": ["metric", "value"],
               "cell_fields": [{"name": "w", "type": "float"}]})


def test_categorical_unaffected():
    _validate({"name": "c", "shape": "categorical",
               "fields": [{"name": "id", "type": "str"}]})


def test_shape_native_content_seals():
    events = [
        {"channel": "propose.alphabet", "manifest_element_id": "e1",
         "body": {"name": "cvss_vec", "shape": "vector", "description": "d",
                  "axes": [{"name": "AttackVector", "min": 0.0, "max": 1.0,
                            "description": "AV"},
                           {"name": "AttackComplexity", "min": 0.0, "max": 1.0}]}},
        {"channel": "propose.alphabet", "manifest_element_id": "e2",
         "body": {"name": "cvss_mtx", "shape": "matrix", "description": "d",
                  "axes": ["metric", "value"],
                  "cell_fields": [{"name": "weight", "type": "float"}]}},
        {"channel": "extract.conc", "manifest_element_id": "e2",
         "body": {"alphabet_name": "cvss_mtx",
                  "coords": {"metric": "AV", "value": "Network"},
                  "cell": {"weight": 0.85}}},
        {"channel": "extract.conc", "manifest_element_id": "e2",
         "body": {"alphabet_name": "cvss_mtx",
                  "coords": {"metric": "AV", "value": "Local"},
                  "cell": {"weight": 0.55}}},
    ]
    pack = build_pack_draft(events, name="t").to_pack()
    by = {a.name: a for a in pack.alphabets}

    # vector survives thin-pre-uncheck (0 entry rows) and ships real axes
    assert "cvss_vec" in by, "vector alphabet was dropped"
    vec = by["cvss_vec"]
    assert [ax.name for ax in vec.schema.axes] == ["AttackVector",
                                                   "AttackComplexity"]

    # matrix ships axes + cell_fields and its cell data survives as starters
    mtx = by["cvss_mtx"]
    assert mtx.schema.axes == ["metric", "value"]
    assert [f.name for f in mtx.schema.cell_fields] == ["weight"]
    assert len(mtx.starter) == 2
    assert mtx.starter[0]["coords"]["metric"] == "AV"

    assert not _validate_pack(pack), "shape-native pack should be seal-ready"


def test_missing_shape_defaults_to_categorical():
    """The agent sometimes omits 'shape'; the pack must still assemble
    (default categorical) rather than ShapeKind(None) crashing the seal."""
    events = [
        {"channel": "propose.alphabet", "manifest_element_id": "e1",
         "body": {"name": "noshape", "description": "d"}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "noshape", "entry": {"id": "a", "label": "A"}}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "noshape", "entry": {"id": "b", "label": "B"}}},
    ]
    pack = build_pack_draft(events, name="t").to_pack()
    a = {x.name: x for x in pack.alphabets}["noshape"]
    assert a.shape.value == "categorical"
    assert len(a.starter) == 2


def test_matrix_flat_entry_rows_split_to_cells():
    """The agent emits matrix content as flat entries (axis values + cell
    value in one dict). The builder splits them into coords/cell using the
    declared axes/cell_fields — no reliance on the agent pre-splitting."""
    events = [
        {"channel": "propose.alphabet", "manifest_element_id": "e1",
         "body": {"name": "temporal", "shape": "matrix", "description": "d",
                  "axes": ["metric_name", "metric_value"],
                  "cell_fields": [{"name": "multiplier", "type": "float"}]}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "temporal",
                  "entry": {"metric_name": "Exploit Code Maturity",
                            "metric_value": "Functional", "multiplier": 0.97}}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "temporal",
                  "entry": {"metric_name": "Exploit Code Maturity",
                            "metric_value": "High", "multiplier": 1.0}}},
    ]
    pack = build_pack_draft(events, name="t").to_pack()
    mtx = {a.name: a for a in pack.alphabets}["temporal"]
    assert len(mtx.starter) == 2
    row = mtx.starter[0]
    assert row["coords"] == {"metric_name": "Exploit Code Maturity",
                             "metric_value": "Functional"}
    assert row["cell"] == {"multiplier": 0.97}
    assert not _validate_pack(pack)


def test_merge_starter_tolerates_missing_id():
    """A categorical starter row lacking 'id' (agent gave only label) must
    merge via a derived key instead of KeyError-ing the whole weapon build."""
    from ara.forge.alphabets import projector
    events = [
        {"channel": "propose.alphabet", "manifest_element_id": "e1",
         "body": {"name": "sev", "shape": "categorical",
                  "fields": [{"name": "id", "type": "str"}]}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "sev", "entry": {"label": "High"}}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "sev", "entry": {"label": "Low"}}},
    ]
    pack = build_pack_draft(events, name="t").to_pack()
    der = projector.project([], pack)
    projector.merge_starter(der)  # must not raise
    assert set(der.categorical["sev"].entries) == {"high", "low"}


# ---- enforcement (reject non-conforming input, don't silently tolerate) ----

def test_shape_is_required():
    """propose.alphabet with no shape is REJECTED (not defaulted) so the
    agent must declare it, rather than a matrix silently becoming categorical."""
    with pytest.raises(ChannelRejection):
        _validate({"name": "x", "description": "d"})
    with pytest.raises(ChannelRejection):
        _validate({"name": "x", "shape": "bogus"})


def test_matrix_cell_validator_rejects_categorical_rows():
    from ara.forge.channels import _validate_matrix_cell
    decl = {"shape": "matrix", "axes": ["metric", "value"],
            "cell_fields": [{"name": "weight", "type": "float"}]}
    # the failure mode from cvss_base_metric_weight: id/label, no coords/value
    with pytest.raises(ChannelRejection):
        _validate_matrix_cell(decl, {"alphabet_name": "m",
                                     "entry": {"id": "AV:Network",
                                               "label": "Attack Vector: Network"}})
    # coords present but cell value absent -> reject
    with pytest.raises(ChannelRejection):
        _validate_matrix_cell(decl, {"alphabet_name": "m",
                                     "coords": {"metric": "AV", "value": "Network"},
                                     "cell": {}})
    # proper {coords, cell} -> ok
    _validate_matrix_cell(decl, {"alphabet_name": "m",
                                 "coords": {"metric": "AV", "value": "Network"},
                                 "cell": {"weight": 0.85}})
    # flat entry whose keys match the schema -> ok
    _validate_matrix_cell(decl, {"alphabet_name": "m",
                                 "entry": {"metric": "AV", "value": "Local",
                                           "weight": 0.55}})


def test_categorical_entry_validator_rejects_empty():
    from ara.forge.channels import _validate_categorical_entry
    with pytest.raises(ChannelRejection):
        _validate_categorical_entry({"alphabet_name": "c", "entry": {}})
    _validate_categorical_entry({"alphabet_name": "c", "entry": {"label": "High"}})


def test_extra_validate_routes_by_declared_shape(monkeypatch):
    """extract.conc looks up the target alphabet's declared shape and applies
    the matching validator (matrix rejects id/label rows; vector rejects rows)."""
    from ara.forge import channels
    ch = channels.ExtractConcChannel(state_root="/tmp")

    def fake_decl(name):
        return {
            "mtx": {"shape": "matrix", "axes": ["a", "b"],
                    "cell_fields": [{"name": "w", "type": "float"}]},
            "vec": {"shape": "vector", "axes": [{"name": "x"}]},
            "cat": {"shape": "categorical"},
        }.get(name)
    monkeypatch.setattr(channels, "_lookup_alphabet_decl",
                        lambda sr, fid, name: fake_decl(name))

    # matrix target, categorical-style row -> reject
    with pytest.raises(ChannelRejection):
        ch._extra_validate("/tmp", "f1",
                           {"alphabet_name": "mtx", "entry": {"id": "z"}})
    # vector target gets any row -> reject
    with pytest.raises(ChannelRejection):
        ch._extra_validate("/tmp", "f1",
                           {"alphabet_name": "vec", "entry": {"id": "z"}})
    # categorical good row -> ok
    ch._extra_validate("/tmp", "f1",
                       {"alphabet_name": "cat", "entry": {"label": "High"}})
    # unknown / not-yet-declared alphabet -> allowed (can't conform yet)
    ch._extra_validate("/tmp", "f1",
                       {"alphabet_name": "unknown", "entry": {"id": "z"}})
    # no alphabet_name (weapon-forge conc) -> allowed
    ch._extra_validate("/tmp", "f1", {"shape": "rubric"})


def test_matrix_coords_cell_wins_over_entry():
    """When the agent sends BOTH a categorical {entry} and matrix
    {coords, cell} in one extract.conc, the coords/cell must win — otherwise
    the real weight is discarded for {id,label} and the matrix seals valueless."""
    events = [
        {"channel": "propose.alphabet", "manifest_element_id": "e1",
         "body": {"name": "m", "shape": "matrix", "axes": ["metric", "value"],
                  "cell_fields": [{"name": "weight", "type": "float"}]}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "m",
                  "coords": {"metric": "AV", "value": "Network"},
                  "cell": {"weight": 0.85},
                  "entry": {"id": "AV:Network", "label": "Attack Vector"}}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "m",
                  "coords": {"metric": "AV", "value": "Local"},
                  "cell": {"weight": 0.55},
                  "entry": {"id": "AV:Local", "label": "Attack Vector"}}},
    ]
    mtx = {a.name: a for a in build_pack_draft(events, name="t").to_pack().alphabets}["m"]
    assert len(mtx.starter) == 2
    assert all(r["cell"].get("weight") for r in mtx.starter), \
        "coords/cell with the real weight must survive, not the {id,label} entry"


def test_matrix_axes_and_cell_fields_must_be_disjoint():
    """A field can't be both a lookup axis and the cell value. Catches the
    report_severity_weight bug: axes=['severity_label','weight'] with
    cell_fields=[weight] — 'weight' can't be both dimension and value."""
    with pytest.raises(ChannelRejection):
        _validate({"name": "sev", "shape": "matrix",
                   "axes": ["severity_label", "weight"],
                   "cell_fields": [{"name": "weight", "type": "int"}]})
    # clean single-axis severity matrix is fine
    _validate({"name": "sev", "shape": "matrix", "axes": ["severity_label"],
               "cell_fields": [{"name": "weight", "type": "int"}]})


def test_seal_flags_conflicting_matrix_cells():
    """A matrix where one coordinate maps to two DIFFERENT cell values is a
    contradiction (conflated source tables) and must block seal; identical
    duplicate coords are fine."""
    from ara.forge.phases.seal import _validate_pack
    events = [
        {"channel": "propose.alphabet", "manifest_element_id": "e1",
         "body": {"name": "sev", "shape": "matrix", "axes": ["label"],
                  "cell_fields": [{"name": "weight", "type": "int"}]}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "sev", "coords": {"label": "CRITICAL"},
                  "cell": {"weight": 10}}},
        {"channel": "extract.conc", "manifest_element_id": "e1",
         "body": {"alphabet_name": "sev", "coords": {"label": "CRITICAL"},
                  "cell": {"weight": 4}}},  # conflict!
    ]
    pack = build_pack_draft(events, name="t").to_pack()
    fails = _validate_pack(pack)
    assert any("conflicting cells" in f for f in fails), fails

    # same value duplicated -> no conflict
    events[2]["body"]["cell"] = {"weight": 10}
    pack2 = build_pack_draft(events, name="t").to_pack()
    assert not any("conflicting" in f for f in _validate_pack(pack2))
