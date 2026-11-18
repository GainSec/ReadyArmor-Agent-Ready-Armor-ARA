import json as _json

from ara import hermes_guard as hg


# ---- Task 1: rogue detection ----------------------------------------

def test_contained_launcher_pids_matches_wrapper():
    procs = [
        (100, "/usr/bin/python3.9 -m ara.cli launch --contain --contain-profile hermes-gateway -- hermes gateway run"),
        (200, "/opt/hermes/venv/bin/python -m hermes_cli.main gateway run"),
    ]
    assert hg.contained_launcher_pids(list_procs=lambda: procs) == {100}


def test_find_uncontained_flags_non_descendant(monkeypatch):
    # 100 = ARA launcher; 101 = contained hermes (child of 100); 200 = rogue (child of 1)
    procs = [
        (100, "python -m ara.cli launch --contain --contain-profile hermes-gateway -- hermes gateway run"),
        (101, "/opt/hermes/venv/bin/python -m hermes_cli.main gateway run"),
        (200, "/opt/hermes/venv/bin/python -m hermes_cli.main gateway run"),
    ]
    parents = {101: 100, 200: 1, 100: 1}
    monkeypatch.setattr(hg, "_parent_pid", lambda p: parents.get(p))
    rogue = hg.find_uncontained_hermes(list_procs=lambda: procs)
    assert rogue == [200]


def test_find_uncontained_ignores_launcher_itself():
    # the launcher cmdline contains "hermes gateway run" but must not be flagged
    procs = [
        (100, "python -m ara.cli launch --contain --contain-profile hermes-gateway -- hermes gateway run"),
    ]
    assert hg.find_uncontained_hermes(list_procs=lambda: procs) == []


# ---- Task 2: guard state --------------------------------------------

def test_state_roundtrip(tmp_path):
    st = hg.GuardState(strikes=[10.0, 20.0], held_off=True, held_off_since=20.0, last_detail="x")
    hg.save_state(tmp_path, st)
    got = hg.load_state(tmp_path)
    assert got == st


def test_load_missing_returns_empty(tmp_path):
    st = hg.load_state(tmp_path)
    assert st.strikes == [] and st.held_off is False and st.held_off_since is None


def test_recent_strikes_prunes_old():
    st = hg.GuardState(strikes=[0.0, 100.0, 190.0], held_off=False, held_off_since=None, last_detail="")
    # now=200: within 180s window -> 100.0 and 190.0 count (0.0 is 200s old)
    assert hg.recent_strikes(st, now=200.0) == 2


# ---- Task 3: unit template + re-assertion ---------------------------

def test_render_unit_fills_paths(tmp_path):
    out = hg.render_unit(
        home=str(tmp_path), state_root=str(tmp_path / "ARA/dashboard/.ara"),
        hermes_bin=str(tmp_path / ".local/bin/hermes"),
        pythonpath=str(tmp_path / "source/src"), path_env="/usr/bin:/bin",
    )
    assert "contain-profile hermes-gateway" in out
    assert "ARA_SHIM_FAILOPEN" not in out
    assert f"Environment=PYTHONPATH={tmp_path}/source/src" in out
    assert out.startswith("[Unit]")


def test_ensure_unit_writes_on_drift(tmp_path):
    unit = tmp_path / "hermes-gateway.service"
    unit.write_text("OLD NATIVE UNIT")
    calls = []
    wrote = hg.ensure_contained_unit(unit, "NEW CONTAINED", reload=lambda: calls.append(1))
    assert wrote is True and unit.read_text() == "NEW CONTAINED" and calls == [1]


def test_ensure_unit_noop_when_correct(tmp_path):
    unit = tmp_path / "hermes-gateway.service"
    unit.write_text("SAME")
    calls = []
    wrote = hg.ensure_contained_unit(unit, "SAME", reload=lambda: calls.append(1))
    assert wrote is False and calls == []


# ---- Task 4: lifecycle-faithful restart -----------------------------

def test_write_restart_marker_shape(tmp_path):
    home = tmp_path / ".hermes"; home.mkdir()
    hg.write_restart_marker(home, now=123.0)
    data = _json.loads((home / ".restart_notify.json").read_text())
    assert data["via_service"] is True and data["requested_at"] == 123.0


def test_restart_contained_marker_then_restart(tmp_path):
    home = tmp_path / ".hermes"; home.mkdir()
    calls = []
    hg.restart_contained(home, systemctl=lambda action: calls.append(action), now=5.0)
    assert (home / ".restart_notify.json").exists()
    assert calls == ["restart"]


# ---- Task 5: reconcile circuit-breaker ------------------------------

def _mk(procs):
    return lambda: procs

ROGUE = [(200, "/venv/python -m hermes_cli.main gateway run")]


def test_reconcile_no_rogue_is_none(tmp_path):
    r = hg.reconcile(tmp_path, now=1.0, list_procs=_mk([]),
                     kill=lambda p: None, relaunch=lambda: None, alert=lambda d: None)
    assert r.action == "none" and r.rogue_pids == []


def test_reconcile_first_rogue_remediates(tmp_path, monkeypatch):
    monkeypatch.setattr(hg, "_parent_pid", lambda p: 1)
    killed, relaunched = [], []
    r = hg.reconcile(tmp_path, now=10.0, list_procs=_mk(ROGUE),
                     kill=killed.append, relaunch=lambda: relaunched.append(1),
                     alert=lambda d: None)
    assert r.action == "remediated" and killed == [200] and relaunched == [1]
    assert hg.load_state(tmp_path).held_off is False


def test_reconcile_second_rogue_within_window_holds_off(tmp_path, monkeypatch):
    monkeypatch.setattr(hg, "_parent_pid", lambda p: 1)
    hg.reconcile(tmp_path, now=10.0, list_procs=_mk(ROGUE),
                 kill=lambda p: None, relaunch=lambda: None, alert=lambda d: None)
    killed, relaunched, alerted = [], [], []
    r = hg.reconcile(tmp_path, now=100.0, list_procs=_mk(ROGUE),  # 90s later < 180s
                     kill=killed.append, relaunch=lambda: relaunched.append(1),
                     alert=alerted.append)
    assert r.action == "held_off" and r.held_off is True
    assert killed == [200] and relaunched == []  # killed but NOT relaunched
    assert len(alerted) == 1
    assert hg.load_state(tmp_path).held_off is True


def test_reconcile_when_held_off_keeps_killing_not_relaunching(tmp_path, monkeypatch):
    monkeypatch.setattr(hg, "_parent_pid", lambda p: 1)
    st = hg.GuardState(strikes=[10.0, 100.0], held_off=True, held_off_since=100.0, last_detail="")
    hg.save_state(tmp_path, st)
    killed, relaunched = [], []
    r = hg.reconcile(tmp_path, now=500.0, list_procs=_mk(ROGUE),
                     kill=killed.append, relaunch=lambda: relaunched.append(1),
                     alert=lambda d: None)
    assert r.action == "held_off" and killed == [200] and relaunched == []


def test_reconcile_strike_expires_after_window(tmp_path, monkeypatch):
    monkeypatch.setattr(hg, "_parent_pid", lambda p: 1)
    hg.reconcile(tmp_path, now=10.0, list_procs=_mk(ROGUE),
                 kill=lambda p: None, relaunch=lambda: None, alert=lambda d: None)
    r = hg.reconcile(tmp_path, now=300.0, list_procs=_mk(ROGUE),  # 290s later > 180s
                     kill=lambda p: None, relaunch=lambda: None, alert=lambda d: None)
    assert r.action == "remediated"  # first strike expired -> treated as fresh


# ---- Task 6: bring_up_contained + sig_kill --------------------------

def test_bring_up_clears_held_off_and_reasserts(tmp_path):
    sr = tmp_path / "state"; sr.mkdir()
    hg.save_state(sr, hg.GuardState(strikes=[1.0], held_off=True, held_off_since=1.0, last_detail="x"))
    unit = tmp_path / "unit.service"
    home = tmp_path / ".hermes"; home.mkdir()
    paths = {"home": str(tmp_path), "state_root": str(sr),
             "hermes_bin": "/bin/hermes", "pythonpath": "/p", "path_env": "/usr/bin",
             "unit_path": unit, "hermes_home": home}
    actions = []
    hg.bring_up_contained(now=9.0, paths=paths, reload=lambda: None,
                          systemctl=lambda a: actions.append(a))
    assert hg.load_state(sr).held_off is False
    assert unit.exists() and "contain-profile hermes-gateway" in unit.read_text()
    assert actions == ["restart"]


def test_sig_kill_swallows_missing():
    hg.sig_kill(2_000_000_000)  # nonexistent pid -> no raise


def test_reassert_unit_corrects_native_drift(tmp_path):
    # hermes rewrote the unit to native; reassert must flip it back to contained
    unit = tmp_path / "hermes-gateway.service"
    unit.write_text("ExecStart=/venv/python -m hermes_cli.main gateway run\n")
    home = tmp_path / ".hermes"; home.mkdir()
    paths = {"home": str(tmp_path), "state_root": str(tmp_path),
             "hermes_bin": "/bin/hermes", "pythonpath": "/p", "path_env": "/usr/bin",
             "unit_path": unit, "hermes_home": home}
    reloaded = []
    changed = hg.reassert_contained_unit(paths, reload=lambda: reloaded.append(1))
    assert changed is True
    assert "contain-profile hermes-gateway" in unit.read_text()
    assert reloaded == [1]
    # second call: already contained -> no-op
    assert hg.reassert_contained_unit(paths, reload=lambda: reloaded.append(1)) is False


# ---- Task 7: alert surface ------------------------------------------

def test_raise_alert_writes_event(tmp_path):
    from ara import hermes_guard_alert as al
    al.raise_alert(tmp_path, "rogue held off")
    evs = al.guard_events(tmp_path)
    assert evs and evs[-1]["detail"] == "rogue held off"
    assert evs[-1]["kind"] == "hermes_guard_holdoff"


# ---- Task 8: Courtyard signal card ----------------------------------

def test_guard_card_states(tmp_path):
    from ara.armory import views
    hg.save_state(tmp_path, hg.GuardState(strikes=[1.0, 2.0], held_off=True,
                                          held_off_since=2.0, last_detail="d"))
    html = views.render_hermes_guard_card(tmp_path)
    assert "HELD OFF" in html
    sr2 = tmp_path / "clean"; sr2.mkdir()
    html2 = views.render_hermes_guard_card(sr2)
    assert "contained" in html2.lower()


# ---- Task 9: periodic guard thread ----------------------------------

def test_guard_loop_calls_run_then_stops():
    from ara.armory import app
    calls = []
    stop = {"n": 0}
    def should_stop():
        stop["n"] += 1
        return stop["n"] > 2   # allow ~2 iterations
    app.start_hermes_guard_thread(
        "/sr", interval_s=0, run=lambda now: calls.append(now),
        sleep=lambda s: None, should_stop=should_stop, join=True)
    assert len(calls) >= 1


def test_default_paths_use_current_home_and_installed_source(tmp_path, monkeypatch):
    for name in ("ARA_ROOT", "ARA_STATE_ROOT", "HERMES_BIN", "ARA_HERMES_UNIT_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/thin/armory/path")

    paths = hg.default_paths()

    assert paths["home"] == str(tmp_path)
    assert paths["pythonpath"] == str(hg.Path(hg.__file__).resolve().parents[1])
    assert paths["hermes_bin"] == str(tmp_path / ".local/bin/hermes")
    assert paths["unit_path"] == tmp_path / ".config/systemd/user/hermes-gateway.service"
    assert paths["hermes_home"] == tmp_path / ".hermes"
    assert paths["state_root"] == str(tmp_path / "ARA/dashboard/.ara")
    assert str(tmp_path / ".local/bin") in paths["path_env"].split(":")
    assert str(tmp_path / ".npm-global/bin") in paths["path_env"].split(":")
    assert "/thin/armory/path" not in paths["path_env"]


def test_default_paths_expand_home_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARA_ROOT", "~/source/ara")
    monkeypatch.setenv("ARA_STATE_ROOT", "~/state")
    monkeypatch.setenv("HERMES_BIN", "~/tools/hermes")
    monkeypatch.setenv("ARA_HERMES_UNIT_PATH", "/custom/bin:/usr/bin")

    paths = hg.default_paths()

    assert paths["pythonpath"] == str(tmp_path / "source/ara/src")
    assert paths["state_root"] == str(tmp_path / "state")
    assert paths["hermes_bin"] == str(tmp_path / "tools/hermes")
    assert paths["path_env"] == "/custom/bin:/usr/bin"


def test_default_paths_without_home_uses_platform_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("ARA_ROOT", raising=False)
    monkeypatch.setattr(hg.Path, "home", lambda: tmp_path)

    assert hg.default_paths()["home"] == str(tmp_path)


def test_default_paths_locate_installed_package_source(tmp_path, monkeypatch):
    monkeypatch.delenv("ARA_ROOT", raising=False)
    package_root = tmp_path / "site-packages"
    monkeypatch.setattr(hg, "__file__", str(package_root / "ara/hermes_guard.py"))

    assert hg.default_paths()["pythonpath"] == str(package_root)
