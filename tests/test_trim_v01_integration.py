"""End-to-end integration tests for armor-trim v0.1.

Exercises the full multi-axis pipeline:

- engagement.json (rich) → derive_trim → bulwark.md + absolute-trim.md
- Mats populated → posture-driven oc:read allows surface in overlay
- TOD windows → runtime tod gate evaluates correctly
- Rate-limit seeded → bucket persists, acquire denies after exhaustion
- Stop-condition seeded → finding stream trips latch
- Knowledge-gap detector → skips recon when facts already populated
- Tool classification gate → stub agent reply parsed + recorded
- Apply → release round-trip preserves byte-stable bulwark
- ACTIVE-PROD-APPROVAL declared on the active+prod combo (Q11)
- Mixed env emits per-host approval gates
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ara import armors as armors_mod
from ara import engagement_mats as em
from ara import tool_registry as tr
from ara import tokens as tk
from ara import trims as tm
from ara.runtime import (
    knowledge_gap as kg,
    rate_bucket as rb,
    stop_watcher as sw,
    tod as tod_mod,
)


def _utc_epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _full_engagement(eid: str = "acme-portal-2026-04") -> dict:
    return {
        "engagement_id": eid,
        "operator_id": "ops@acme.com",
        "target": {
            "primary": "https://portal.acme.com",
            "scope_allows": ["portal.acme.com", "api.acme.com"],
            "oos": ["billing.acme.com"],
            "env": "prod",
        },
        "test_window": {
            "start": "2026-05-04T13:00:00Z",
            "end":   "2026-05-11T13:00:00Z",
            "tod_allowed": [{
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "09:00", "end": "17:00",
                "tz": "America/New_York",
            }],
        },
        "authorization": {
            "signed_by": "ops@acme.com",
            "signed_at": "2026-05-01T00:00:00Z",
        },
        "delivery_channels": ["http", "tls"],
        "posture": "whitebox",
        "active_testing": True,
        "depth": "exploitation",
        "rate_limits": {
            "global_rps": 10,
            "per_host": {"portal.acme.com": 5},
            "overflow": "queue",
        },
        "stop_conditions": ["first_critical", "max_findings:50"],
        "operator_prefs": {
            "absolute_enforce": True,
            "release_on_armor_unequip": True,
        },
        "misc": "Single tenant. Do not test billing flows.",
    }


# ---------------------------------------------------------------------------
# Pipeline: derive → meta + files + intake + tokens
# ---------------------------------------------------------------------------

def test_full_pipeline_derives_complete_trim(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    # Pre-populate whitebox mats so derive_trim emits oc:read allows.
    em.add_mat_bytes(
        tmp_path, "acme-portal-2026-04", "source",
        "main.py", b"print('hi')",
    )
    em.add_mat_bytes(
        tmp_path, "acme-portal-2026-04", "architecture",
        "design.md", b"# arch overview",
    )

    meta = tm.derive_trim(
        tmp_path, "blacksmith",
        engagement=_full_engagement(),
    )

    # Schema + axis fields
    assert meta.env == "prod"
    assert meta.posture == "whitebox"
    assert meta.active_testing is True
    assert meta.depth == "exploitation"
    assert meta.rate_limits["global_rps"] == 10
    assert meta.stop_conditions == ["first_critical", "max_findings:50"]
    assert len(meta.tod_allowed) == 1

    # Tokens declared per spec §13
    expected = {
        "ACTIVE-TESTING-APPROVAL", "ENV-PROD", "ACTIVE-PROD-APPROVAL",
        "POSTURE-WHITEBOX",
    }
    assert expected.issubset(set(meta.declared_tokens))

    # Files materialized
    bw = tm.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    abs_md = tm.trim_absolute_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()

    # Prod gates
    assert "approval url     * method:DELETE" in bw
    assert "approval oc:bash * delete *" in bw
    # Whitebox mats reads
    assert "allow oc:read **/engagements/acme-portal-2026-04/mats/source/**" in bw
    assert "allow oc:read **/engagements/acme-portal-2026-04/mats/architecture/**" in bw
    # Absolute prose: prod warning + mats listing + TOD + rate + stop
    assert "PROD ENVIRONMENT" in abs_md
    assert "Provided mats" in abs_md
    assert "America/New_York" in abs_md
    assert "10 RPS global" in abs_md
    assert "first_critical" in abs_md

    # Canonical intake stored
    intake = em.read_intake(tmp_path, "acme-portal-2026-04")
    assert intake is not None
    assert intake["target"]["env"] == "prod"


def test_active_prod_required_token_declared(tmp_path: Path) -> None:
    """Q11: active+prod must declare ACTIVE-PROD-APPROVAL standing."""
    armors_mod.create_armor(tmp_path, "blacksmith")
    eng = _full_engagement()
    eng["target"]["env"] = "prod"
    eng["active_testing"] = True
    meta = tm.derive_trim(tmp_path, "blacksmith", engagement=eng)
    assert "ACTIVE-PROD-APPROVAL" in meta.declared_tokens

    # Passive+prod should NOT declare it.
    eng2 = _full_engagement()
    eng2["target"]["env"] = "prod"
    eng2["active_testing"] = False
    eng2["engagement_id"] = "other-eng"
    meta2 = tm.derive_trim(tmp_path, "blacksmith", engagement=eng2)
    assert "ACTIVE-PROD-APPROVAL" not in meta2.declared_tokens


def test_mixed_env_emits_per_host_gates(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    eng = _full_engagement()
    eng["target"]["env"] = "mixed"
    eng["target"]["assets"] = [
        {"host": "portal.acme.com", "env": "prod"},
        {"host": "staging.acme.com", "env": "nonprod"},
    ]
    meta = tm.derive_trim(tmp_path, "blacksmith", engagement=eng)
    bw = tm.trim_bulwark_section_path(
        tmp_path, "blacksmith", meta.trim_id,
    ).read_text()
    assert "approval url     https://portal.acme.com/* method:DELETE" in bw
    # Nonprod-tagged host should NOT get a per-host gate.
    assert "approval url     https://staging.acme.com" not in bw


def test_apply_release_byte_stable_with_v01_payload(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    bw_path = tmp_path / "bulwark.md"
    bw_path.write_text("# operator standing rules\nallow fs /home/me/**\n")
    bw_before = bw_path.read_text()
    armor_obj = armors_mod.load_armor(tmp_path, "blacksmith")
    assert armor_obj is not None
    abs_before = armor_obj.absolute_path.read_text()

    meta = tm.derive_trim(
        tmp_path, "blacksmith", engagement=_full_engagement(),
    )
    tm.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    assert "ARA-ARMOR-TRIM-BEGIN blacksmith" in bw_path.read_text()

    tm.release_trim(tmp_path, "blacksmith", meta.trim_id)
    # Byte-stable round trip.
    assert bw_path.read_text() == bw_before
    assert armor_obj.absolute_path.read_text() == abs_before


# ---------------------------------------------------------------------------
# Pipeline: derive + apply seeds runtime substrate
# ---------------------------------------------------------------------------

def test_apply_then_seed_runtime_buckets(tmp_path: Path) -> None:
    """The runtime token bucket should be seeded from the trim's
    rate_limits at apply time. (Helper tested directly here; the hook
    into apply_trim is wired by the supervisor.)"""
    armors_mod.create_armor(tmp_path, "blacksmith")
    meta = tm.derive_trim(
        tmp_path, "blacksmith", engagement=_full_engagement(),
    )
    tm.apply_trim(tmp_path, "blacksmith", meta.trim_id)

    # Seed the runtime buckets from the persisted meta — what the
    # supervisor would do.
    rb.seed_buckets(
        tmp_path, meta.engagement_id or "",
        rate_limits=meta.rate_limits, now_epoch=100.0,
    )
    # Per-host bucket exists at the configured RPS.
    b = rb.get_bucket(tmp_path, "acme-portal-2026-04", "portal.acme.com")
    assert b is not None and b.capacity == 5.0

    # Drain the per-host bucket; the 6th call denies.
    for _ in range(5):
        res = rb.acquire(
            tmp_path, "acme-portal-2026-04", "portal.acme.com",
            now_epoch=100.0,
        )
        assert res.granted is True
    res = rb.acquire(
        tmp_path, "acme-portal-2026-04", "portal.acme.com", now_epoch=100.0,
    )
    assert res.granted is False


def test_tod_gate_blocks_outside_window_grants_extend(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    meta = tm.derive_trim(
        tmp_path, "blacksmith", engagement=_full_engagement(),
    )
    eid = meta.engagement_id or "acme-portal-2026-04"
    tod_allowed = meta.tod_allowed

    # Sat 2026-05-09 14:00 UTC → out of window (window is Mon–Fri).
    sat_epoch = _utc_epoch(2026, 5, 9, 14)
    res = tod_mod.evaluate_gate(
        tmp_path, eid, tod_allowed=tod_allowed, now_epoch=sat_epoch,
    )
    assert res.allow is False
    assert res.requires_decision is True

    # Operator extends for 1h.
    tod_mod.grant_decision(
        tmp_path, eid, tod_mod.DECISION_EXTEND,
        ttl_seconds=3600, now_epoch=sat_epoch,
    )
    res2 = tod_mod.evaluate_gate(
        tmp_path, eid, tod_allowed=tod_allowed,
        now_epoch=sat_epoch + 60,
    )
    assert res2.allow is True
    assert res2.state == "extended"


def test_stop_condition_trips_on_critical_finding(tmp_path: Path) -> None:
    eid = "acme-portal-2026-04"
    summary = sw.FindingsSummary(total=1, by_severity={"critical": 1})
    v = sw.evaluate_and_latch(
        tmp_path, eid,
        conditions=["first_critical", "max_findings:50"],
        summary=summary,
        now_epoch=_utc_epoch(2026, 5, 5, 14),
        window_start_epoch=_utc_epoch(2026, 5, 4, 13),
    )
    assert v.tripped is True
    # Latch persists — second call returns the latched verdict even on
    # a clean summary.
    v2 = sw.evaluate_and_latch(
        tmp_path, eid,
        conditions=["first_critical"],
        summary=sw.FindingsSummary(total=0),
        now_epoch=_utc_epoch(2026, 5, 5, 15),
    )
    assert v2.tripped is True
    assert "latched" in v2.reason


def test_knowledge_gap_skips_recon_when_mats_populate_facts(tmp_path: Path) -> None:
    """Whitebox engagement: mats parsed → facts populated → recon
    crumb's preconditions satisfied → skip."""
    class _Store:
        def __init__(self, counts: dict[str, int]) -> None:
            self._c = counts

        def count(self, shape: str) -> int:
            return self._c.get(shape, 0)

    crumb_produces = ["subdomain.host", "endpoint.url"]
    reqs = kg.parse_produces(crumb_produces)
    store = _Store({"subdomain.host": 8, "endpoint.url": 42})
    res = kg.has_sufficient_coverage(reqs, store)
    assert res.skip is True

    # If facts are missing, run.
    store2 = _Store({"subdomain.host": 8})
    res2 = kg.has_sufficient_coverage(reqs, store2)
    assert res2.skip is False


# ---------------------------------------------------------------------------
# Tool classification: pause-and-ask flow
# ---------------------------------------------------------------------------

def test_classification_gate_first_use_records_and_evaluates(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    meta = tm.derive_trim(
        tmp_path, "blacksmith", engagement=_full_engagement(),
    )

    # Stub agent classification reply (the supervisor would inject the
    # prompt and parse the actual model turn — here we just simulate).
    agent_reply = (
        '{"active": true, "destructive": false, "intrusive": true, '
        '"scanner": true, "confidence": 3, "reasoning": "service version probe"}'
    )
    classification, conf, reasoning = tr.parse_classification_response(
        agent_reply,
    )
    key = tr.key_for("/usr/bin/nmap", ["-sV", "-p", "80,443", "portal.acme.com"])
    entry = tr.record_classification(
        tmp_path, key=key,
        classification=classification, confidence=conf, reasoning=reasoning,
        engagement_id=meta.engagement_id,
        weapon=meta.weapon_ref or "",
    )
    assert entry.status == tr.STATUS_PENDING_REVIEW
    assert entry.confidence == 3

    # Policy verdict composes registry + trim flags. active=True +
    # active_testing=True + env=prod + destructive=False → allowed
    # without the prod-destructive token.
    ctx = tr.PolicyContext(
        active_testing=meta.active_testing, env=meta.env, posture=meta.posture,
    )
    verdict = tr.evaluate_policy(entry, ctx)
    assert verdict.allowed is True
    assert verdict.requires_token is None


def test_classification_destructive_on_prod_requires_token(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    meta = tm.derive_trim(
        tmp_path, "blacksmith", engagement=_full_engagement(),
    )
    # Stub a destructive classification.
    entry = tr.record_classification(
        tmp_path, key="sqlmap | -u --batch*",
        classification=tr.Classification(
            active=True, destructive=True, intrusive=True, scanner=True,
        ),
        confidence=3,
        reasoning="sqlmap with --batch can mutate target DB",
    )
    ctx = tr.PolicyContext(
        active_testing=meta.active_testing, env=meta.env, posture=meta.posture,
    )
    verdict = tr.evaluate_policy(entry, ctx)
    assert verdict.allowed is True
    assert verdict.requires_token == "PROD-DESTRUCTIVE-APPROVAL"


def test_classification_passive_engagement_denies_active_tool(
    tmp_path: Path,
) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    eng = _full_engagement()
    eng["active_testing"] = False
    eng["engagement_id"] = "passive-eng"
    meta = tm.derive_trim(tmp_path, "blacksmith", engagement=eng)

    entry = tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="active scan",
    )
    ctx = tr.PolicyContext(
        active_testing=meta.active_testing, env=meta.env, posture=meta.posture,
    )
    verdict = tr.evaluate_policy(entry, ctx)
    assert verdict.allowed is False
    assert "active_testing=false" in verdict.reason


# ---------------------------------------------------------------------------
# Token interactions
# ---------------------------------------------------------------------------

def test_apply_emits_v01_engagement_tokens(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    meta = tm.derive_trim(
        tmp_path, "blacksmith", engagement=_full_engagement(),
    )
    tm.apply_trim(tmp_path, "blacksmith", meta.trim_id)
    # TRIM-CONFIRMED is set on apply (already covered in v0 tests).
    assert tk.has_token(
        f"TRIM-CONFIRMED:{meta.trim_id}", state_root=tmp_path,
    )
    # The trim's declared_tokens are *registered* — they don't auto-set.
    # Operator (or the wizard) sets them when granting approval.
    reg = tk.known_tokens(tmp_path)
    for name in meta.declared_tokens:
        # Targeted tokens like ACTIVE-PROD-APPROVAL:<eid> aren't in the
        # builtin registry; the base name is.
        base = name.split(":", 1)[0]
        assert base in reg, f"{base} should be a known builtin"
