import subprocess

from ara.mitm_ca import CA


def test_sign_leaf_regenerates_expired_cached_cert(tmp_path, monkeypatch):
    ca = CA(tmp_path)
    ca.ensure()
    key, crt = ca.leaf_paths("model.example.test")
    key.write_text("stale key")
    crt.write_text("stale cert")

    generated = []

    def fake_run(cmd, check=False, capture_output=False):
        if cmd[:3] == ["openssl", "x509", "-checkend"]:
            return subprocess.CompletedProcess(cmd, 1)
        if cmd[:2] == ["openssl", "genrsa"]:
            generated.append("key")
            key.write_text("fresh key")
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["openssl", "req", "-new"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["openssl", "x509", "-req"]:
            generated.append("cert")
            crt.write_text("fresh cert")
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected openssl command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    returned_key, returned_crt = ca.sign_leaf("model.example.test")

    assert returned_key == key
    assert returned_crt == crt
    assert generated == ["key", "cert"]
    assert key.read_text() == "fresh key"
    assert crt.read_text() == "fresh cert"
