from ara.clients import forge_mcp_server as fms


def test_pack_mode_exposes_only_pack_tools():
    tools = fms._tools_for_mode({"ARA_FORGE_MCP_MODE": "pack"})
    names = {t["name"] for t in tools}
    assert names == {
        "mats.search", "mats.get", "exhaust.status",
        "propose.alphabet", "extract.conc",
        "propose.binding", "propose.chunk", "propose.detection",
        "extract.bulwark_rule", "extract.rhema_rule",
    }
    # never the weapon knowledge-extraction tools
    assert "extract.proc" not in names and "extract.store" not in names


def test_pack_mode_detected_from_env():
    assert fms._mode_from_env({"ARA_FORGE_MCP_MODE": "pack"}) == "pack"


def test_forge_mode_unchanged_still_has_weapon_tools():
    names = {t["name"] for t in fms._tools_for_mode({})}
    assert "extract.proc" in names and "propose.alphabet" not in names
