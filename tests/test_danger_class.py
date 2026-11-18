"""Tests for the mechanical danger classifier and timeout auto-grant."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ara import danger_class as dc
from ara import approvals as A


# ── classifier: irreversible ────────────────────────────────────────────
@pytest.mark.parametrize("op,msg,pattern", [
    ("browser.nav", {"url": "http://d/cgi-bin/factoryreset"}, "factoryreset"),
    ("rhema.request", {"path": "/cgi-bin/factory-reset"}, "factory+reset"),
    ("rhema.request", {"path": "/dev/disk/format"}, "disk+format"),
    ("rhema.request", {"path": "/dev/nand/erase"}, "nand+erase"),
    ("exec.spawn", {"command": "/sbin/mkfs.ext4"}, "mkfs.ext4"),
])
def test_irreversible(op, msg, pattern):
    f = dc.classify_event(op, msg)
    assert f.tier == dc.IRREVERSIBLE, (op, msg, f.tier)
    assert f.pattern == pattern
    assert not dc.is_auto_grant_eligible(op, msg)


# ── firmware / browsable resources must NOT hard-block ───────────────────
@pytest.mark.parametrize("path", [
    "/admin/firmware/flash", "/firmware/", "/firmware/upgrade",
    "/cgi-bin/sysupgrade", "/firmware/list",
])
def test_firmware_not_irreversible(path):
    assert dc.classify_event(
        "rhema.request", {"path": path}).tier != dc.IRREVERSIBLE


# ── fs reads / enumeration are NEVER classified (reading != triggering) ──
@pytest.mark.parametrize("op,msg", [
    ("fs.read", {"path": "/home/u/factoryreset_notes.txt"}),
    ("fs.readdir", {"path": "/opt/firmware"}),
    ("fs.open", {"path": "/tmp/mkfs.log"}),
    ("fs.stat", {"path": "/data/wipe.sh"}),
])
def test_fs_never_classified(op, msg):
    f = dc.classify_event(op, msg)
    assert f.tier == dc.NONE, (op, msg, f.tier)
    assert dc.is_auto_grant_eligible(op, msg)


# ── flags carry a bulwark-style typed layer + method ────────────────────
def test_flag_is_typed():
    f = dc.classify_event(
        "net.request", {"url": "http://d/cgi-bin/factoryreset",
                        "method": "post"})
    assert f.layer == "net" and f.method == "POST"
    fe = dc.classify_command("/sbin/mkfs")
    assert fe.layer == "exec"


# ── classifier: provable ────────────────────────────────────────────────
@pytest.mark.parametrize("path", [
    "/api/users/42/delete", "/cgi-bin/reboot", "/account/password-reset",
    "/cgi-bin/downfile?f=/etc/passwd", "/svc/disable",
])
def test_provable(path):
    f = dc.classify_event("rhema.request", {"path": path})
    assert f.tier == dc.PROVABLE, (path, f.tier)
    assert not dc.is_auto_grant_eligible("rhema.request", {"path": path})


# ── classifier: none (routine) — must NOT flag ──────────────────────────
@pytest.mark.parametrize("op,msg", [
    ("rhema.request", {"path": "/api/data?format=json"}),
    ("rhema.request", {"path": "/static/reset.css"}),
    ("rhema.request", {"path": "/firmware/update-check"}),
    ("rhema.request", {"path": "/index.html"}),
    ("rhema.request", {"path": "/cgi-bin/getversion"}),
    ("exec.spawn", {"command": "/bin/bash", "args": ["-c", "curl x/factoryreset"]}),
    ("exec.spawn", {"command": "python3", "args": ["-c", "import urllib"]}),
])
def test_routine_is_eligible(op, msg):
    assert dc.classify_event(op, msg).tier == dc.NONE
    assert dc.is_auto_grant_eligible(op, msg)


# ── autogrant config ────────────────────────────────────────────────────
def test_autogrant_default_on(tmp_path: Path):
    assert A.autogrant_enabled(tmp_path) is True
    assert A.autogrant_timeout_s(tmp_path) == A.AUTOGRANT_TIMEOUT_DEFAULT


def test_autogrant_settings_and_env(tmp_path: Path):
    A.write_settings(tmp_path, {"autogrant": False, "autogrant_timeout_s": 9})
    assert A.autogrant_enabled(tmp_path) is False
    assert A.autogrant_timeout_s(tmp_path) == 9.0
    # env override wins over settings
    assert A.autogrant_enabled(
        tmp_path, environ={"ARA_APPROVAL_AUTOGRANT": "on"}) is True
    assert A.autogrant_timeout_s(
        tmp_path, environ={"ARA_APPROVAL_AUTOGRANT_S": "3"}) == 3.0


# ── on_timeout resolution ───────────────────────────────────────────────
def test_timeout_autogrant_allows(tmp_path: Path):
    rec = A.ApprovalRecord(
        id="a1", session_id="s", created_at=time.time(), op="exec.spawn")
    A.write_pending(tmp_path, rec)
    res = A.wait_for_resolution_sync(
        tmp_path, "a1", timeout=0.3, on_timeout="allow")
    assert res.status == "allowed"
    assert res.action == "allow-timeout"


def test_timeout_deny_default(tmp_path: Path):
    rec = A.ApprovalRecord(
        id="a2", session_id="s", created_at=time.time(), op="exec.spawn")
    A.write_pending(tmp_path, rec)
    res = A.wait_for_resolution_sync(tmp_path, "a2", timeout=0.3)
    assert res.status == "timeout"
    assert res.action == "timeout"


# ── registry: record + auto-bulwark + toggle ────────────────────────────
from ara import danger_registry as REG  # noqa: E402


def test_irreversible_auto_bulwark(tmp_path: Path):
    (tmp_path / "bulwark.md").write_text("## global\n")
    url = "http://d/cgi-bin/factoryreset"
    f = dc.classify_url(url)
    rec = REG.flag_and_maybe_block(
        tmp_path, op="net.request", flag=f, target=url, armor_id="a1")
    assert rec["added_to_bulwark"] is True
    bw = (tmp_path / "bulwark.md").read_text()
    assert "## armor: a1" in bw
    assert "deny url d/cgi-bin/factoryreset" in bw
    # idempotent — state stays added, no duplicate line
    REG.flag_and_maybe_block(
        tmp_path, op="net.request", flag=f, target=url, armor_id="a1")
    assert (tmp_path / "bulwark.md").read_text().count(
        "deny url d/cgi-bin/factoryreset") == 1
    assert REG.list_flags(tmp_path)[0]["added_to_bulwark"] is True


def test_provable_recorded_not_blocked(tmp_path: Path):
    (tmp_path / "bulwark.md").write_text("## global\n")
    url = "http://d/api/x/delete"
    rec = REG.flag_and_maybe_block(
        tmp_path, op="net.request", flag=dc.classify_url(url), target=url,
        armor_id="a1")
    assert rec["added_to_bulwark"] is False
    assert "delete" not in (tmp_path / "bulwark.md").read_text()


def test_operator_toggle_add_remove(tmp_path: Path):
    (tmp_path / "bulwark.md").write_text("## global\n")
    url = "http://d/api/x/reboot"
    REG.flag_and_maybe_block(
        tmp_path, op="net.request", flag=dc.classify_url(url), target=url,
        armor_id="a1")  # provable -> not in bulwark
    assert REG.set_bulwark(
        tmp_path, op="net.request", target=url, add=True, armor_id="a1") is True
    assert "reboot" in (tmp_path / "bulwark.md").read_text()
    assert REG.set_bulwark(
        tmp_path, op="net.request", target=url, add=False,
        armor_id="a1") is False
    assert "reboot" not in (tmp_path / "bulwark.md").read_text()
