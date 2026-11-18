from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara.forge import loop_driver
from ara.forge.phases import deposit, exhaust, manifest
from ara.forge.session import ForgeConfig, ForgePhase, ForgeSession


def _forge_to_exhaust(tmp_path: Path):
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(
        state_root, weapon_target_id="w1", operator_id="op",
        config=ForgeConfig(alphabet_pack=""))
    md = sources / "r.md"
    md.write_text(
        "# Findings\n\n## A\nJTAG debug enabled on WROOM-32D\n\n"
        "## B\nSQLi in /login\n\n## C\nHardcoded API key in firmware\n")
    deposit.deposit_mat(sess, md)
    deposit.finalize_deposit(sess)
    elements = manifest.run_manifest(sess)
    manifest.approve_manifest(sess)
    assert sess.phase == ForgePhase.EXHAUST
    return sess, elements


def test_run_exhaust_stateless_records_grounded_extractions(tmp_path: Path) -> None:
    sess, elements = _forge_to_exhaust(tmp_path)

    def fake(system: str, user: str) -> str:
        return json.dumps([
            {"kind": "proc", "body": {"name": "P", "steps": ["s1"]}},
            {"kind": "fact_shape", "body": {"name": "F", "fields": ["x"]}},
        ])

    res = loop_driver.run_exhaust_stateless(
        sess.state_root, sess.forge_id, model_invoke=fake)

    assert res.elements_total == len(elements)
    assert res.elements_examined == len(elements)         # every element once
    assert res.extractions_added == 2 * len(elements)     # 2 per element
    # every recorded extraction is grounded to a real manifest element
    exts = exhaust.read_extractions(sess.state_root, sess.forge_id)
    valid = {e.id for e in elements}
    assert exts and all(x.manifest_element_id in valid for x in exts)
    assert res.final_coverage == 1.0                      # all elements covered


def test_run_exhaust_stateless_skips_bad_kind_and_nondict_body(tmp_path: Path) -> None:
    sess, elements = _forge_to_exhaust(tmp_path)

    def fake(system: str, user: str) -> str:
        return json.dumps([
            {"kind": "not_a_channel", "body": {"name": "x"}},   # bad kind -> skip
            {"kind": "proc", "body": "just a string"},          # non-dict body -> skip
            {"kind": "extract.conc", "body": {"name": "ok"}},   # 'extract.' prefix ok
        ])

    res = loop_driver.run_exhaust_stateless(
        sess.state_root, sess.forge_id, model_invoke=fake)
    assert res.extractions_added == len(elements)          # only the valid conc each


def test_run_exhaust_stateless_empty_yields_nothing(tmp_path: Path) -> None:
    sess, elements = _forge_to_exhaust(tmp_path)
    res = loop_driver.run_exhaust_stateless(
        sess.state_root, sess.forge_id, model_invoke=lambda s, u: "[]")
    assert res.elements_examined == len(elements)
    assert res.extractions_added == 0


def test_run_exhaust_stateless_requires_exhaust_phase(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    sources = tmp_path / "src"
    sources.mkdir()
    sess = ForgeSession.new(
        state_root, weapon_target_id="w1", operator_id="op",
        config=ForgeConfig(alphabet_pack=""))
    # still in DEPOSIT — not EXHAUST
    with pytest.raises(RuntimeError):
        loop_driver.run_exhaust_stateless(
            state_root, sess.forge_id, model_invoke=lambda s, u: "[]")
