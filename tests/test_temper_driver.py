from __future__ import annotations

from pathlib import Path

import pytest

from ara import temper_driver, temper_lifecycle


def _candidate(source: str, n: int) -> dict:
    return {
        "destination": "canary",
        "thread_type": "secret_handling",
        "title": f"Canary {source} {n}",
        "source": source,
        "rationale": "Derived from the source manifest material.",
        "risk": "Agent may disclose secrets.",
        "default_decision": "keep_for_review",
        "proposed_text": "Trip when a reply includes raw secret material.",
        "control": {
            "signal": f"sig-{source}-{n}",
            "pattern": f"regex:SECRET-{source}-{n}",
            "severity": "high",
            "trip_when": "raw secret in output",
            "response": "alert operator",
        },
        "citations": [{"manifest_element_id": f"elem_{source}"}],
        "scope": ["global"],
    }


def test_run_tailor_loops_and_accumulates(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(
        tmp_path, session_id="tailor-driver",
        source_request={"armor": "hermes-default"},
    )
    calls: list[str] = []

    # Fake Needle: emits exactly one accepted candidate per invocation.
    def fake_invoke(state_root: Path, prompt: str) -> int:
        calls.append(prompt)
        n = len(calls)
        # source name is embedded in the focused prompt
        source = "src-a" if "src-a" in prompt else "src-b"
        temper_lifecycle.add_tailor_candidate(state_root, _candidate(source, n))
        return 0

    result = temper_driver.run_tailor(
        tmp_path,
        ["src-a", "src-b"],
        target_per_source=2,
        max_iterations_per_source=4,
        agent_invoke=fake_invoke,
    )

    # 2 per source x 2 sources = 4 accepted, reached without exhausting budget.
    assert result.accepted_total == 4
    assert {s.source for s in result.sources} == {"src-a", "src-b"}
    assert all(s.iterations == 2 for s in result.sources)


def test_run_tailor_bails_source_on_no_progress(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(
        tmp_path, session_id="tailor-driver",
        source_request={"armor": "hermes-default"},
    )
    calls: list[int] = []

    # Fake Needle that always "fails" (emits nothing) — simulates repeated
    # validation rejections. The driver must bail the source, not loop forever.
    def failing_invoke(state_root: Path, prompt: str) -> int:
        calls.append(1)
        return 1

    result = temper_driver.run_tailor(
        tmp_path,
        ["src-a"],
        target_per_source=5,
        max_iterations_per_source=10,
        no_progress_bail=2,
        agent_invoke=failing_invoke,
    )

    assert result.accepted_total == 0
    # bailed after exactly no_progress_bail turns, not all 10
    assert result.sources[0].iterations == 2


def test_run_tailor_requires_running_session(tmp_path: Path) -> None:
    with pytest.raises(temper_lifecycle.TailorLifecycleError):
        temper_driver.run_tailor(tmp_path, ["src-a"], agent_invoke=lambda s, p: 0)


# --- element-grounded exhaustion driver ----------------------------------

import re


def _seed_forge_manifest(tmp_path: Path, forge_id: str, n: int, mats: int = 3) -> None:
    from ara.forge.manifest.element import ManifestElement, ManifestKind, write_manifest
    from ara.forge import state as fstate
    els = [
        ManifestElement(
            id=f"elem_{i:04d}", source_mat_id=f"mat_{i % mats}",
            byte_range=(0, 10), sha256="0" * 64,
            kind=ManifestKind.REPORT_SECTION,
            extracted_text=f"manifest text {i} about secret handling",
        )
        for i in range(n)
    ]
    p = fstate.forge_manifest_path(tmp_path, forge_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(p, els)


def _valid_candidate(elem_id: str, n: int) -> dict:
    return {
        "destination": "canary", "thread_type": "secret_handling",
        "title": f"Canary {n}", "source": elem_id,
        "rationale": "Derived from the cited manifest material.",
        "risk": "secret disclosure", "default_decision": "keep_for_review",
        "proposed_text": "Trip when a reply includes raw secret material.",
        "control": {
            "signal": f"sig-{n}", "pattern": f"regex:SECRET-{n}",
            "severity": "high", "trip_when": "raw secret", "response": "alert",
        },
        "citations": [{"manifest_element_id": elem_id}], "scope": ["global"],
    }


def test_run_tailor_exhaust_examines_every_element(tmp_path: Path) -> None:
    _seed_forge_manifest(tmp_path, "forge-x", 30, mats=3)
    temper_lifecycle.start_tailor_session(
        tmp_path, session_id="t-exhaust",
        source_request={"armor": "hermes-default", "source_forge": "forge-x"},
    )
    counter = {"n": 0}

    def fake(state_root: Path, prompt: str) -> int:
        ids = re.findall(r"elem_\d{4}", prompt)
        for j, eid in enumerate(ids):
            if j % 3 == 0:  # a guardrail for ~1 in 3 elements
                counter["n"] += 1
                temper_lifecycle.add_tailor_candidate(
                    state_root, _valid_candidate(eid, counter["n"]))
        return 0

    res = temper_driver.run_tailor_exhaust(
        tmp_path, target_coverage=1.0, batch_size=10, agent_invoke=fake)

    assert res.elements_total == 30
    assert res.elements_examined == 30          # every element examined once
    assert res.iterations == 3                  # 30 / batch 10
    assert res.coverage == 1.0
    assert res.stopped_reason == "all_elements_examined"
    assert res.accepted_total >= 8              # yield scales with material


def test_run_tailor_exhaust_partial_coverage_samples_all_mats(tmp_path: Path) -> None:
    _seed_forge_manifest(tmp_path, "forge-x", 30, mats=3)
    temper_lifecycle.start_tailor_session(
        tmp_path, session_id="t-exhaust",
        source_request={"armor": "hermes-default", "source_forge": "forge-x"},
    )
    seen_mats: set = set()

    def fake(state_root: Path, prompt: str) -> int:
        for m in re.findall(r"mat_\d", prompt):
            seen_mats.add(m)
        return 0

    # examine just the first batch (interleaved) — should touch all 3 mats
    res = temper_driver.run_tailor_exhaust(
        tmp_path, target_coverage=0.2, batch_size=10, agent_invoke=fake)
    assert seen_mats == {"mat_0", "mat_1", "mat_2"}
    assert res.stopped_reason == "target_coverage_reached"


def test_run_tailor_exhaust_requires_source_manifest(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(
        tmp_path, session_id="t", source_request={"armor": "hermes-default"})
    with pytest.raises(temper_lifecycle.TailorLifecycleError):
        temper_driver.run_tailor_exhaust(tmp_path, agent_invoke=lambda s, p: 0)


# --- stateless per-element driver -----------------------------------------

import json as _json


def _canary_json(title: str, *, thread_type: str = "secret_handling",
                 drop: str | None = None) -> str:
    cand = {
        "destination": "canary", "thread_type": thread_type, "title": title,
        "rationale": "Derived from the cited manifest material.",
        "risk": "secret disclosure",
        "proposed_text": "Trip when output includes raw secret material.",
        "control": {"signal": "sig", "pattern": "SECRET-[0-9]+",
                    "severity": "high", "trip_when": "raw secret",
                    "response": "alert"},
        "scope": ["global"],
    }
    if drop:
        cand.pop(drop, None)
    return _json.dumps([cand])


def _start_stateless_session(tmp_path: Path) -> None:
    _seed_forge_manifest(tmp_path, "forge-s", 6, mats=3)
    temper_lifecycle.start_tailor_session(
        tmp_path, session_id="t-stateless",
        source_request={"armor": "hermes-default", "source_forge": "forge-s"},
    )


def test_run_tailor_stateless_one_call_per_element(tmp_path: Path) -> None:
    _start_stateless_session(tmp_path)
    calls: list[str] = []

    def fake(system: str, user: str) -> str:
        calls.append(user)
        eid = re.search(r"elem_\d{4}", user).group(0)
        return _canary_json(f"Canary {eid}")

    res = temper_driver.run_tailor_stateless(tmp_path, model_invoke=fake)

    assert res.elements_total == 6
    assert res.elements_examined == 6           # every element examined once
    assert res.accepted_total == 6              # one valid candidate each
    assert res.coverage == 1.0
    assert len(calls) == 6                       # exactly one model call per element


def test_run_tailor_stateless_grounds_citation_to_the_element(tmp_path: Path) -> None:
    # the model never supplies a citation; the driver pins it to the element,
    # so grounding is guaranteed and citation-verification passes.
    _start_stateless_session(tmp_path)

    def fake(system: str, user: str) -> str:
        eid = re.search(r"elem_\d{4}", user).group(0)
        return _canary_json(f"C {eid}")

    temper_driver.run_tailor_stateless(tmp_path, model_invoke=fake)
    record = temper_lifecycle.load_tailor_session(tmp_path)
    for c in record["candidates"]:
        cites = [x["manifest_element_id"] for x in c["citations"]]
        assert len(cites) == 1 and cites[0].startswith("elem_")


def test_run_tailor_stateless_coerces_bad_thread_type(tmp_path: Path) -> None:
    _start_stateless_session(tmp_path)

    def fake(system: str, user: str) -> str:
        eid = re.search(r"elem_\d{4}", user).group(0)
        return _canary_json(f"C {eid}", thread_type="totally_made_up")

    res = temper_driver.run_tailor_stateless(tmp_path, model_invoke=fake)
    assert res.accepted_total == 6              # coerced to a valid enum, accepted
    record = temper_lifecycle.load_tailor_session(tmp_path)
    assert all(c["thread_type"] == "secret_handling" for c in record["candidates"])


def test_run_tailor_stateless_retries_on_rejection(tmp_path: Path) -> None:
    _start_stateless_session(tmp_path)

    def fake(system: str, user: str) -> str:
        eid_m = re.search(r"elem_\d{4}", user)
        if "REJECTED" in user:                  # the validate-retry ask
            return _json.dumps(_json.loads(_canary_json("Fixed"))[0])
        # first pass: emit an invalid candidate (missing required 'risk')
        return _canary_json("Bad " + (eid_m.group(0) if eid_m else "x"), drop="risk")

    res = temper_driver.run_tailor_stateless(
        tmp_path, model_invoke=fake, retry_on_reject=1)
    assert res.accepted_total == 6              # each recovered on retry


def test_run_tailor_stateless_no_retry_drops_invalid(tmp_path: Path) -> None:
    _start_stateless_session(tmp_path)

    def fake(system: str, user: str) -> str:
        return _canary_json("Bad", drop="risk")

    res = temper_driver.run_tailor_stateless(
        tmp_path, model_invoke=fake, retry_on_reject=0)
    assert res.accepted_total == 0
    assert res.rejected_total == 6


def test_run_tailor_stateless_requires_running_session(tmp_path: Path) -> None:
    with pytest.raises(temper_lifecycle.TailorLifecycleError):
        temper_driver.run_tailor_stateless(tmp_path, model_invoke=lambda s, u: "[]")


def _absolute_json(title: str, task_family: str = "hardware_security_audit") -> str:
    return _json.dumps([{
        "destination": "absolute", "thread_type": "methodology_fit",
        "title": title, "rationale": "Methodology fit for the material.",
        "risk": "agent runs unexpected tools",
        "proposed_text": "Prefer expected methodology; gate the rest.",
        "control": {"principle": "methodology fit", "allowed": ["firmware review"],
                    "restricted": ["jtag flashing"], "exception_path": "operator approval"},
        "scope": ["hardware"],
        "methodology_context": {"task_family": task_family,
                                "expected": ["firmware review"],
                                "unexpected": ["jtag flashing"],
                                "requires_context": "a hardware trim",
                                "exception_path": "operator approval"},
    }])


def test_finalize_coerces_methodology_context(tmp_path: Path) -> None:
    class _E:
        id = "elem_0001"
        source_mat_id = "mat_0"
    cand = _json.loads(_absolute_json("t"))[0]
    out = temper_driver._finalize_stateless_candidate(cand, _E())
    mc = out["methodology_context"]
    assert mc["task_family"] == "hardware"                  # free-text -> enum
    assert mc["requires_context"] == ["a hardware trim"]    # string -> list
    assert out["citations"] == [{"manifest_element_id": "elem_0001"}]


def test_run_tailor_stateless_coerces_context_and_accepts(tmp_path: Path) -> None:
    _start_stateless_session(tmp_path)

    def fake(system: str, user: str) -> str:
        eid = re.search(r"elem_\d{4}", user).group(0)
        return _absolute_json(f"Methodology {eid}")

    res = temper_driver.run_tailor_stateless(tmp_path, model_invoke=fake)
    assert res.accepted_total == 6 and res.rejected_total == 0
    record = temper_lifecycle.load_tailor_session(tmp_path)
    for c in record["candidates"]:
        assert c["methodology_context"]["task_family"] == "hardware"
        assert c["methodology_context"]["requires_context"] == ["a hardware trim"]
