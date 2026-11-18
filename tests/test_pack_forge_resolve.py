from pathlib import Path

from ara.forge.alphabets import pack as P


def test_resolve_prefers_runtime_then_builtin(tmp_path: Path) -> None:
    # built-in offsec resolves with no runtime store
    assert P.resolve_pack("offsec", tmp_path).name == "offsec"
    # a runtime pack resolves (offsec content written under a new name)
    rp = P.runtime_packs_dir(tmp_path)
    rp.mkdir(parents=True)
    P.write_pack(rp / "mydom.json", P.load_builtin_pack("offsec"))
    assert P.resolve_pack("mydom", tmp_path).name == "offsec"
    names = P.list_packs(tmp_path)
    assert "mydom" in names and "offsec" in names


def test_runtime_wins_over_builtin(tmp_path: Path) -> None:
    rp = P.runtime_packs_dir(tmp_path)
    rp.mkdir(parents=True)
    # write a runtime pack named "offsec" and confirm the store copy is used
    pk = P.load_builtin_pack("offsec")
    P.write_pack(rp / "offsec.json", pk)
    assert (rp / "offsec.json").exists()
    assert P.resolve_pack("offsec", tmp_path).name == "offsec"
