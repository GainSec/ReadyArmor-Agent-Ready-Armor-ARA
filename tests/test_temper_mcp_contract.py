from __future__ import annotations

from pathlib import Path

import pytest

from ara import temper_lifecycle
from ara.clients import temper_mcp_server


EXPECTED_CANDIDATE_TOOLS = {
    "temper.absolute_candidate": "absolute",
    "temper.bulwark_candidate": "bulwark",
    "temper.canary_candidate": "canary",
    "temper.rhema_candidate": "rhema",
    "temper.approval_gate_candidate": "approval_gate",
    "temper.alert_hook_candidate": "alert_hook",
    "temper.trim_template_candidate": "trim_template",
}
SUMMARY_TOOL = "temper.coverage_summary"


def _tool_schema(name: str) -> dict:
    for tool in temper_mcp_server._TOOLS:
        if tool["name"] == name:
            return tool["inputSchema"]
    raise AssertionError(f"tool {name!r} not found")


def _candidate_args(**overrides: object) -> dict:
    args = {
        "thread_type": "secret_handling",
        "title": "Do not reveal private signing material",
        "source": "mat_usb_firewall_pentest_v1",
        "rationale": (
            "The material repeatedly treats private keys as sensitive "
            "evidence that can be used only as a presence/absence signal."
        ),
        "risk": "Agent may expose secrets while trying to prove a finding.",
        "default_decision": "suggest",
        "proposed_text": (
            "Do not disclose private keys or certificate private material "
            "in cleartext. Report only the presence, path, and handling risk."
        ),
        "control": _canary_control(),
        "citations": [
            {
                "manifest_element_id": "elem_tls_cert_001",
                "quote": "private key material must not be copied",
            }
        ],
        "scope": ["web_app"],
    }
    args.update(overrides)
    return args


def _methodology_context(**overrides: object) -> dict:
    context = {
        "task_family": "web_app",
        "expected": ["HTTP request review", "browser/proxy validation"],
        "unexpected": ["binwalk", "firmware extraction", "/sbin/*"],
        "requires_context": ["firmware_analysis_trim", "hardware_weapon"],
        "exception_path": (
            "Require operator approval or a matching firmware/hardware trim "
            "before using these tools."
        ),
    }
    context.update(overrides)
    return context


def _absolute_control(**overrides: object) -> dict:
    control = {
        "principle": "Report secret evidence by presence and risk, not value.",
        "allowed": ["presence", "path", "owner", "risk", "redacted proof"],
        "restricted": ["private key bytes", "cleartext password", "bearer token"],
        "exception_path": "Reveal exact values only after explicit operator approval.",
    }
    control.update(overrides)
    return control


def _rhema_control(**overrides: object) -> dict:
    control = {
        "action": "rewrite",
        "layer": "reply",
        "pattern": "regex:PRIVATE KEY",
        "replacement": "[REDACTED_SECRET]",
        "rationale": "MATS allow presence/risk reporting, not raw values.",
    }
    control.update(overrides)
    return control


def _bulwark_control(**overrides: object) -> dict:
    control = {
        "action": "approval",
        "layer": "fs",
        "match": "/etc/**",
        "condition": "task_family == web_app",
        "exception_path": (
            "Allow with matching host/system trim, matching weapon, or "
            "explicit operator approval."
        ),
    }
    control.update(overrides)
    return control


def _canary_control(**overrides: object) -> dict:
    control = {
        "signal": "private-key-disclosure",
        "pattern": "regex:PRIVATE KEY",
        "severity": "high",
        "trip_when": "agent output includes raw private-key material",
        "response": "record canary hit and alert operator",
    }
    control.update(overrides)
    return control


def _alert_control(**overrides: object) -> dict:
    control = {
        "event": "secret_disclosure_request",
        "severity": "high",
        "message": "Agent approached raw secret disclosure.",
        "route": "courtyard",
    }
    control.update(overrides)
    return control


def _safety_constraints(**overrides: object) -> dict:
    constraints = {
        "surfaces": ["fs", "exec"],
        "allowed": [
            "task workspace",
            "uploaded MATS",
            "browser/proxy artifacts",
            "explicitly declared evidence paths",
        ],
        "restricted": ["/etc/**", "/root/**", "~/.ssh/**", "/dev/**", "/sbin/**"],
        "requires_context": [
            "host_config_trim",
            "system_review_weapon",
            "explicit operator approval",
        ],
        "exception_path": (
            "Allow only when the trim or weapon explicitly declares host/system "
            "review, or when the operator approves the single access."
        ),
    }
    constraints.update(overrides)
    return constraints


def test_temper_mcp_exposes_single_candidate_tool() -> None:
    names = {tool["name"] for tool in temper_mcp_server._TOOLS}

    # One candidate tool (destination is an argument) + summary + the two
    # grounding tools (mats read surface).
    assert names == {
        temper_mcp_server._CANDIDATE_TOOL,
        SUMMARY_TOOL,
        temper_mcp_server._MATS_SEARCH_TOOL,
        temper_mcp_server._MATS_GET_TOOL,
    }
    assert all(not name.startswith("extract.") for name in names)
    assert all(name.startswith("temper.") for name in names)


def test_candidate_tool_schema_requires_review_metadata_and_destination() -> None:
    schema = _tool_schema(temper_mcp_server._CANDIDATE_TOOL)

    assert schema["required"] == [
        "destination",
        "thread_type",
        "title",
        "source",
        "rationale",
        "risk",
        "default_decision",
        "proposed_text",
        "control",
        "citations",
    ]
    assert schema["additionalProperties"] is False
    # destination is now an in-payload enum, not derived from the tool name.
    assert "destination" in schema["properties"]
    assert set(schema["properties"]["destination"]["enum"]) == set(
        EXPECTED_CANDIDATE_TOOLS.values()
    )
    for key in (
        "thread_type", "control", "citations",
        "methodology_context", "safety_constraints",
    ):
        assert key in schema["properties"]


def test_single_candidate_tool_routes_by_destination_arg(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-contract",
    )
    result = temper_mcp_server.call_tool(
        tmp_path,
        temper_mcp_server._CANDIDATE_TOOL,
        _candidate_args(destination="canary", title="Canary via single tool"),
    )
    assert result["ok"] is True
    assert result["candidate"]["destination"] == "canary"


def test_single_candidate_tool_rejects_unknown_destination(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="destination must be one of",
    ):
        temper_mcp_server.call_tool(
            tmp_path,
            temper_mcp_server._CANDIDATE_TOOL,
            _candidate_args(destination="bogus_surface"),
        )


def test_candidate_tool_rejects_wrong_control_shape_for_destination(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")

    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="replacement",
    ):
        temper_mcp_server.call_tool(
            tmp_path,
            "temper.rhema_candidate",
            _candidate_args(
                thread_type="prompt_mediation",
                proposed_text="Rewrite replies to redact private keys.",
                control={
                    "action": "rewrite",
                    "layer": "reply",
                    "pattern": "regex:PRIVATE KEY",
                    "rationale": "MATS require secret redaction.",
                },
            ),
        )

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session["candidates"] == []
    assert len(session["rejected_candidates"]) == 1
    rejected = session["rejected_candidates"][0]
    assert rejected["destination"] == "rhema"
    assert rejected["attempt"]["control"]["action"] == "rewrite"
    assert "replacement" in rejected["error"]
    assert "correct and resubmit" in rejected["correction"]["instruction"]
    assert "control.replacement" in rejected["correction"]["rejected_fields"]
    assert "replacement" in rejected["correction"]["expected_control"]["notes"]


def test_mcp_tool_error_returns_correction_payload_for_resubmission(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    server = temper_mcp_server._Server(tmp_path)
    messages: list[dict] = []
    server._write = messages.append  # type: ignore[method-assign]

    server._tool_call(99, {
        "name": "temper.rhema_candidate",
        "arguments": _candidate_args(
            thread_type="prompt_mediation",
            proposed_text="Rewrite replies to redact private keys.",
            control={
                "action": "rewrite",
                "layer": "reply",
                "pattern": "regex:PRIVATE KEY",
                "rationale": "MATS require secret redaction.",
            },
        ),
    })

    assert len(messages) == 1
    response = messages[0]
    assert "result" in response
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["ok"] is False
    assert result["structuredContent"]["rejected"]["destination"] == "rhema"
    assert "correct and resubmit" in result["content"][0]["text"]


def test_coverage_summary_rejects_malformed_emitted_cells(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    result = temper_mcp_server.call_tool(
        tmp_path,
        "temper.canary_candidate",
        _candidate_args(source="mat"),
    )

    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="requires candidate_ids",
    ):
        temper_mcp_server.call_tool(
            tmp_path,
            SUMMARY_TOOL,
            {
                "sources": [{
                    "source_id": "mat",
                    "minimum_exception_reason": (
                        "regression test focuses on malformed coverage"
                    ),
                    "destinations": {
                        "canary": {
                            "status": "emitted",
                            "id": result["candidate"]["candidate_id"],
                        },
                        "rhema": {"status": "skipped", "reason": "not needed"},
                        "absolute": {"status": "skipped", "reason": "not needed"},
                        "approval_gate": {
                            "status": "skipped",
                            "reason": "not needed",
                        },
                        "alert_hook": {
                            "status": "skipped",
                            "reason": "not needed",
                        },
                        "bulwark": {"status": "skipped", "reason": "not needed"},
                        "trim_template": {
                            "status": "skipped",
                            "reason": "not needed",
                        },
                    },
                }],
            },
        )

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert "coverage_summary" not in session


def test_candidate_tools_map_to_fixed_destinations() -> None:
    for tool_name, destination in EXPECTED_CANDIDATE_TOOLS.items():
        assert temper_mcp_server.tool_destination(tool_name) == destination


def test_candidate_tool_description_covers_every_destination() -> None:
    descriptions = {
        tool["name"]: tool["description"].lower()
        for tool in temper_mcp_server._TOOLS
    }
    candidate = descriptions[temper_mcp_server._CANDIDATE_TOOL]

    # The single tool's description must explain every destination + its cue.
    for destination in EXPECTED_CANDIDATE_TOOLS.values():
        assert destination in candidate
    for cue in ("trip", "rewrite", "approval", "operator", "alert", "enforceable"):
        assert cue in candidate
    assert "coverage" in descriptions[SUMMARY_TOOL]
    assert "skipped" in descriptions[SUMMARY_TOOL]


def test_candidate_tool_stores_pending_candidate_in_tailor_session(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-contract",
    )

    result = temper_mcp_server.call_tool(
        tmp_path,
        "temper.canary_candidate",
        _candidate_args(title="Canary on private key disclosure"),
    )

    assert result["ok"] is True
    assert result["candidate_count"] == 1
    assert result["candidate"]["destination"] == "canary"

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session["status"] == "running"
    assert session["candidates"][0]["destination"] == "canary"
    assert session["candidates"][0]["title"] == "Canary on private key disclosure"
    assert "candidate_id" in session["candidates"][0]
    assert "created_at" in session["candidates"][0]
    assert not (
        tmp_path / "armors" / "hermes-default" / "temper" / "proposals"
    ).exists()


def test_candidate_tool_rejects_missing_citations(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    args = _candidate_args()
    args.pop("citations")

    with pytest.raises(temper_lifecycle.TailorLifecycleError):
        temper_mcp_server.call_tool(
            tmp_path,
            "temper.bulwark_candidate",
            args,
        )

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session.get("candidates", []) == []


def test_candidate_tool_rejects_generic_instruction_ban_without_methodology_context(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")

    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="methodology_context",
    ):
        temper_mcp_server.call_tool(
            tmp_path,
            "temper.absolute_candidate",
            _candidate_args(
                thread_type="methodology_fit",
                title="No Hardware Attack Tool Instructions",
                proposed_text=(
                    "The agent must not output actionable instructions for "
                    "hardware attack tool operation."
                ),
                control=_absolute_control(),
            ),
        )

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session.get("candidates", []) == []


def test_candidate_tool_accepts_methodology_fit_boundary(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")

    result = temper_mcp_server.call_tool(
        tmp_path,
        "temper.bulwark_candidate",
        _candidate_args(
            title="Gate firmware tools during WSTG web-app work",
            thread_type="methodology_fit",
            rationale=(
                "WSTG material describes web application authentication and "
                "transport testing, not firmware extraction."
            ),
            risk=(
                "Agent may drift from web-app testing into firmware or host "
                "administration actions that are not expected for this task family."
            ),
            proposed_text=(
                "approval exec regex:^(binwalk|flashrom|esptool|adb)$ when "
                "active methodology is web_app and no firmware/hardware trim "
                "or weapon is active. approval fs /sbin/** under the same condition."
            ),
            control=_bulwark_control(
                layer="exec",
                match="regex:^(binwalk|flashrom|esptool|adb)$",
            ),
            methodology_context=_methodology_context(),
        ),
    )

    candidate = result["candidate"]
    assert candidate["destination"] == "bulwark"
    assert candidate["methodology_context"]["task_family"] == "web_app"
    assert "binwalk" in candidate["methodology_context"]["unexpected"]


def test_web_app_methodology_rejects_broad_web_testing_restrictions(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")

    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="web_app methodology cannot broadly restrict expected web testing",
    ):
        temper_mcp_server.call_tool(
            tmp_path,
            "temper.absolute_candidate",
            _candidate_args(
                title="Gate browser automation during web-app work",
                thread_type="methodology_fit",
                control=_absolute_control(
                    principle="methodology_fit",
                    allowed=["HTTP request review"],
                    restricted=[
                        "browser automation",
                        "XSS testing frameworks",
                        "web port scanners",
                    ],
                ),
                methodology_context=_methodology_context(
                    unexpected=[
                        "browser automation",
                        "XSS testing frameworks",
                        "web port scanners",
                    ],
                ),
            ),
        )

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session["candidates"] == []
    assert session["rejected_candidates"]


def test_candidate_tool_accepts_safety_constraint_boundary(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")

    result = temper_mcp_server.call_tool(
        tmp_path,
        "temper.bulwark_candidate",
        _candidate_args(
            title="Gate host filesystem access during web-app work",
            thread_type="safety_boundary",
            rationale=(
                "Web-app testing normally needs task files, uploaded MATS, "
                "and browser/proxy artifacts, not host operating-system paths."
            ),
            risk=(
                "Agent may drift into host administration or secret discovery "
                "that is unnecessary for a web-app task."
            ),
            proposed_text=(
                "approval fs /etc/**, /root/**, ~/.ssh/**, /dev/**, and "
                "/sbin/** unless the active trim or weapon explicitly permits "
                "host/system review."
            ),
            control=_bulwark_control(),
            safety_constraints=_safety_constraints(),
        ),
    )

    candidate = result["candidate"]
    assert candidate["destination"] == "bulwark"
    assert candidate["safety_constraints"]["surfaces"] == ["fs", "exec"]
    assert "/etc/**" in candidate["safety_constraints"]["restricted"]


def test_candidate_tool_rejects_path_boundary_without_context(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")

    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="methodology_context or safety_constraints",
    ):
        temper_mcp_server.call_tool(
            tmp_path,
            "temper.bulwark_candidate",
            _candidate_args(
                title="Block host filesystem access",
                thread_type="safety_boundary",
                rationale="Host filesystem reads are unnecessary for this task.",
                risk="Agent may touch unrelated host configuration.",
                proposed_text="deny fs /etc/** and /root/**",
                control=_bulwark_control(action="deny", match="/etc/**"),
            ),
        )

    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert session.get("candidates", []) == []


def test_candidate_tool_requires_running_tailor_session(tmp_path: Path) -> None:
    with pytest.raises(temper_lifecycle.TailorLifecycleError):
        temper_mcp_server.call_tool(
            tmp_path,
            "temper.absolute_candidate",
            _candidate_args(),
        )


def test_coverage_summary_tool_stores_structured_completion(
    tmp_path: Path,
) -> None:
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default"},
        session_id="tailor-contract",
    )
    first = temper_mcp_server.call_tool(
        tmp_path,
        "temper.canary_candidate",
        _candidate_args(title="Canary on private key disclosure"),
    )
    second = temper_mcp_server.call_tool(
        tmp_path,
        "temper.rhema_candidate",
        _candidate_args(
            title="Rhema redaction for private key disclosure",
            thread_type="prompt_mediation",
            control=_rhema_control(),
        ),
    )
    third = temper_mcp_server.call_tool(
        tmp_path,
        "temper.alert_hook_candidate",
        _candidate_args(
            title="Alert on private key disclosure request",
            thread_type="alert_signal",
            control=_alert_control(),
        ),
    )
    ids = [
        first["candidate"]["candidate_id"],
        second["candidate"]["candidate_id"],
        third["candidate"]["candidate_id"],
    ]

    result = temper_mcp_server.call_tool(
        tmp_path,
        SUMMARY_TOOL,
        {
            "sources": [{
                "source_id": "mat_usb_firewall_pentest_v1",
                "minimum_exception_reason": (
                    "contract test uses a focused three-candidate fixture"
                ),
                "destinations": {
                    "canary": {"status": "emitted", "candidate_ids": [ids[0]]},
                    "rhema": {"status": "emitted", "candidate_ids": [ids[1]]},
                    "alert_hook": {"status": "emitted", "candidate_ids": [ids[2]]},
                    "absolute": {"status": "skipped", "reason": "covered by existing doctrine"},
                    "approval_gate": {"status": "skipped", "reason": "no gated action"},
                    "bulwark": {"status": "skipped", "reason": "no runtime boundary"},
                    "trim_template": {"status": "skipped", "reason": "not task framing"},
                },
            }],
            "notes": "One source fully accounted for.",
        },
    )

    assert result["ok"] is True
    assert result["coverage_source_count"] == 1
    session = temper_lifecycle.load_tailor_session(tmp_path)
    assert session is not None
    assert (
        session["coverage_summary"]["sources"][0]["source_id"]
        == "mat_usb_firewall_pentest_v1"
    )


# --- D5: scope + task_family enum constraints (admission) ----------------

def test_candidate_rejected_when_scope_missing(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    args = _candidate_args(destination="canary")
    args.pop("scope")
    with pytest.raises(temper_lifecycle.TailorLifecycleError, match="scope"):
        temper_mcp_server.call_tool(
            tmp_path, temper_mcp_server._CANDIDATE_TOOL, args,
        )


def test_candidate_rejected_for_unknown_scope_tag(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    with pytest.raises(temper_lifecycle.TailorLifecycleError, match="unknown tags"):
        temper_mcp_server.call_tool(
            tmp_path,
            temper_mcp_server._CANDIDATE_TOOL,
            _candidate_args(destination="canary", scope=["mobile_ios"]),
        )


def test_candidate_rejected_for_unknown_task_family(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    with pytest.raises(temper_lifecycle.TailorLifecycleError, match="task_family"):
        temper_mcp_server.call_tool(
            tmp_path,
            temper_mcp_server._CANDIDATE_TOOL,
            _candidate_args(
                destination="bulwark",
                thread_type="methodology_fit",
                control=_bulwark_control(),
                methodology_context=_methodology_context(task_family="investigation_workflow"),
            ),
        )


def test_candidate_rejected_for_dead_rhema_pattern(tmp_path: Path) -> None:
    temper_lifecycle.start_tailor_session(tmp_path, session_id="tailor-contract")
    with pytest.raises(temper_lifecycle.TailorLifecycleError, match="bare label|over-broad"):
        temper_mcp_server.call_tool(
            tmp_path,
            temper_mcp_server._CANDIDATE_TOOL,
            _candidate_args(
                destination="rhema",
                thread_type="prompt_mediation",
                control=_rhema_control(pattern="web_app_config_secrets"),
            ),
        )


# --- D2/D3: grounding (mats tools + verified citations) ------------------

def _seed_source_manifest(tmp_path: Path, forge_id: str, elem_ids: list) -> None:
    from ara.forge.manifest.element import (
        ManifestElement, ManifestKind, write_manifest,
    )
    from ara.forge import state as fstate
    els = [
        ManifestElement(
            id=eid, source_mat_id="mat_x", byte_range=(0, 10),
            sha256="0" * 64, kind=ManifestKind.REPORT_SECTION,
            extracted_text=f"manifest text for {eid} describing secrets handling",
        )
        for eid in elem_ids
    ]
    path = fstate.forge_manifest_path(tmp_path, forge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(path, els)


def test_grounding_mats_tools_serve_source_manifest(tmp_path: Path) -> None:
    _seed_source_manifest(tmp_path, "forge-src", ["elem_aaa", "elem_bbb"])
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default", "source_forge": "forge-src"},
        session_id="tailor-ground",
    )
    res = temper_mcp_server.call_tool(
        tmp_path, temper_mcp_server._MATS_SEARCH_TOOL, {"query": "secrets"},
    )
    assert {r["manifest_element_id"] for r in res["results"]} >= {
        "elem_aaa", "elem_bbb",
    }
    got = temper_mcp_server.call_tool(
        tmp_path, temper_mcp_server._MATS_GET_TOOL,
        {"manifest_element_id": "elem_aaa"},
    )
    assert got["manifest_element_id"] == "elem_aaa"
    assert "elem_aaa" in got["text"]


def test_grounding_accepts_real_citation_rejects_fabricated(tmp_path: Path) -> None:
    _seed_source_manifest(tmp_path, "forge-src", ["elem_aaa", "elem_bbb"])
    temper_lifecycle.start_tailor_session(
        tmp_path,
        source_request={"armor": "hermes-default", "source_forge": "forge-src"},
        session_id="tailor-ground",
    )
    ok = temper_mcp_server.call_tool(
        tmp_path,
        temper_mcp_server._CANDIDATE_TOOL,
        _candidate_args(
            destination="canary",
            citations=[{"manifest_element_id": "elem_aaa"}],
        ),
    )
    assert ok["ok"] is True

    with pytest.raises(
        temper_lifecycle.TailorLifecycleError,
        match="not in the source forge manifest",
    ):
        temper_mcp_server.call_tool(
            tmp_path,
            temper_mcp_server._CANDIDATE_TOOL,
            _candidate_args(
                destination="canary",
                citations=[{"manifest_element_id": "elem_fabricated"}],
            ),
        )


# --- per-destination control sub-schemas (reject-rate lever) --------------

def test_candidate_schema_has_per_destination_control_subschemas() -> None:
    from ara import temper_controls
    schema = temper_mcp_server._CANDIDATE_INPUT_SCHEMA
    branches = schema["allOf"]
    by_dest = {
        b["if"]["properties"]["destination"]["const"]:
            b["then"]["properties"]["control"]
        for b in branches
    }
    # one branch per destination
    assert set(by_dest) == set(EXPECTED_CANDIDATE_TOOLS.values())
    # each control sub-schema's required fields match the authoritative contract
    for dest, control_schema in by_dest.items():
        contract = temper_controls.describe_control_contract(dest)
        assert set(control_schema["required"]) == set(contract["required"])
        # sub-schemas stay permissive so a valid candidate is never blocked
        assert control_schema["additionalProperties"] is True
    # action enums are surfaced for the destinations that have them
    assert set(by_dest["rhema"]["properties"]["action"]["enum"]) == {
        "deny", "approval", "rewrite", "shadow_rewrite",
    }
    assert set(by_dest["bulwark"]["properties"]["action"]["enum"]) == {
        "deny", "approval", "allow",
    }
