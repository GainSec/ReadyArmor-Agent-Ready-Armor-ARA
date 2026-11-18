from pathlib import Path

ABS = Path("src/ara/seed/armors/codex/absolutes/default.md").read_text()
BULWARK = Path("src/ara/seed/weapons/pen/policy/bulwark.md").read_text()


def test_codex_absolutes_are_pack_not_weapon():
    assert "alphabet pack" in ABS.lower()
    assert "propose.alphabet" in ABS
    # all four alphabet shapes are taught
    for _s in ("categorical", "matrix", "template_set", "vector"):
        assert _s in ABS
    # the stale blacksmith/weapon language is gone
    assert "forge a new weapon" not in ABS.lower()
    assert "extract.proc" not in ABS


def test_pen_bulwark_allows_pack_draft():
    assert "pack_draft.json" in BULWARK
