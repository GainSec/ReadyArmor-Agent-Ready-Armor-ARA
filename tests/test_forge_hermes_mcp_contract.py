import asyncio

from ara.clients import forge_mcp_server
from ara.forge.loop_driver import _build_prompt
from ara.forge.manifest.element import ManifestElement, ManifestKind
from ara.forge.phases.exhaust import CoverageState


def _tool_schema(name: str) -> dict:
    for tool in forge_mcp_server._TOOLS:
        if tool["name"] == name:
            return tool["inputSchema"]
    raise AssertionError(f"tool {name!r} not found")


def test_task_mcp_mode_advertises_only_engagement_runtime_tools() -> None:
    tools = forge_mcp_server._tools_for_mode({"ARA_FORGE_MCP_MODE": "task"})
    names = {tool["name"] for tool in tools}

    assert names == {
        "fact.assert",
        "lead.file",
        "finding.file",
        "evidence.search",
        "evidence.get",
        "chunks.emit_tag",
    }
    assert "mats.search" not in names
    assert not any(name.startswith("extract.") for name in names)


def test_chunks_emit_tag_tool_description_contains_no_stale_example_tags() -> None:
    tools = forge_mcp_server._tools_for_mode({"ARA_FORGE_MCP_MODE": "task"})
    tool = next(tool for tool in tools if tool["name"] == "chunks.emit_tag")
    desc = tool["description"]

    assert "<go>" not in desc
    assert "<pause>" not in desc
    assert "Valid exit tags" in desc
    assert "rejected" in desc


def test_task_mcp_mode_rejects_forge_authoring_calls() -> None:
    server = forge_mcp_server._Server.__new__(forge_mcp_server._Server)
    server._mode = "task"
    server.client = object()
    server._recent_evidence_refs = []
    writes: list[dict] = []
    server._write = writes.append

    asyncio.run(server._tool_call(1, {
        "name": "mats.search",
        "arguments": {"query": "phase"},
    }))

    assert writes[0]["error"]["code"] == -32601
    assert "not available in task MCP mode" in writes[0]["error"]["message"]


def test_mats_read_tools_advertise_forge_id() -> None:
    for tool_name in ("mats.search", "mats.get"):
        schema = _tool_schema(tool_name)

        assert "forge_id" in schema["properties"]
        assert "Forge session id" in schema["properties"]["forge_id"]["description"]


def test_resource_extract_tool_advertises_weapon_resource_contract() -> None:
    schema = _tool_schema("extract.resource")
    body_desc = schema["properties"]["body"]["description"]

    assert schema["required"] == ["manifest_element_id", "body"]
    assert "resource classes" in body_desc
    assert "reference" in body_desc
    assert "wordlist" in body_desc
    assert "needs_resolution" in body_desc


def test_task_channel_mcp_schemas_match_runtime_required_fields() -> None:
    fact = _tool_schema("fact.assert")
    lead = _tool_schema("lead.file")

    assert {"predicate", "value", "asserted_by_crumb"}.issubset(
        set(fact["required"])
    )
    assert "depends_on" in fact["properties"]
    assert "evidence_refs" in fact["properties"]
    assert {"title", "reasoning"}.issubset(set(lead["required"]))
    assert "evidence_refs" in lead["properties"]
    assert "fact_refs" in lead["properties"]
    assert "not_mechanically_testable" in lead["properties"]
    assert "hypothesis" not in lead["properties"]


def test_evidence_read_tools_are_exposed_to_hermes_mcp() -> None:
    search = _tool_schema("evidence.search")
    get = _tool_schema("evidence.get")

    assert search["required"] == ["query"]
    assert "k" in search["properties"]
    assert get["required"] == ["evidence_id"]
    assert "kb" in get["properties"]


def test_mcp_adapter_carries_recent_evidence_into_fact_and_normalizes_source() -> None:
    recent = ["ev_abc", "ev_def"]

    body = forge_mcp_server._adapt_task_channel_args(
        "fact.assert",
        {
            "predicate": "target.route.exists",
            "value": {"path": "/login", "status": 200},
            "source": "browser_inspection",
        },
        recent,
    )

    assert body["evidence_refs"] == recent
    assert body["asserted_by_crumb"] == "mcp:fact.assert"
    assert body["source"] == "agent_assertion"
    assert "original source=browser_inspection" in body["note"]


def test_mcp_adapter_carries_recent_evidence_into_lead_and_normalizes_origin() -> None:
    body = forge_mcp_server._adapt_task_channel_args(
        "lead.file",
        {
            "title": "Admin route may need auth testing",
            "reasoning": "Observed /admin during target enumeration.",
            "origin": "weapon-initiated",
        },
        ["ev_admin"],
    )

    assert body["evidence_refs"] == ["ev_admin"]
    assert body["origin"] == "agent_hypothesis"
    assert "original origin=weapon-initiated" in body["notes"]


def test_mcp_adapter_synthesizes_lead_title_from_reasoning() -> None:
    body = forge_mcp_server._adapt_task_channel_args(
        "lead.file",
        {
            "reasoning": (
                "Observed Next.js framework headers and static chunks; "
                "follow-up should enumerate exposed routes."
            ),
        },
        ["ev_admin"],
    )

    assert body["title"] == (
        "Observed Next.js framework headers and static chunks; "
        "follow-up should enumerate exposed routes."
    )[:80]
    assert body["reasoning"].startswith("Observed Next.js framework headers")
    assert body["evidence_refs"] == ["ev_admin"]


def test_mcp_evidence_ids_are_remembered_from_search_and_get_results() -> None:
    recent: list[str] = []

    forge_mcp_server._remember_evidence_ids(
        recent,
        {
            "results": [
                {"record": {"id": "ev_one"}},
                {"record": {"id": "ev_two"}},
            ],
        },
    )
    forge_mcp_server._remember_evidence_ids(
        recent,
        {"record": {"id": "ev_three"}},
    )

    assert recent == ["ev_three", "ev_one", "ev_two"]


def test_mcp_server_applies_recent_evidence_to_followup_fact() -> None:
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        async def call(self, name: str, args: dict, timeout: float):
            calls.append((name, args))
            if name == "evidence.search":
                return {"results": [{"record": {"id": "ev_login"}}]}
            return {"ok": True}

    server = forge_mcp_server._Server.__new__(forge_mcp_server._Server)
    server.client = FakeClient()
    server._recent_evidence_refs = []
    writes: list[dict] = []
    server._write = writes.append

    asyncio.run(server._tool_call(1, {
        "name": "evidence.search",
        "arguments": {"query": "login"},
    }))
    asyncio.run(server._tool_call(2, {
        "name": "fact.assert",
        "arguments": {
            "predicate": "target.route.exists",
            "value": "/login",
            "source": "html-analysis",
        },
    }))

    assert calls[1][0] == "fact.assert"
    assert calls[1][1]["evidence_refs"] == ["ev_login"]
    assert calls[1][1]["source"] == "agent_assertion"
    assert calls[1][1]["asserted_by_crumb"] == "mcp:fact.assert"


def test_mcp_server_searches_binder_when_fact_lacks_refs() -> None:
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        async def call(self, name: str, args: dict, timeout: float):
            calls.append((name, args))
            if name == "evidence.search":
                assert "login" in args["query"]
                return {"results": [{"record": {"id": "ev_login"}}]}
            return {"ok": True}

    server = forge_mcp_server._Server.__new__(forge_mcp_server._Server)
    server.client = FakeClient()
    server._recent_evidence_refs = []
    writes: list[dict] = []
    server._write = writes.append

    asyncio.run(server._tool_call(1, {
        "name": "fact.assert",
        "arguments": {
            "predicate": "target.route.status",
            "value": {"path": "/login", "status": 200},
        },
    }))

    assert calls[0][0] == "evidence.search"
    assert calls[1][0] == "fact.assert"
    assert calls[1][1]["evidence_refs"] == ["ev_login"]
    assert calls[1][1]["asserted_by_crumb"] == "mcp:fact.assert"


def test_mcp_server_falls_back_to_latest_session_evidence_when_fact_search_misses(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        async def call(self, name: str, args: dict, timeout: float):
            calls.append((name, args))
            if name == "evidence.search":
                return {"results": []}
            return {"ok": True}

    monkeypatch.setattr(
        forge_mcp_server,
        "_latest_session_evidence_refs",
        lambda limit=3: ["ev_recent"],
    )

    server = forge_mcp_server._Server.__new__(forge_mcp_server._Server)
    server.client = FakeClient()
    server._recent_evidence_refs = []
    writes: list[dict] = []
    server._write = writes.append

    asyncio.run(server._tool_call(1, {
        "name": "fact.assert",
        "arguments": {
            "predicate": "target.route.status",
            "value": {"path": "/", "status": 200},
        },
    }))

    assert calls[0][0] == "evidence.search"
    assert calls[1][0] == "fact.assert"
    assert calls[1][1]["evidence_refs"] == ["ev_recent"]
    assert calls[1][1]["asserted_by_crumb"] == "mcp:fact.assert"


def test_exhaust_prompt_requires_forge_id_on_every_mcp_call() -> None:
    element = ManifestElement(
        id="elem_demo",
        source_mat_id="mat_demo",
        byte_range=(0, 10),
        sha256="0" * 64,
        kind=ManifestKind.REPORT_SECTION,
        extracted_text="Demo source material.",
    )
    prompt = _build_prompt(
        "forge-demo",
        1,
        CoverageState(
            total_elements=1,
            elements_with_extractions=0,
            fraction=0.0,
            pending=["elem_demo"],
        ),
        [element],
        min_extractions=1,
        template=forge_mcp_server_contract_template(),
    )

    assert 'forge_id="forge-demo"' in prompt
    assert "Every forge MCP tool call must include forge_id" in prompt
    assert "extract.resource" in prompt
    assert "grants network access" in prompt


def forge_mcp_server_contract_template() -> str:
    from ara.forge.loop_driver import _DEFAULT_BATCH_PROMPT_TEMPLATE

    return _DEFAULT_BATCH_PROMPT_TEMPLATE
