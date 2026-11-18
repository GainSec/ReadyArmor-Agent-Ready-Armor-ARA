"""Tests for ``ara.trims`` — engagement-scoped armor overlays.

Per ``docs/specs/armor-trim-v0.md`` (Q-approved 2026-04-26). Covers:

- TrimMeta round-trip (to_dict/from_dict).
- ``new_trim`` / ``load_trim`` / ``list_trims`` / ``list_all_trims``.
- ``_validate_trim_id`` accepts/rejects expected shapes.
- Marker strip + wrap is line-start anchored and prose-mention safe.
- ``apply_trim`` / ``release_trim`` round-trip preserves operator
  content above and below the markers byte-for-byte.
- Single-trim-per-armor enforcement (Q2 v0).
- ``derive_trim`` is deterministic and produces sensible
  bulwark-section + absolute-trim from an engagement dict.
- TTL injection respects min(operator, engagement) per Q11.
- ``release_active_trim_for_armor`` hook honors
  ``release_on_armor_unequip``.
- TRIM-CONFIRMED token is set/unset on apply/release.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ara import armors as armors_mod
from ara import tokens as tokens_mod
from ara import trims as trims_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_armor(state: Path, name: str = "blacksmith") -> None:
    armors_mod.create_armor(state, name)


def _iso_in(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Dataclass + validation
# ---------------------------------------------------------------------------

def test_trim_meta_roundtrip() -> None:
    meta = trims_mod.TrimMeta(
        trim_id="acme-2026-04",
        armor="blacksmith",
        target="https://portal.acme.com",
        channels=["http", "tls"],
        declared_tokens=["ACTIVE-TESTING-APPROVAL"],
        operator_prefs={"approve_high_risk": True},
    )
    rt = trims_mod.TrimMeta.from_dict(meta.to_dict())
    assert rt == meta


# ---------------------------------------------------------------------------
# v0.1 schema fields
# ---------------------------------------------------------------------------

def test_trim_meta_v01_defaults() -> None:
    """v0 trims (no v0.1 fields) load with safe defaults."""
    v0_dict = {
        "trim_id": "old", "armor": "blacksmith",
    }
    rt = trims_mod.TrimMeta.from_dict(v0_dict)
    assert rt.schema_version == "0.1"
    assert rt.env == "nonprod"
    assert rt.posture == "blackbox"
    assert rt.depth == "recon"
    assert rt.active_testing is False
    assert rt.assets == []
    assert rt.tod_allowed == []
    assert rt.rate_limits == {}
    assert rt.stop_conditions == []


def test_trim_meta_v01_roundtrip_full() -> None:
    meta = trims_mod.TrimMeta(
        trim_id="acme-2026-04",
        armor="blacksmith",
        env="prod",
        posture="whitebox",
        active_testing=True,
        depth="exploitation",
        assets=[
            {"host": "portal.acme.com", "env": "prod"},
            {"host": "staging.acme.com", "env": "nonprod"},
        ],
        tod_allowed=[{
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "start": "09:00", "end": "17:00",
            "tz": "America/New_York",
        }],
        rate_limits={"global_rps": 10, "per_host": {"portal.acme.com": 5}},
        stop_conditions=["first_critical", "max_findings:50"],
        declared_tokens=[
            "ACTIVE-TESTING-APPROVAL", "ENV-PROD",
            "ACTIVE-PROD-APPROVAL", "POSTURE-WHITEBOX",
        ],
    )
    rt = trims_mod.TrimMeta.from_dict(meta.to_dict())
    assert rt == meta


def test_trim_meta_v01_persists_to_disk_and_loads(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.new_trim(tmp_path, "blacksmith", "t1")
    meta.env = "prod"
    meta.posture = "whitebox"
    meta.active_testing = True
    meta.tod_allowed = [{"days": ["mon"], "start": "09:00", "end": "17:00",
                         "tz": "UTC"}]
    meta.rate_limits = {"global_rps": 5}
    meta.stop_conditions = ["first_critical"]
    trims_mod.save_meta(tmp_path, meta)

    loaded = trims_mod.load_trim(tmp_path, "blacksmith", "t1")
    assert loaded is not None
    assert loaded.env == "prod"
    assert loaded.posture == "whitebox"
    assert loaded.active_testing is True
    assert loaded.tod_allowed[0]["start"] == "09:00"
    assert loaded.rate_limits == {"global_rps": 5}
    assert loaded.stop_conditions == ["first_critical"]
    assert loaded.schema_version == "0.1"


@pytest.mark.parametrize("good", [
    "abc", "abc-2026-04", "a.b.c", "AcmePortal", "x_y_z", "a" * 64,
])
def test_validate_trim_id_accepts(good: str) -> None:
    trims_mod._validate_trim_id(good)


@pytest.mark.parametrize("bad", [
    "", "-leading-dash", ".dot", "/slash", "has space", "a" * 65, "a/b",
])
def test_validate_trim_id_rejects(bad: str) -> None:
    with pytest.raises(trims_mod.InvalidTrimId):
        trims_mod._validate_trim_id(bad)


# ---------------------------------------------------------------------------
# new_trim / list / load / delete
# ---------------------------------------------------------------------------

def test_new_trim_creates_files_and_loads_back(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.new_trim(
        tmp_path, "blacksmith", "t1",
        target="https://x.example", operator_id="me",
    )
    assert meta.trim_id == "t1"
    assert trims_mod.trim_meta_path(tmp_path, "blacksmith", "t1").is_file()
    assert trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", "t1").is_file()
    assert trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", "t1").is_file()
    loaded = trims_mod.load_trim(tmp_path, "blacksmith", "t1")
    assert loaded is not None and loaded.trim_id == "t1"


def test_new_trim_unknown_armor_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        trims_mod.new_trim(tmp_path, "ghost", "t1")


def test_new_trim_duplicate_raises(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    trims_mod.new_trim(tmp_path, "blacksmith", "t1")
    with pytest.raises(FileExistsError):
        trims_mod.new_trim(tmp_path, "blacksmith", "t1")


def test_list_trims_sorted_and_per_armor(tmp_path: Path) -> None:
    _mk_armor(tmp_path, "blacksmith")
    _mk_armor(tmp_path, "scout")
    trims_mod.new_trim(tmp_path, "blacksmith", "b1")
    trims_mod.new_trim(tmp_path, "blacksmith", "a2")
    trims_mod.new_trim(tmp_path, "scout", "z9")

    bs = [t.trim_id for t in trims_mod.list_trims(tmp_path, "blacksmith")]
    assert bs == ["a2", "b1"]
    sc = [t.trim_id for t in trims_mod.list_trims(tmp_path, "scout")]
    assert sc == ["z9"]


def test_list_all_trims_crosses_armors(tmp_path: Path) -> None:
    _mk_armor(tmp_path, "blacksmith")
    _mk_armor(tmp_path, "scout")
    trims_mod.new_trim(tmp_path, "blacksmith", "b1")
    trims_mod.new_trim(tmp_path, "scout", "z9")
    ids = sorted(t.trim_id for t in trims_mod.list_all_trims(tmp_path))
    assert ids == ["b1", "z9"]


def test_delete_trim_removes_dir(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    trims_mod.new_trim(tmp_path, "blacksmith", "t1")
    assert trims_mod.delete_trim(tmp_path, "blacksmith", "t1") is True
    assert trims_mod.load_trim(tmp_path, "blacksmith", "t1") is None
    assert trims_mod.delete_trim(tmp_path, "blacksmith", "t1") is False


def test_delete_trim_refuses_when_applied(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    trims_mod.new_trim(tmp_path, "blacksmith", "t1")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    with pytest.raises(trims_mod.TrimCurrentlyApplied):
        trims_mod.delete_trim(tmp_path, "blacksmith", "t1")


# ---------------------------------------------------------------------------
# Marker strip + wrap
# ---------------------------------------------------------------------------

def test_strip_trim_block_preserves_operator_content() -> None:
    text = (
        "# operator header\n"
        "allow fs /home/me/**\n"
        "\n"
        "# ARA-ARMOR-TRIM-BEGIN blacksmith t1\n"
        "allow url https://x/**\n"
        "# ARA-ARMOR-TRIM-END\n"
        "\n"
        "deny fs /etc/**\n"
    )
    out = trims_mod._strip_trim_block(text, "blacksmith", "t1")
    assert "ARA-ARMOR-TRIM" not in out
    assert "operator header" in out
    assert "deny fs /etc/**" in out


def test_strip_trim_block_ignores_inline_marker_mention() -> None:
    # A marker mid-line in prose should NOT be treated as a real begin.
    text = (
        "# Note: do not write `# ARA-ARMOR-TRIM-BEGIN blacksmith t1` "
        "in prose. (this is just docs)\n"
        "allow fs /home/**\n"
    )
    out = trims_mod._strip_trim_block(text, "blacksmith", "t1")
    assert out == text


def test_wrap_then_strip_is_identity() -> None:
    base = "## operator\nallow fs /tmp/**\n"
    body = "allow url https://x/**\nallow net x:443\n"
    wrapped = base + trims_mod._wrap_trim_block(
        "blacksmith", "t1", body, base=base,
    )
    out = trims_mod._strip_trim_block(wrapped, "blacksmith", "t1")
    assert out == base


# ---------------------------------------------------------------------------
# apply_trim / release_trim
# ---------------------------------------------------------------------------

def _seed_trim_with_rules(state: Path, armor: str, trim_id: str) -> None:
    trims_mod.new_trim(state, armor, trim_id)
    trims_mod.trim_bulwark_section_path(state, armor, trim_id).write_text(
        "## armor: blacksmith\n"
        "allow url https://portal.acme.com/**\n"
        "allow net portal.acme.com:443\n"
    )
    trims_mod.trim_absolute_path(state, armor, trim_id).write_text(
        "## Engagement: acme portal\nROE attested.\n"
    )


def test_apply_then_release_is_byte_stable(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    bw = tmp_path / "bulwark.md"
    bw.write_text("# operator standing rules\nallow fs /home/me/**\n")
    bw_before = bw.read_text()

    armor_obj = armors_mod.load_armor(tmp_path, "blacksmith")
    assert armor_obj is not None
    abs_before = armor_obj.absolute_path.read_text()

    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")

    # Markers landed
    assert "ARA-ARMOR-TRIM-BEGIN blacksmith t1" in bw.read_text()
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "t1"
    assert tokens_mod.has_token(
        "TRIM-CONFIRMED:t1", state_root=tmp_path
    )

    # Release strips cleanly back to the byte-stable baseline.
    trims_mod.release_trim(tmp_path, "blacksmith", "t1")
    assert bw.read_text() == bw_before
    assert armor_obj.absolute_path.read_text() == abs_before
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") is None
    assert not tokens_mod.has_token(
        "TRIM-CONFIRMED:t1", state_root=tmp_path
    )


def test_apply_is_idempotent(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    bw_after_first = (tmp_path / "bulwark.md").read_text()
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    assert (tmp_path / "bulwark.md").read_text() == bw_after_first


def test_apply_refuses_second_trim_on_same_armor(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    _seed_trim_with_rules(tmp_path, "blacksmith", "t2")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    with pytest.raises(trims_mod.TrimCurrentlyApplied):
        trims_mod.apply_trim(tmp_path, "blacksmith", "t2")


def test_release_unknown_is_noop(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    res = trims_mod.release_trim(tmp_path, "blacksmith")
    assert res["released"] is False


def test_release_with_no_trim_id_releases_active(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    res = trims_mod.release_trim(tmp_path, "blacksmith")
    assert res["released"] is True
    assert res["trim_id"] == "t1"


# ---------------------------------------------------------------------------
# TTL injection
# ---------------------------------------------------------------------------

def test_inject_ttl_adds_to_bare_rules() -> None:
    body = "allow url https://x/**\ndeny  net y:*\n"
    expires = _iso_in(3600)
    out = trims_mod._inject_ttl_into_rules(body, expires)
    for line in out.splitlines():
        assert " ttl " in line


def test_inject_ttl_no_expires_is_passthrough() -> None:
    body = "allow url https://x/**\n"
    assert trims_mod._inject_ttl_into_rules(body, None) == body


def test_inject_ttl_min_wins_keeps_shorter_existing() -> None:
    body = "allow ttl 30s url https://x/**\n"
    out = trims_mod._inject_ttl_into_rules(body, _iso_in(3600))
    # 30s < 3600s, existing kept
    assert "ttl 30s" in out


def test_inject_ttl_min_wins_replaces_longer_existing() -> None:
    body = "allow ttl 7d url https://x/**\n"
    # Pin `now` so the engagement window is exactly 1h.
    fixed_now = 1_700_000_000.0
    end_iso = datetime.fromtimestamp(
        fixed_now + 3600, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = trims_mod._inject_ttl_into_rules(body, end_iso, now=fixed_now)
    assert "ttl 7d" not in out
    assert "ttl 1h" in out


def test_inject_ttl_skips_non_rule_lines() -> None:
    body = "# comment\n\nallow url https://x/**\n"
    out = trims_mod._inject_ttl_into_rules(body, _iso_in(60))
    lines = out.splitlines()
    assert lines[0] == "# comment"
    assert lines[1] == ""
    assert " ttl " in lines[2]


# ---------------------------------------------------------------------------
# derive_trim
# ---------------------------------------------------------------------------

def _engagement(end_iso: str) -> dict:
    return {
        "engagement_id": "acme-2026-04",
        "operator_id": "ops@acme.com",
        "target": {
            "primary": "https://portal.acme.com",
            "scope_allows": ["portal.acme.com", "api.acme.com"],
            "oos": ["billing.acme.com"],
        },
        "test_window": {"start": _iso_in(0), "end": end_iso},
        "authorization": {
            "signed_by": "ops@acme.com", "signed_at": "2026-04-20T00:00:00Z",
        },
        "delivery_channels": ["http", "tls"],
        "operator_prefs": {
            "approve_high_risk": True,
            "high_risk_patterns": ["* delete *", "* drop *"],
        },
        "active_testing": "YES",
        "misc": "Single-tenant, do not test billing flows.",
    }


def test_derive_trim_materializes_files(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    end = _iso_in(7 * 24 * 3600)
    eng = _engagement(end)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        weapon_id="wstg-flocker-v0",
        engagement=eng,
    )
    assert meta.trim_id == "acme-2026-04"
    assert meta.derived_from == "engagement-derive-v0.1"
    assert meta.expires_at == end
    assert "ACTIVE-TESTING-APPROVAL" in meta.declared_tokens

    bw_section = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", "acme-2026-04",
    ).read_text()
    # Scope allows present
    assert "allow url   https://portal.acme.com/**" in bw_section
    assert "allow url   https://api.acme.com/**" in bw_section
    # OOS deny present
    assert "deny  url   https://billing.acme.com/**" in bw_section
    # Default-deny regex emitted (Q10)
    assert "deny  url   regex:^https?://(?!" in bw_section
    # High-risk operator approval line emitted
    assert "approval oc:bash * delete *" in bw_section

    abs_section = trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", "acme-2026-04",
    ).read_text()
    assert "Engagement: https://portal.acme.com" in abs_section
    assert "**In scope:**" in abs_section
    assert "billing.acme.com" in abs_section
    assert "Operator Notes" in abs_section


def test_derive_trim_scope_deny_regex_tolerates_port_and_pathless(
    tmp_path: Path,
) -> None:
    """The default-deny-everything-outside-allowlist regex must tolerate
    `:port` and path-less URLs. Without these tolerances, in-scope HTTPS
    traffic was incorrectly denied by both request-scope (URL with
    `:443`) and response-scope (URL like ``https://host:443`` with no
    path) bulwark checks. Reproduces a field-test bug found 2026-04-27
    while wiring pause-and-ask v0.2 against an LLM provider host that
    had been correctly added to scope_allows.
    """
    import re as _re
    _mk_armor(tmp_path)
    eng = {
        "engagement_id": "test-eng-2026", "operator_id": "ops",
        "target": {
            "primary": "https://target.example.com",
            "scope_allows": ["target.example.com", "*.target.example.com"],
            "env": "nonprod",
        },
        "test_window": {"start": _iso_in(0), "end": _iso_in(86400)},
        "posture": "blackbox", "active_testing": True,
    }
    trims_mod.derive_trim(tmp_path, "blacksmith", engagement=eng)
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", "test-eng-2026",
    ).read_text()
    deny_line = next(
        line for line in bw.splitlines()
        if "deny  url   regex:" in line
    )
    pattern = deny_line.split("regex:", 1)[1].strip()
    rx = _re.compile(pattern)

    # Out-of-scope URLs SHOULD be denied.
    assert rx.match("https://evil.com/")
    assert rx.match("https://evil.com/foo/bar")

    # In-scope URLs SHOULD NOT be denied — including with port,
    # without trailing slash, and as path-less host:port forms.
    assert not rx.match("https://target.example.com/")
    assert not rx.match("https://target.example.com/v1/x")
    assert not rx.match("https://target.example.com:443/v1/x")
    assert not rx.match("https://target.example.com:443")  # response-scope
    assert not rx.match("https://api.target.example.com/")
    assert not rx.match("https://api.target.example.com:443")


def test_derive_trim_then_apply_round_trip(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    end = _iso_in(3600)
    trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_engagement(end),
    )
    trims_mod.apply_trim(tmp_path, "blacksmith", "acme-2026-04")
    bw = (tmp_path / "bulwark.md").read_text()
    # TTL injected by apply_trim
    assert " ttl " in bw
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "acme-2026-04"


# ---------------------------------------------------------------------------
# release_active_trim_for_armor (armor-unequip hook)
# ---------------------------------------------------------------------------

def test_release_active_trim_for_armor_releases_when_flag_true(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    res = trims_mod.release_active_trim_for_armor(tmp_path, "blacksmith")
    assert res is not None
    assert res["released"] is True
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") is None


def test_release_active_trim_for_armor_respects_flag_false(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    meta = trims_mod.load_trim(tmp_path, "blacksmith", "t1")
    assert meta is not None
    meta.release_on_armor_unequip = False
    trims_mod.save_meta(tmp_path, meta)
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")

    res = trims_mod.release_active_trim_for_armor(tmp_path, "blacksmith")
    assert res is not None
    assert res["released"] is False
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "t1"


def test_release_active_trim_for_armor_noop_when_none(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    assert trims_mod.release_active_trim_for_armor(
        tmp_path, "blacksmith"
    ) is None


# ---------------------------------------------------------------------------
# Armor-unequip hook integration via armors.set_active_armor
# ---------------------------------------------------------------------------

def test_set_active_armor_releases_prior_trim(tmp_path: Path) -> None:
    _mk_armor(tmp_path, "blacksmith")
    _mk_armor(tmp_path, "scout")
    armors_mod.set_active_armor(tmp_path, "blacksmith")

    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "t1"

    # Swap to scout — blacksmith's trim should auto-release.
    armors_mod.set_active_armor(tmp_path, "scout")
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") is None


def test_set_active_armor_skips_release_when_flag_false(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path, "blacksmith")
    _mk_armor(tmp_path, "scout")
    armors_mod.set_active_armor(tmp_path, "blacksmith")

    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    meta = trims_mod.load_trim(tmp_path, "blacksmith", "t1")
    assert meta is not None
    meta.release_on_armor_unequip = False
    trims_mod.save_meta(tmp_path, meta)
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")

    armors_mod.set_active_armor(tmp_path, "scout")
    # Trim should remain applied per the flag.
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "t1"


# ---------------------------------------------------------------------------
# derive_trim v0.1 — multi-axis composition
# ---------------------------------------------------------------------------

def _eng_v01(**overrides) -> dict:
    """Base v0.1 engagement dict; tests override individual fields."""
    base = {
        "engagement_id": "acme-2026-04",
        "operator_id": "ops@acme.com",
        "target": {
            "primary": "https://portal.acme.com",
            "scope_allows": ["portal.acme.com", "api.acme.com"],
            "oos": ["billing.acme.com"],
            "env": "nonprod",
        },
        "test_window": {"start": _iso_in(0), "end": _iso_in(7 * 86400)},
        "authorization": {"signed_by": "ops@acme.com",
                          "signed_at": "2026-04-27T00:00:00Z"},
        "delivery_channels": ["http", "tls"],
        "posture": "blackbox",
        "active_testing": False,
        "depth": "recon",
        "operator_prefs": {},
        "misc": "",
    }
    # Deep-merge target overrides
    if "target" in overrides:
        merged_target = {**base["target"], **overrides.pop("target")}
        base["target"] = merged_target
    base.update(overrides)
    return base


def test_derive_v01_env_nonprod_declares_env_token(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(target={"env": "nonprod"}),
    )
    assert "ENV-NONPROD" in meta.declared_tokens
    assert "ENV-PROD" not in meta.declared_tokens
    assert meta.env == "nonprod"


def test_derive_v01_env_prod_emits_approval_gates_and_token(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(
            target={"env": "prod"},
            active_testing=True,
        ),
    )
    assert "ENV-PROD" in meta.declared_tokens
    assert "ACTIVE-PROD-APPROVAL" in meta.declared_tokens
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "approval url     * method:DELETE" in bw
    assert "approval oc:bash * delete *" in bw
    abs_md = trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "PROD ENVIRONMENT" in abs_md


def test_derive_v01_env_mixed_emits_per_host_rules(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    eng = _eng_v01(target={
        "env": "mixed",
        "assets": [
            {"host": "portal.acme.com", "env": "prod"},
            {"host": "staging.acme.com", "env": "nonprod"},
        ],
    })
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement=eng)
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "approval url     https://portal.acme.com/* method:DELETE" in bw
    # Nonprod-tagged host should NOT have an approval gate.
    assert "approval url     https://staging.acme.com" not in bw


def test_derive_v01_posture_blackbox_denies_mats(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(posture="blackbox"),
    )
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "deny  oc:read **/engagements/acme-2026-04/mats/**" in bw
    assert meta.posture == "blackbox"


def test_derive_v01_posture_whitebox_allows_populated_mat_classes(
    tmp_path: Path,
) -> None:
    from ara import engagement_mats as em
    em.add_mat_bytes(
        tmp_path, "acme-2026-04", "source", "main.py", b"print('hi')",
    )
    em.add_mat_bytes(
        tmp_path, "acme-2026-04", "architecture", "design.md", b"# arch",
    )
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(posture="whitebox"),
    )
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "allow oc:read **/engagements/acme-2026-04/mats/source/**" in bw
    assert "allow oc:read **/engagements/acme-2026-04/mats/architecture/**" in bw
    # No allow for empty classes.
    assert "/mats/creds/" not in bw
    assert "POSTURE-WHITEBOX" in meta.declared_tokens
    abs_md = trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "Provided mats" in abs_md
    assert "engagements/acme-2026-04/mats/source/" in abs_md


def test_derive_v01_uses_only_selected_mats_for_posture_allows(
    tmp_path: Path,
) -> None:
    from ara import engagement_mats as em
    em.add_mat_bytes(
        tmp_path, "acme-2026-04", "source", "main.py", b"print('hi')",
    )
    em.add_mat_bytes(
        tmp_path, "acme-2026-04", "architecture", "design.md", b"# arch",
    )
    assert em.set_mat_selected(
        tmp_path, "acme-2026-04", "architecture/design.md", False,
    ) is True
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(posture="whitebox"),
    )
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "allow oc:read **/engagements/acme-2026-04/mats/source/**" in bw
    assert "allow oc:read **/engagements/acme-2026-04/mats/architecture/**" not in bw
    abs_md = trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "engagements/acme-2026-04/mats/source/" in abs_md
    assert "engagements/acme-2026-04/mats/architecture/" not in abs_md


def test_derive_v01_passive_gates_outbound_to_scope(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(active_testing=False),
    )
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "approval url     https://portal.acme.com/**" in bw
    assert "ACTIVE-TESTING-APPROVAL" not in meta.declared_tokens


def test_derive_v01_active_declares_token_no_passive_gate(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(active_testing=True),
    )
    assert "ACTIVE-TESTING-APPROVAL" in meta.declared_tokens
    bw = trims_mod.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    # Passive-only gate header should be absent.
    assert "Passive-only" not in bw


def test_derive_v01_persists_axis_fields_to_meta(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(
            target={"env": "prod"},
            posture="graybox",
            active_testing=True,
            depth="exploitation",
            test_window={
                "start": _iso_in(0), "end": _iso_in(7 * 86400),
                "tod_allowed": [{
                    "days": ["mon", "tue"], "start": "09:00",
                    "end": "17:00", "tz": "UTC",
                }],
            },
            rate_limits={"global_rps": 10, "per_host": {"portal.acme.com": 5}},
            stop_conditions=["first_critical", "max_findings:50"],
        ),
    )
    assert meta.env == "prod"
    assert meta.posture == "graybox"
    assert meta.active_testing is True
    assert meta.depth == "exploitation"
    assert meta.tod_allowed[0]["start"] == "09:00"
    assert meta.rate_limits == {"global_rps": 10, "per_host": {"portal.acme.com": 5}}
    assert meta.stop_conditions == ["first_critical", "max_findings:50"]


def test_derive_v01_writes_canonical_intake(tmp_path: Path) -> None:
    """Spec §12 — intake.json lands at engagement-scoped path."""
    from ara import engagement_mats as em
    _mk_armor(tmp_path)
    trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(),
    )
    intake = em.read_intake(tmp_path, "acme-2026-04")
    assert intake is not None
    assert intake["engagement_id"] == "acme-2026-04"
    assert intake["target"]["primary"] == "https://portal.acme.com"


def test_derive_v01_absolute_trim_includes_tod_and_rate_limits(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(
            test_window={
                "start": _iso_in(0), "end": _iso_in(86400),
                "tod_allowed": [{
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "start": "09:00", "end": "17:00",
                    "tz": "America/New_York",
                }],
            },
            rate_limits={"global_rps": 10, "overflow": "queue"},
            stop_conditions=["first_critical"],
        ),
    )
    abs_md = trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "Test window (time-of-day)" in abs_md
    assert "America/New_York" in abs_md
    assert "Rate limits" in abs_md
    assert "10 RPS global" in abs_md
    assert "Stop conditions" in abs_md
    assert "first_critical" in abs_md


def test_refresh_trim_window_updates_materialized_absolute_and_active_block(
    tmp_path: Path,
) -> None:
    _mk_armor(tmp_path)
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    old_end = "2026-06-18T16:14:00Z"
    new_start = "2026-06-19T01:42:27Z"
    new_end = "2026-06-19T02:42:27Z"
    meta = trims_mod.derive_trim(
        tmp_path,
        "blacksmith",
        trim_id="task-refresh",
        engagement=_eng_v01(
            test_window={
                "start": "2026-06-18T15:14:00Z",
                "end": old_end,
                "tod_allowed": [],
            },
            active_testing=True,
        ),
    )
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    assert old_end in trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()

    updated = trims_mod.refresh_trim_window(
        tmp_path,
        "blacksmith",
        meta.trim_id,
        start=new_start,
        end=new_end,
        apply_if_active=True,
    )

    assert updated.test_window_start == new_start
    assert updated.test_window_end == new_end
    assert updated.expires_at == new_end
    absolute = trims_mod.trim_absolute_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    source = (
        trims_mod.trim_dir(tmp_path, "blacksmith", meta.trim_id)
        / "source" / "engagement.json"
    ).read_text()
    active_absolute = armors_mod.load_armor(tmp_path, "blacksmith").absolute_path.read_text()
    assert new_end in absolute
    assert new_end in source
    assert new_end in active_absolute
    assert old_end not in absolute
    assert old_end not in active_absolute


def test_derive_v01_legacy_active_testing_yes_string_still_works(
    tmp_path: Path,
) -> None:
    """Back-compat: pre-0.1 engagement.json files used 'YES'/'NO'."""
    _mk_armor(tmp_path)
    meta = trims_mod.derive_trim(
        tmp_path, "blacksmith",
        engagement=_eng_v01(active_testing="YES"),
    )
    assert meta.active_testing is True
    assert "ACTIVE-TESTING-APPROVAL" in meta.declared_tokens


# ---------------------------------------------------------------------------
# Pre-existing armor-unequip hook tests
# ---------------------------------------------------------------------------

def test_set_active_armor_same_name_does_not_release(tmp_path: Path) -> None:
    _mk_armor(tmp_path)
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    _seed_trim_with_rules(tmp_path, "blacksmith", "t1")
    trims_mod.apply_trim(tmp_path, "blacksmith", "t1")
    # Re-equip the same armor — trim should survive.
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    assert trims_mod.active_trim_id(tmp_path, "blacksmith") == "t1"
