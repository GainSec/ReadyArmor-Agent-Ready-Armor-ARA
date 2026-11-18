from pathlib import Path

from ara.forge.session import ForgeSession, ForgeConfig, ForgeKind, ForgePhase
from ara.forge.phases import synthesize, seal as seal_mod
from ara.forge.pack_draft import PackDraft
from ara.forge.alphabets import pack as pack_mod


def _pack_session(tmp_path: Path, draft: PackDraft) -> ForgeSession:
    s = ForgeSession.new(tmp_path, weapon_target_id=draft.name, operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    synthesize.save_pack_draft(tmp_path, s.forge_id, draft)
    s.phase = ForgePhase.SEAL
    return s


def _valid_draft(name="tinydom") -> PackDraft:
    d = PackDraft(name=name)
    d.add_alphabet({"name": "category", "shape": "categorical",
                    "fields": [{"name": "id", "type": "str"}]})
    d.add_entry("category", {"id": "sqli"})
    d.add_binding({"field": "category", "rule": "FIELD_IN_ALPHABET", "alphabet": "category"})
    return d


def test_seal_pack_writes_valid_pack(tmp_path: Path) -> None:
    s = _pack_session(tmp_path, _valid_draft())
    res = seal_mod.seal_pack(s)
    assert res.ok, res.failures
    out = pack_mod.runtime_packs_dir(tmp_path) / "tinydom.json"
    assert out.exists()
    # reloadable + resolvable
    assert pack_mod.load_pack(out).get("category") is not None
    assert pack_mod.resolve_pack("tinydom", tmp_path).name == "tinydom"
    assert s.phase == ForgePhase.SEALED


def test_seal_pack_rejects_dangling_binding(tmp_path: Path) -> None:
    d = _valid_draft("bad")
    d.add_binding({"field": "sev", "rule": "FIELD_IN_MATRIX", "alphabet": "nope"})
    s = _pack_session(tmp_path, d)
    res = seal_mod.seal_pack(s)
    assert not res.ok
    assert any("nope" in f for f in res.failures)
    assert not (pack_mod.runtime_packs_dir(tmp_path) / "bad.json").exists()


def test_seal_pack_rejects_empty_alphabet(tmp_path: Path) -> None:
    d = PackDraft(name="empt")
    d.add_alphabet({"name": "category", "shape": "categorical",
                    "fields": [{"name": "id", "type": "str"}]})  # no entries
    s = _pack_session(tmp_path, d)
    res = seal_mod.seal_pack(s)
    assert not res.ok
    # force allows it
    s2 = _pack_session(tmp_path, d)
    assert seal_mod.seal_pack(s2, force=True).ok
