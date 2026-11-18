from pathlib import Path
from types import SimpleNamespace

from ara.forge import cli
from ara.forge.session import ForgeSession
from ara.forge.alphabets import pack as pack_mod


def test_forge_new_kind_pack_creates_pack_session(tmp_path: Path, capsys) -> None:
    args = SimpleNamespace(state_root=str(tmp_path), weapon_target_id="mydom",
                           kind="pack", predecessor=None, engagement_id=None)
    assert cli._cmd_new(args) == 0
    out = capsys.readouterr().out
    import json
    fid = json.loads(out)["forge_id"]
    sess = ForgeSession.load(tmp_path, fid)
    assert sess.config.kind == "pack"


def test_pack_promote_copies_to_builtin(tmp_path: Path, monkeypatch, capsys) -> None:
    # a runtime pack exists
    rp = pack_mod.runtime_packs_dir(tmp_path)
    rp.mkdir(parents=True)
    pack_mod.write_pack(rp / "mydom.json", pack_mod.load_builtin_pack("offsec"))
    # redirect the built-in dir to a temp so we don't touch the repo
    fake_builtin = tmp_path / "builtin_packs"
    fake_builtin.mkdir()
    monkeypatch.setattr(pack_mod, "_PACKS_DIR", fake_builtin)
    args = SimpleNamespace(state_root=str(tmp_path), name="mydom")
    assert cli._cmd_pack_promote(args) == 0
    assert (fake_builtin / "mydom.json").exists()


def test_pack_list_shows_both(tmp_path: Path, capsys) -> None:
    rp = pack_mod.runtime_packs_dir(tmp_path)
    rp.mkdir(parents=True)
    pack_mod.write_pack(rp / "mydom.json", pack_mod.load_builtin_pack("offsec"))
    args = SimpleNamespace(state_root=str(tmp_path))
    assert cli._cmd_pack_list(args) == 0
    import json
    rows = json.loads(capsys.readouterr().out)
    names = {r["name"]: r for r in rows}
    assert names["mydom"]["runtime"] is True
    assert names["offsec"]["builtin"] is True
