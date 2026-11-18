from __future__ import annotations

from pathlib import Path

from ara import temper_dedup, temper_lifecycle


def _abs(i: int) -> dict:
    return {"destination": "absolute", "title": f"A{i}", "candidate_id": f"c{i}",
            "control": {"principle": f"principle {i}"}, "scope": ["web_app"]}


def _session_with(tmp_path: Path, cands: list) -> None:
    temper_lifecycle.start_tailor_session(
        tmp_path, session_id="s", source_request={"armor": "x"})
    rec = temper_lifecycle.load_tailor_session(tmp_path)
    rec["candidates"] = cands
    temper_lifecycle._write_tailor_session(tmp_path, rec)


def test_dedup_collapses_duplicates_nondestructively(tmp_path: Path) -> None:
    _session_with(tmp_path, [_abs(i) for i in range(5)])
    res = temper_dedup.dedup_tailor_session(
        tmp_path, model_invoke=lambda s, u: "[[0,1,2]]")
    assert res.kept_total == 3 and res.dropped_total == 2 and res.applied
    rec = temper_lifecycle.load_tailor_session(tmp_path)
    assert len(rec["candidates"]) == 3
    assert len(rec["deduped_out"]) == 2
    # dropped are tagged with what they merged into (reversible, not deleted)
    assert all(c["_dedup_merged_into"] == "c0" for c in rec["deduped_out"])
    assert rec["dedup_history"][-1]["dropped"] == 2


def test_restore_dedup_fully_undoes(tmp_path: Path) -> None:
    _session_with(tmp_path, [_abs(i) for i in range(5)])
    temper_dedup.dedup_tailor_session(tmp_path, model_invoke=lambda s, u: "[[0,1,2]]")
    n = temper_dedup.restore_dedup(tmp_path)
    assert n == 2
    rec = temper_lifecycle.load_tailor_session(tmp_path)
    assert len(rec["candidates"]) == 5 and rec["deduped_out"] == []
    assert all("_dedup_merged_into" not in c for c in rec["candidates"])


def test_dedup_dry_run_does_not_touch_session(tmp_path: Path) -> None:
    _session_with(tmp_path, [_abs(i) for i in range(5)])
    res = temper_dedup.dedup_tailor_session(
        tmp_path, apply=False, model_invoke=lambda s, u: "[[0,1,2]]")
    assert res.kept_total == 3 and res.applied is False
    rec = temper_lifecycle.load_tailor_session(tmp_path)
    assert len(rec["candidates"]) == 5           # untouched
    assert not rec.get("deduped_out")


def test_dedup_scopes_to_named_destinations(tmp_path: Path) -> None:
    cands = [_abs(0), _abs(1), {
        "destination": "bulwark", "title": "B", "candidate_id": "cb",
        "control": {"match": "x"}, "scope": ["global"]}]
    _session_with(tmp_path, cands)
    temper_dedup.dedup_tailor_session(
        tmp_path, destinations=["absolute"], model_invoke=lambda s, u: "[[0,1]]")
    rec = temper_lifecycle.load_tailor_session(tmp_path)
    assert len(rec["candidates"]) == 2            # 1 absolute kept + bulwark
    assert any(c["destination"] == "bulwark" for c in rec["candidates"])


def test_dedup_bad_model_output_is_a_noop(tmp_path: Path) -> None:
    _session_with(tmp_path, [_abs(i) for i in range(4)])
    res = temper_dedup.dedup_tailor_session(
        tmp_path, model_invoke=lambda s, u: "not json at all")
    assert res.kept_total == 4 and res.dropped_total == 0   # nothing merged
