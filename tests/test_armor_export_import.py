from pathlib import Path
import pytest
from ara import armors as armors_mod


def test_export_roundtrip(short_tmp: Path):
    state = short_tmp / ".ara"
    a = armors_mod.create_armor(state, "orig", description="hello")
    # Add a file to ensure nested content survives.
    (a.root / "overrides.json").write_text('{"overrides":[],"blackholes":[]}')
    blob = armors_mod.export_armor(state, "orig")
    assert len(blob) > 0

    state2 = short_tmp / ".ara-dst"
    armors_mod.import_armor(state2, blob)
    restored = armors_mod.load_armor(state2, "orig")
    assert restored is not None
    assert restored.meta.description == "hello"


def test_import_rename(short_tmp: Path):
    state = short_tmp / ".ara"
    armors_mod.create_armor(state, "src")
    blob = armors_mod.export_armor(state, "src")
    armors_mod.import_armor(state, blob, rename_to="src-copy")
    assert armors_mod.load_armor(state, "src-copy") is not None


def test_import_rejects_existing_without_overwrite(short_tmp: Path):
    state = short_tmp / ".ara"
    armors_mod.create_armor(state, "x")
    blob = armors_mod.export_armor(state, "x")
    with pytest.raises(FileExistsError):
        armors_mod.import_armor(state, blob)
    armors_mod.import_armor(state, blob, overwrite=True)
    assert armors_mod.load_armor(state, "x") is not None


def test_import_rejects_traversal(short_tmp: Path):
    import io, tarfile
    state = short_tmp / ".ara"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="evil/../escape.md")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"pwn\n"))
    with pytest.raises(ValueError):
        armors_mod.import_armor(state, buf.getvalue())


def test_import_rejects_symlink(short_tmp: Path):
    import io, tarfile
    state = short_tmp / ".ara"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="evil/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(ValueError):
        armors_mod.import_armor(state, buf.getvalue())


def test_shadow_mode_persists(short_tmp: Path):
    state = short_tmp / ".ara"
    armors_mod.create_armor(state, "x")
    armors_mod.update_armor_meta(state, "x", shadow_mode=True)
    a = armors_mod.load_armor(state, "x")
    assert a.meta.shadow_mode is True
    # Round-trip via export/import preserves shadow_mode.
    armors_mod.update_armor_meta(state, "x", shadow_mode=True)
    blob = armors_mod.export_armor(state, "x")
    state2 = short_tmp / ".ara2"
    armors_mod.import_armor(state2, blob)
    restored = armors_mod.load_armor(state2, "x")
    assert restored.meta.shadow_mode is True
