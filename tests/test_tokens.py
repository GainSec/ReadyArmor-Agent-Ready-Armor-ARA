"""Tests for ara.tokens — the approval-token primitive (BRA-derived)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ara import tokens as tokens_mod
from ara.runtime import audit as raudit


@pytest.fixture(autouse=True)
def _reset_state():
    tokens_mod._reset_for_tests()
    raudit.reset_registry_for_tests()
    yield
    tokens_mod._reset_for_tests()
    raudit.reset_registry_for_tests()


# ---------- name validation ----------

def test_valid_names_accepted() -> None:
    for name in (
        "ENGAGEMENT-AUTHORIZED",
        "TRIM-CONFIRMED",
        "SCOPE-GRANT:billing.acme.com",
        "TRIM-CONFIRMED:eng-2026-04",
        "MY-CUSTOM-TOKEN",
        "X1",
        "X-Y-Z:a.b.c",
    ):
        assert tokens_mod._validate_name(name) == name


def test_invalid_names_rejected() -> None:
    for bad in (
        "lowercase-token",
        "Mixed-Case",
        "WITH SPACE",
        "trailing-",
        "1LEADING-DIGIT",
        ":NO-BASE",
        "BASE:",
        "BASE:bad space",
        "",
    ):
        with pytest.raises(tokens_mod.InvalidTokenName):
            tokens_mod._validate_name(bad)


def test_qualified_name_split() -> None:
    assert tokens_mod._split_qualified("FOO") == ("FOO", None)
    assert tokens_mod._split_qualified("SCOPE-GRANT:host.example") == \
        ("SCOPE-GRANT", "host.example")


# ---------- registry ----------

def test_builtins_registered_on_first_access(tmp_path: Path) -> None:
    reg = tokens_mod.known_tokens(tmp_path)
    for name in tokens_mod.BUILTIN_TOKENS:
        assert name in reg
    assert reg["SCOPE-GRANT"].one_time is True
    assert reg["ACTIVE-TESTING-APPROVAL"].one_time is False
    assert reg["ACTIVE-TESTING-APPROVAL"].source == "builtin"


def test_register_token_extends_registry() -> None:
    spec = tokens_mod.register_token(
        "MY-CUSTOM", description="test", one_time=False, source="programmatic",
    )
    assert spec.name == "MY-CUSTOM"
    reg = tokens_mod.known_tokens()
    assert reg["MY-CUSTOM"].description == "test"
    assert reg["MY-CUSTOM"].source == "programmatic"


def test_register_token_validates_shape() -> None:
    with pytest.raises(tokens_mod.InvalidTokenName):
        tokens_mod.register_token("lowercase")


# ---------- v0.1 amendment builtins ----------

def test_v01_amendment_builtins_registered(tmp_path: Path) -> None:
    """Spec armor-trim-v0.1-amendment §13 — new builtins are present."""
    reg = tokens_mod.known_tokens(tmp_path)
    expected_v01 = [
        "ENV-PROD", "ENV-NONPROD",
        "PROD-DESTRUCTIVE-APPROVAL", "ACTIVE-PROD-APPROVAL",
        "POSTURE-GRAYBOX", "POSTURE-WHITEBOX",
        "WINDOW-EXTEND", "WINDOW-PIVOT-READONLY",
        "RESUME-AFTER-STOP", "RUN-RECON-DESPITE-KNOWLEDGE",
    ]
    for name in expected_v01:
        assert name in reg, f"v0.1 builtin missing: {name}"


def test_v01_one_time_defaults(tmp_path: Path) -> None:
    """One-time tokens documented at registration time."""
    reg = tokens_mod.known_tokens(tmp_path)
    one_time = {
        "PROD-DESTRUCTIVE-APPROVAL",
        "WINDOW-EXTEND", "WINDOW-PIVOT-READONLY",
        "RESUME-AFTER-STOP", "RUN-RECON-DESPITE-KNOWLEDGE",
        "SCOPE-GRANT",
    }
    for name in one_time:
        assert reg[name].one_time is True, f"{name} should default one_time=True"
    standing = {"ENV-PROD", "ENV-NONPROD", "ACTIVE-PROD-APPROVAL",
                "POSTURE-GRAYBOX", "POSTURE-WHITEBOX"}
    for name in standing:
        assert reg[name].one_time is False, f"{name} should default standing"


def test_v01_targeted_token_sets_per_engagement(tmp_path: Path) -> None:
    """ENV-PROD:<eid> targeted shape works."""
    tokens_mod.set_token(
        "ENV-PROD:acme-portal-2026-04",
        reason="trim apply",
        state_root=tmp_path,
    )
    assert tokens_mod.has_token(
        "ENV-PROD:acme-portal-2026-04", state_root=tmp_path,
    )
    # Different engagement id is independent.
    assert not tokens_mod.has_token(
        "ENV-PROD:other-engagement", state_root=tmp_path,
    )


def test_v01_one_time_consumes_on_require(tmp_path: Path) -> None:
    """PROD-DESTRUCTIVE-APPROVAL is consumed by require_token."""
    tokens_mod.set_token(
        "PROD-DESTRUCTIVE-APPROVAL",
        reason="single state-changing prod op",
        state_root=tmp_path,
    )
    assert tokens_mod.has_token(
        "PROD-DESTRUCTIVE-APPROVAL", state_root=tmp_path,
    )
    tokens_mod.require_token(
        "PROD-DESTRUCTIVE-APPROVAL", state_root=tmp_path,
    )
    # Consumed.
    assert not tokens_mod.has_token(
        "PROD-DESTRUCTIVE-APPROVAL", state_root=tmp_path,
    )


def test_operator_config_extends_registry(tmp_path: Path) -> None:
    (tmp_path / "tokens.json").write_text(json.dumps({
        "DESCRIBED-TOKEN": {"description": "a custom one", "one_time": True},
        "another-bad-name": {"description": "ignored"},
        "PLAIN-TOKEN": {},
    }))
    reg = tokens_mod.known_tokens(tmp_path)
    assert "DESCRIBED-TOKEN" in reg
    assert reg["DESCRIBED-TOKEN"].description == "a custom one"
    assert reg["DESCRIBED-TOKEN"].one_time is True
    assert reg["DESCRIBED-TOKEN"].source == "operator"
    assert "PLAIN-TOKEN" in reg
    assert "another-bad-name" not in reg


def test_operator_config_reloads_on_mtime_change(tmp_path: Path) -> None:
    cfg = tmp_path / "tokens.json"
    cfg.write_text(json.dumps({"FIRST-VERSION": {}}))
    reg = tokens_mod.known_tokens(tmp_path)
    assert "FIRST-VERSION" in reg
    # Bump mtime + change content
    time.sleep(0.05)
    cfg.write_text(json.dumps({"SECOND-VERSION": {}}))
    import os
    os.utime(cfg, None)
    reg2 = tokens_mod.known_tokens(tmp_path)
    assert "SECOND-VERSION" in reg2


# ---------- set / has / unset basics ----------

def test_set_token_writes_record_and_chmods(tmp_path: Path) -> None:
    p = tokens_mod.set_token(
        "ACTIVE-TESTING-APPROVAL",
        reason="test",
        set_by="operator@example.com",
        state_root=tmp_path,
    )
    assert p.exists()
    # mode 0o600
    import stat
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600
    # content round-trips
    rec = json.loads(p.read_text())
    assert rec["name"] == "ACTIVE-TESTING-APPROVAL"
    assert rec["reason"] == "test"
    assert rec["set_by"] == "operator@example.com"
    assert rec["one_time"] is False
    assert rec["target"] is None


def test_has_token_finds_set(tmp_path: Path) -> None:
    assert not tokens_mod.has_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    tokens_mod.set_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    assert tokens_mod.has_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)


def test_unset_returns_true_when_existed(tmp_path: Path) -> None:
    tokens_mod.set_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    assert tokens_mod.unset_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path) is True
    assert not tokens_mod.has_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    # second unset is a no-op returning False
    assert tokens_mod.unset_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path) is False


def test_set_overwrites_existing(tmp_path: Path) -> None:
    tokens_mod.set_token("ACTIVE-TESTING-APPROVAL",
                          reason="first", state_root=tmp_path)
    tokens_mod.set_token("ACTIVE-TESTING-APPROVAL",
                          reason="second", state_root=tmp_path)
    p = tokens_mod.token_path("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    rec = json.loads(p.read_text())
    assert rec["reason"] == "second"


# ---------- one-time consumption ----------

def test_one_time_token_consumed_on_require(tmp_path: Path) -> None:
    tokens_mod.set_token(
        "SCOPE-GRANT:host.example", one_time=True, state_root=tmp_path,
    )
    assert tokens_mod.has_token("SCOPE-GRANT:host.example", state_root=tmp_path)
    assert tokens_mod.require_token("SCOPE-GRANT:host.example", state_root=tmp_path) is True
    # consumed on first require
    assert not tokens_mod.has_token("SCOPE-GRANT:host.example", state_root=tmp_path)
    # second require returns False
    assert tokens_mod.require_token("SCOPE-GRANT:host.example", state_root=tmp_path) is False


def test_persistent_token_not_consumed(tmp_path: Path) -> None:
    tokens_mod.set_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    assert tokens_mod.require_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path) is True
    assert tokens_mod.require_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path) is True
    assert tokens_mod.has_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)


def test_issue_one_time_token_helper(tmp_path: Path) -> None:
    p = tokens_mod.issue_one_time_token(
        "SCOPE-GRANT", target="api.example.com", state_root=tmp_path,
    )
    assert p.name == "SCOPE-GRANT__api.example.com"
    assert tokens_mod.has_token(
        "SCOPE-GRANT:api.example.com", state_root=tmp_path,
    )


# ---------- expiry ----------

def test_expired_token_not_present(tmp_path: Path) -> None:
    past = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    tokens_mod.set_token(
        "ACTIVE-TESTING-APPROVAL", expires_at=past, state_root=tmp_path,
    )
    assert not tokens_mod.has_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    # has_token auto-cleans
    p = tokens_mod.token_path("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    assert not p.exists()


def test_unexpired_token_present(tmp_path: Path) -> None:
    future = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    tokens_mod.set_token(
        "ACTIVE-TESTING-APPROVAL", expires_at=future, state_root=tmp_path,
    )
    assert tokens_mod.has_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)


def test_malformed_expiry_string_does_not_expire(tmp_path: Path) -> None:
    # Bad ISO8601 → parser falls through without flagging expired
    tokens_mod.set_token(
        "ACTIVE-TESTING-APPROVAL", expires_at="not-a-date", state_root=tmp_path,
    )
    assert tokens_mod.has_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)


# ---------- list_tokens ----------

def test_list_tokens_shows_set_state(tmp_path: Path) -> None:
    tokens_mod.set_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    tokens_mod.issue_one_time_token(
        "SCOPE-GRANT", target="x.example", state_root=tmp_path,
    )
    listing = tokens_mod.list_tokens(tmp_path)
    assert listing["ACTIVE-TESTING-APPROVAL"] is True
    assert listing["DESTRUCTIVE-APPROVAL"] is False
    assert listing["SCOPE-GRANT:x.example"] is True


# ---------- audit log ----------

def test_audit_log_grows_per_op(tmp_path: Path) -> None:
    tokens_mod.set_token(
        "ACTIVE-TESTING-APPROVAL",
        reason="step 4 yes", set_by="op",
        state_root=tmp_path,
    )
    tokens_mod.require_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    tokens_mod.unset_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    log = tmp_path / "tokens.audit.jsonl"
    assert log.exists()
    lines = [
        json.loads(line) for line in log.read_text().splitlines() if line.strip()
    ]
    kinds = [r["kind"] for r in lines]
    assert kinds == ["token_set", "token_required", "token_unset"]
    body = lines[0]["body"]
    assert body["token"] == "ACTIVE-TESTING-APPROVAL"
    assert body["reason"] == "step 4 yes"
    assert body["set_by"] == "op"
    # require body records consumption flag
    req_body = lines[1]["body"]
    assert req_body["consumed"] is False           # persistent token, no consume
    assert req_body["present"] is True


def test_audit_chain_is_hash_chained(tmp_path: Path) -> None:
    tokens_mod.set_token("ACTIVE-TESTING-APPROVAL", state_root=tmp_path)
    tokens_mod.set_token("DESTRUCTIVE-APPROVAL", state_root=tmp_path)
    log = tmp_path / "tokens.audit.jsonl"
    ok, lineno, err = raudit.verify_chain(log)
    assert ok is True, f"chain broken at line {lineno}: {err}"
    assert lineno == 2


# ---------- targeted tokens ----------

def test_set_token_records_target_for_qualified_name(tmp_path: Path) -> None:
    tokens_mod.set_token(
        "SCOPE-GRANT:billing.acme.com", state_root=tmp_path,
    )
    p = tokens_mod.token_path("SCOPE-GRANT:billing.acme.com", state_root=tmp_path)
    assert "SCOPE-GRANT__billing.acme.com" in p.name
    rec = json.loads(p.read_text())
    assert rec["target"] == "billing.acme.com"


def test_one_time_default_inherited_from_builtin(tmp_path: Path) -> None:
    # SCOPE-GRANT default is one_time=True per BUILTIN
    tokens_mod.set_token("SCOPE-GRANT:host.example", state_root=tmp_path)
    p = tokens_mod.token_path("SCOPE-GRANT:host.example", state_root=tmp_path)
    rec = json.loads(p.read_text())
    assert rec["one_time"] is True
