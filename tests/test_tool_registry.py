"""Tests for ``ara.tool_registry`` — pause-and-ask classification + policy.

Per spec ``armor-trim-v0.1-amendment.md`` §11. Covers:

- Key derivation (basename, .exe strip, flag pattern reduction).
- Dataclass round-trip (Classification, OperatorOverride, ToolEntry).
- ``record_classification`` confidence tiers + paranoid mode → status mapping.
- TTL math (default 24h; capped at test_window_end if sooner).
- Reclassification path preserves use_count + first-seen, clears override.
- ``record_use`` bumps use_count + engagements_used.
- Operator actions: approve / override / request-reclassify.
- ``merge_expired_reviews`` flips TTL'd entries to ``merged_by_ttl``.
- Prompt builder: includes flags, never target data.
- Response parser: bare JSON, fenced ```json``` blocks, embedded JSON,
  malformed input.
- Conflict detection between global + weapon-local registries.
- Policy verdict: 3 rule paths (active+passive, destructive+prod, allow).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ara import tool_registry as tr


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args, expected", [
    (["-sV", "-p", "80,443", "portal.acme.com"], "-sV -p*"),
    (["-u", "https://x", "--batch"],             "-u --batch*"),
    (["-t", "tpls/cves/", "-target", "x"],       "-t -target*"),
    (["target.example"],                          "*"),
    ([],                                          "*"),
    # Field-test surfaced bug: `--key=value` flags were silently dropped.
    # Now preserved with short-numeric values literal, others globbed.
    (["-u", "https://x", "--batch", "--risk=3"], "-u --batch --risk=3*"),
    (["--risk=3"],                                "--risk=3*"),
    (["--risk=1"],                                "--risk=1*"),
    (["--tamper=between"],                        "--tamper=**"),
    (["--cookie=session=abc; auth=xyz"],          "--cookie=**"),
])
def test_derive_flag_pattern(args: list, expected: str) -> None:
    assert tr.derive_flag_pattern(args) == expected


def test_distinct_keys_for_distinct_risk_levels() -> None:
    """Field test surfaced this: sqlmap --risk=3 and --risk=1 must
    classify separately (risk=3 enables destructive payloads)."""
    k_risky = tr.key_for("sqlmap", ["-u", "https://x", "--risk=3"])
    k_safe = tr.key_for("sqlmap", ["-u", "https://x", "--risk=1"])
    assert k_risky != k_safe


def test_key_for_strips_path_and_ext() -> None:
    assert tr.key_for("/usr/bin/nmap", ["-sV"]) == "nmap | -sV*"
    assert tr.key_for("/opt/homebrew/bin/nmap", ["-sV"]) == "nmap | -sV*"
    assert tr.key_for("nmap.exe", ["-sV"]) == "nmap | -sV*"


def test_key_for_validates_tool_name() -> None:
    with pytest.raises(tr.InvalidToolName):
        tr.key_for("", [])
    with pytest.raises(tr.InvalidToolName):
        tr.key_for("not allowed!", [])


def test_key_for_distinguishes_invocation_shapes() -> None:
    """Spec §11.1 — `nmap -sV` and `nmap -sP` are different keys."""
    assert tr.key_for("nmap", ["-sV"]) != tr.key_for("nmap", ["-sP"])


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

def test_classification_roundtrip() -> None:
    c = tr.Classification(active=True, intrusive=True, scanner=True)
    assert tr.Classification.from_dict(c.to_dict()) == c


def test_operator_override_roundtrip() -> None:
    ov = tr.OperatorOverride(
        classification=tr.Classification(active=False, destructive=False),
        reason="false positive history",
        ts="2026-04-27T13:00:00Z",
    )
    assert tr.OperatorOverride.from_dict(ov.to_dict()) == ov


def test_toolentry_roundtrip_with_override() -> None:
    e = tr.ToolEntry(
        key="nmap | -sV*",
        classification=tr.Classification(active=True, intrusive=True),
        confidence=2,
        reasoning="probes for service version",
        classified_by="agent",
        classified_at="2026-04-27T13:00:00Z",
        status=tr.STATUS_PENDING_REVIEW,
        ttl_expires_at="2026-04-28T13:00:00Z",
        first_seen_engagement="acme",
        use_count=3,
        engagements_used=["acme", "foo"],
        operator_override=tr.OperatorOverride(
            classification=tr.Classification(active=True, destructive=True),
            reason="wraps a destructive script in our env",
            ts="2026-04-27T14:00:00Z",
        ),
    )
    rt = tr.ToolEntry.from_dict(e.to_dict())
    assert rt == e


def test_toolentry_tool_and_flag_pattern_properties() -> None:
    e = tr.ToolEntry(
        key="sqlmap | -u --batch*",
        classification=tr.Classification(),
        confidence=1, reasoning="", classified_by="agent",
        classified_at="", status=tr.STATUS_PENDING_FIRST_USE,
    )
    assert e.tool == "sqlmap"
    assert e.flag_pattern == "-u --batch*"


def test_toolentry_effective_classification_uses_override() -> None:
    base = tr.Classification(active=False)
    override = tr.Classification(active=True, destructive=True)
    e = tr.ToolEntry(
        key="x | *", classification=base, confidence=1, reasoning="",
        classified_by="agent", classified_at="",
        status=tr.STATUS_OPERATOR_OVERRIDDEN,
        operator_override=tr.OperatorOverride(
            classification=override, reason="", ts="",
        ),
    )
    assert e.effective_classification == override


def test_toolentry_invalid_status_rejects() -> None:
    with pytest.raises(tr.InvalidStatus):
        tr.ToolEntry.from_dict({
            "key": "x | *", "status": "garbage",
            "classification": {}, "confidence": 1, "reasoning": "",
            "classified_by": "agent", "classified_at": "",
        })


# ---------------------------------------------------------------------------
# record_classification — status / TTL
# ---------------------------------------------------------------------------

def _fixed_now() -> float:
    return 1_700_000_000.0   # 2023-11-14T22:13:20Z


def test_record_classification_3of3_pending_review(tmp_path: Path) -> None:
    entry = tr.record_classification(
        tmp_path,
        key="nmap | -sV*",
        classification=tr.Classification(active=True, intrusive=True, scanner=True),
        confidence=3,
        reasoning="probes service versions",
        engagement_id="acme",
        now=_fixed_now(),
    )
    assert entry.status == tr.STATUS_PENDING_REVIEW
    assert entry.ttl_expires_at is not None


def test_record_classification_2of3_pending_review(tmp_path: Path) -> None:
    entry = tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="", now=_fixed_now(),
    )
    assert entry.status == tr.STATUS_PENDING_REVIEW


def test_record_classification_1of3_pending_first_use(tmp_path: Path) -> None:
    entry = tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=1, reasoning="unsure", now=_fixed_now(),
    )
    assert entry.status == tr.STATUS_PENDING_FIRST_USE
    assert entry.ttl_expires_at is None        # blocked, no review TTL


def test_record_classification_paranoid_overrides_confidence(tmp_path: Path) -> None:
    entry = tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="confident",
        paranoid=True, now=_fixed_now(),
    )
    assert entry.status == tr.STATUS_PENDING_FIRST_USE


def test_record_classification_invalid_confidence_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        tr.record_classification(
            tmp_path, key="x | *",
            classification=tr.Classification(),
            confidence=4, reasoning="",
        )


def test_ttl_default_is_24h(tmp_path: Path) -> None:
    now = _fixed_now()
    entry = tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="", now=now,
    )
    expected = datetime.fromtimestamp(
        now + 24 * 3600, tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert entry.ttl_expires_at == expected


def test_ttl_capped_by_test_window_end(tmp_path: Path) -> None:
    now = _fixed_now()
    # window ends in 1h — should win over the 24h default.
    window_end = datetime.fromtimestamp(
        now + 3600, tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="",
        test_window_end=window_end, now=now,
    )
    assert entry.ttl_expires_at == window_end


def test_reclassification_preserves_use_count_and_clears_override(
    tmp_path: Path,
) -> None:
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="v1", engagement_id="acme",
        now=_fixed_now(),
    )
    tr.record_use(tmp_path, key="nmap | -sV*", engagement_id="acme")
    tr.operator_override(
        tmp_path, "nmap | -sV*",
        classification=tr.Classification(active=False),
        reason="operator says no",
    )
    # Reclassify — fresh agent classification arrives.
    re = tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True, intrusive=True),
        confidence=2, reasoning="v2", now=_fixed_now(),
    )
    assert re.use_count == 1
    assert re.first_seen_engagement == "acme"
    assert re.operator_override is None      # cleared on reclassify
    assert re.reclassify_on_next_call is False


# ---------------------------------------------------------------------------
# record_use
# ---------------------------------------------------------------------------

def test_record_use_increments_and_dedupes_engagements(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="",
    )
    tr.record_use(tmp_path, key="x | *", engagement_id="a")
    tr.record_use(tmp_path, key="x | *", engagement_id="a")
    e = tr.record_use(tmp_path, key="x | *", engagement_id="b")
    assert e is not None
    assert e.use_count == 3
    assert e.engagements_used == ["a", "b"]


def test_record_use_returns_none_when_unknown(tmp_path: Path) -> None:
    assert tr.record_use(tmp_path, key="never | *", engagement_id="x") is None


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------

def test_operator_approve_flips_status(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="",
    )
    e = tr.operator_approve(tmp_path, "x | *")
    assert e is not None
    assert e.status == tr.STATUS_CONFIRMED
    assert e.ttl_expires_at is None


def test_operator_override_records_and_marks_overridden(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="",
    )
    e = tr.operator_override(
        tmp_path, "x | *",
        classification=tr.Classification(active=False),
        reason="false positive in this engagement",
    )
    assert e is not None
    assert e.status == tr.STATUS_OPERATOR_OVERRIDDEN
    assert e.operator_override is not None
    assert e.effective_classification.active is False


def test_operator_request_reclassify(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(),
        confidence=3, reasoning="",
    )
    e = tr.operator_request_reclassify(tmp_path, "x | *")
    assert e is not None and e.reclassify_on_next_call is True


# ---------------------------------------------------------------------------
# merge_expired_reviews
# ---------------------------------------------------------------------------

def test_merge_expired_reviews_flips_status(tmp_path: Path) -> None:
    now = _fixed_now()
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="", now=now,
    )
    # Sweep with now = +25h → past the 24h TTL.
    merged = tr.merge_expired_reviews(
        tmp_path, now=now + 25 * 3600,
    )
    assert len(merged) == 1
    assert merged[0].status == tr.STATUS_MERGED_BY_TTL
    e = tr.lookup(tmp_path, "x | *")
    assert e is not None and e.status == tr.STATUS_MERGED_BY_TTL


def test_merge_expired_reviews_skips_unrelated_statuses(tmp_path: Path) -> None:
    now = _fixed_now()
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="", now=now,
    )
    tr.operator_approve(tmp_path, "x | *")
    merged = tr.merge_expired_reviews(tmp_path, now=now + 999 * 3600)
    assert merged == []


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def test_prompt_builder_includes_tool_and_flags() -> None:
    prompt = tr.build_classification_prompt(
        tool="/usr/bin/nmap",
        args=["-sV", "-p", "80,443", "portal.acme.com"],
        engagement_env="prod", posture="graybox",
    )
    assert "nmap" in prompt
    assert "-sV" in prompt
    assert "prod" in prompt
    assert "graybox" in prompt
    # Sanitisation: no target hostnames.
    assert "portal.acme.com" not in prompt
    # Flags-only line — port number `80,443` is positional, not a flag,
    # so it should be filtered out.
    assert "80,443" not in prompt


def test_prompt_builder_handles_no_flags() -> None:
    prompt = tr.build_classification_prompt(
        tool="ls", args=["/tmp"],
    )
    assert "(no flags)" in prompt


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

_GOOD_JSON = (
    '{"active": true, "destructive": false, "intrusive": true, '
    '"scanner": true, "confidence": 3, "reasoning": "version probing"}'
)


def test_parse_response_bare_json() -> None:
    cls, conf, reasoning = tr.parse_classification_response(_GOOD_JSON)
    assert cls.active is True
    assert cls.destructive is False
    assert conf == 3
    assert "version probing" in reasoning


def test_parse_response_fenced_json_block() -> None:
    text = f"```json\n{_GOOD_JSON}\n```"
    cls, conf, _ = tr.parse_classification_response(text)
    assert cls.active is True and conf == 3


def test_parse_response_embedded_in_prose() -> None:
    text = f"Sure, here's my classification:\n\n{_GOOD_JSON}\n\nLet me know."
    cls, conf, _ = tr.parse_classification_response(text)
    assert cls.active is True and conf == 3


def test_parse_response_rejects_invalid_confidence() -> None:
    bad = _GOOD_JSON.replace('"confidence": 3', '"confidence": 4')
    with pytest.raises(ValueError):
        tr.parse_classification_response(bad)


def test_parse_response_rejects_non_dict() -> None:
    with pytest.raises(ValueError):
        tr.parse_classification_response("[1, 2, 3]")


def test_parse_response_rejects_empty() -> None:
    with pytest.raises(ValueError):
        tr.parse_classification_response("")


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def test_find_conflicts_surfaces_disagreement(tmp_path: Path) -> None:
    # Global says active=true.
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="",
    )
    # Weapon-local says active=false.
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    (weapon_dir / "tool_classifications.json").write_text(
        '{"entries": [{"key": "nmap | -sV*", '
        '"classification": {"active": false}, '
        '"confidence": 3, "reasoning": "weapon claim", '
        '"classified_by": "agent", "classified_at": "2026-04-27T00:00:00Z", '
        '"status": "confirmed"}]}'
    )
    conflicts = tr.find_conflicts(tmp_path, weapon_dir)
    assert len(conflicts) == 1
    glob, weap = conflicts[0]
    assert glob.classification.active is True
    assert weap.classification.active is False


def test_find_conflicts_empty_when_aligned(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="nmap | -sV*",
        classification=tr.Classification(active=True),
        confidence=3, reasoning="",
    )
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()
    (weapon_dir / "tool_classifications.json").write_text(
        '{"entries": [{"key": "nmap | -sV*", '
        '"classification": {"active": true}, '
        '"confidence": 3, "reasoning": "", '
        '"classified_by": "agent", "classified_at": "2026-04-27T00:00:00Z", '
        '"status": "confirmed"}]}'
    )
    assert tr.find_conflicts(tmp_path, weapon_dir) == []


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------

def _entry(active: bool = False, destructive: bool = False) -> tr.ToolEntry:
    return tr.ToolEntry(
        key="nmap | -sV*",
        classification=tr.Classification(active=active, destructive=destructive),
        confidence=3, reasoning="", classified_by="agent",
        classified_at="2026-04-27T00:00:00Z", status=tr.STATUS_CONFIRMED,
    )


def test_policy_denies_active_when_passive_engagement() -> None:
    v = tr.evaluate_policy(
        _entry(active=True),
        tr.PolicyContext(active_testing=False, env="nonprod", posture="blackbox"),
    )
    assert v.allowed is False
    assert "active_testing=false" in v.reason


def test_policy_requires_token_for_destructive_on_prod() -> None:
    v = tr.evaluate_policy(
        _entry(active=True, destructive=True),
        tr.PolicyContext(active_testing=True, env="prod", posture="blackbox"),
    )
    assert v.allowed is True
    assert v.requires_token == "PROD-DESTRUCTIVE-APPROVAL"


def test_policy_allows_when_within_contract() -> None:
    v = tr.evaluate_policy(
        _entry(active=True, destructive=False),
        tr.PolicyContext(active_testing=True, env="nonprod", posture="blackbox"),
    )
    assert v.allowed is True
    assert v.requires_token is None


def test_policy_uses_operator_override() -> None:
    """Operator override flips the effective classification — verdict
    should reflect the override, not the original."""
    e = _entry(active=False)
    e.operator_override = tr.OperatorOverride(
        classification=tr.Classification(active=True, destructive=True),
        reason="actually destructive on this customer's stack",
        ts="2026-04-27T00:00:00Z",
    )
    v = tr.evaluate_policy(
        e, tr.PolicyContext(
            active_testing=True, env="prod", posture="blackbox",
        ),
    )
    assert v.requires_token == "PROD-DESTRUCTIVE-APPROVAL"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_list_entries_returns_sorted_round_trip(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="b | *",
        classification=tr.Classification(),
        confidence=3, reasoning="",
    )
    tr.record_classification(
        tmp_path, key="a | *",
        classification=tr.Classification(),
        confidence=3, reasoning="",
    )
    keys = [e.key for e in tr.list_entries(tmp_path)]
    assert keys == ["a | *", "b | *"]


def test_delete_entry_removes_persistent_row(tmp_path: Path) -> None:
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(),
        confidence=3, reasoning="",
    )
    assert tr.delete_entry(tmp_path, "x | *") is True
    assert tr.lookup(tmp_path, "x | *") is None
    assert tr.delete_entry(tmp_path, "x | *") is False
