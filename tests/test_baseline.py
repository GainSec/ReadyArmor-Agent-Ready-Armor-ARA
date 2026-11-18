import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")

import json
from pathlib import Path
from ara import baseline as baseline_mod


def _seed_session(root: Path, sid: str, policy_hits: list[dict], events: list[dict] | None = None) -> Path:
    sd = root / "sessions" / sid
    sd.mkdir(parents=True)
    (sd / "policy_hits.jsonl").write_text("".join(json.dumps(r) + "\n" for r in policy_hits))
    if events:
        (sd / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in events))
    return sd


def test_record_and_load(short_tmp: Path):
    state = short_tmp / ".ara"
    s = _seed_session(state, "s1", [
        {"op": "fs.open", "path": "/etc/hosts", "verdict": "allow"},
        {"op": "fs.open", "path": "/usr/lib/x.so", "verdict": "allow"},
        {"op": "exec.spawn", "command": "git", "verdict": "allow"},
        {"op": "net.connect", "host": "api.example.com", "port": 443, "verdict": "allow"},
    ])
    b = baseline_mod.record_from_session(state, "clean-run", s)
    assert set(b.paths) == {"/etc/hosts", "/usr/lib/x.so"}
    assert set(b.commands) == {"git"}
    assert set(b.hosts) == {"api.example.com:443"}

    loaded = baseline_mod.load(state, "clean-run")
    assert loaded.source_session == "s1"
    assert sorted(loaded.paths) == ["/etc/hosts", "/usr/lib/x.so"]


def test_diff_shows_deviations(short_tmp: Path):
    state = short_tmp / ".ara"
    baseline_src = _seed_session(state, "sA", [
        {"op": "fs.open", "path": "/etc/hosts", "verdict": "allow"},
        {"op": "exec.spawn", "command": "git", "verdict": "allow"},
    ])
    baseline_mod.record_from_session(state, "base", baseline_src)

    suspicious = _seed_session(state, "sB", [
        {"op": "fs.open", "path": "/etc/hosts", "verdict": "allow"},
        {"op": "fs.open", "path": "/home/u/.ssh/id_rsa", "verdict": "deny"},
        {"op": "exec.spawn", "command": "curl", "verdict": "deny"},
        {"op": "net.connect", "host": "evil.example.com", "port": 443, "verdict": "deny"},
    ])
    d = baseline_mod.diff_against_session(baseline_mod.load(state, "base"), suspicious)
    assert d.new_paths == ["/home/u/.ssh/id_rsa"]
    assert d.new_commands == ["curl"]
    assert d.new_hosts == ["evil.example.com:443"]


def test_clean_run_has_no_diff(short_tmp: Path):
    state = short_tmp / ".ara"
    s = _seed_session(state, "s1", [
        {"op": "fs.open", "path": "/etc/hosts", "verdict": "allow"},
    ])
    baseline_mod.record_from_session(state, "b", s)
    s2 = _seed_session(state, "s2", [
        {"op": "fs.open", "path": "/etc/hosts", "verdict": "allow"},
    ])
    d = baseline_mod.diff_against_session(baseline_mod.load(state, "b"), s2)
    assert d.total == 0


def test_invalid_name_rejected(short_tmp: Path):
    import pytest
    with pytest.raises(ValueError):
        baseline_mod.path_for(short_tmp / ".ara", "../evil")


def test_to_armor_creates_loadout(short_tmp: Path):
    import json as _json
    from ara import armors as armors_mod, overrides as overrides_mod
    state = short_tmp / ".ara"
    s = _seed_session(state, "s1", [
        {"op": "fs.open", "path": "/etc/hosts", "verdict": "allow"},
        {"op": "exec.spawn", "command": "git", "verdict": "allow"},
        {"op": "net.connect", "host": "api.github.com", "port": 443, "verdict": "allow"},
    ])
    baseline_mod.record_from_session(state, "clean", s)

    summary = baseline_mod.to_armor(state, "clean", "clean-armor")
    assert summary["armor"] == "clean-armor"
    assert summary["overrides_count"] == 3

    # Armor loads fine.
    a = armors_mod.load_armor(state, "clean-armor")
    assert a is not None
    assert a.meta.tags == ["baseline:clean"]

    # WALLS has the three approval sections only.
    walls_txt = (a.root / "WALLS.md").read_text()
    assert "approval_path" in walls_txt
    assert "approval_command" in walls_txt
    assert "approval_net" in walls_txt
    assert "deny_path" not in walls_txt

    # Overrides hold every baseline resource.
    store = overrides_mod.load_at(a.overrides_path)
    patterns = {e.pattern for e in store.overrides}
    assert "/etc/hosts" in patterns
    assert "git" in patterns
    assert "api.github.com:443" in patterns


def test_to_armor_rejects_existing_without_overwrite(short_tmp: Path):
    import pytest
    state = short_tmp / ".ara"
    s = _seed_session(state, "s1", [
        {"op": "fs.open", "path": "/etc/hosts", "verdict": "allow"},
    ])
    baseline_mod.record_from_session(state, "b", s)
    baseline_mod.to_armor(state, "b", "derived")
    with pytest.raises(FileExistsError):
        baseline_mod.to_armor(state, "b", "derived")
    # Overwrite works.
    baseline_mod.to_armor(state, "b", "derived", overwrite=True)
