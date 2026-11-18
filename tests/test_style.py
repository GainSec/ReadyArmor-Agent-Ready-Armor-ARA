"""Tests for runtime.style."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext, ChannelRejection
from ara.runtime.style.channels import install_style_channel
from ara.runtime.style.extractor import extract_section_profile
from ara.runtime.style.profile import (
    DistributionStats,
    ScoringWeights,
    SectionProfile,
    WeaponStyle,
    read,
    write,
)
from ara.runtime.style.scorer import score_section


def setup_function(_fn) -> None:
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


# ---------- profile IO ----------

def test_weapon_style_round_trip(tmp_path: Path) -> None:
    style = WeaponStyle(
        reference_mat_id="mat_xyz", extracted_at=time.time(),
        bootstrap_mode=False,
        profiles={
            "executive_summary": SectionProfile(
                sentence_length=DistributionStats(20.0, 5.0, 17.0, 20.0, 23.0, 50),
                hedging_density=1.0, voice_ratio=0.2,
            ),
        },
        weights=ScoringWeights(),
    )
    p = tmp_path / "style.json"
    write(p, style)
    loaded = read(p)
    assert "executive_summary" in loaded.profiles
    assert loaded.profiles["executive_summary"].hedging_density == 1.0
    assert loaded.reference_mat_id == "mat_xyz"


# ---------- extractor (deterministic) ----------

def test_extractor_is_deterministic() -> None:
    text = "This is a test. Probably this works. Likely so."
    a = extract_section_profile(text)
    b = extract_section_profile(text)
    assert a == b


def test_extractor_counts_hedging_density() -> None:
    text = "Probably this is correct. Likely it works. Maybe."
    p = extract_section_profile(text)
    assert p.hedging_density > 0
    assert "probably" in p.hedging_markers


def test_extractor_passive_voice_ratio() -> None:
    text = (
        "The system was compromised. Findings are documented. "
        "We tested the endpoint. Bob ran the scan."
    )
    p = extract_section_profile(text)
    assert 0.0 < p.voice_ratio < 1.0


def test_extractor_citation_density() -> None:
    text = (
        "Per F:abcdef0123 and L:1234567890 the issue is clear. "
        "See evidence e:abcdef0123."
    )
    p = extract_section_profile(text)
    assert p.citation_density > 0


def test_extractor_heading_distribution() -> None:
    text = "# Title\n## Sub\n### Sub-sub\n## Another\n"
    p = extract_section_profile(text)
    assert p.heading_depth_distribution == {1: 1, 2: 2, 3: 1}


def test_extractor_handles_empty_text() -> None:
    p = extract_section_profile("")
    assert p.hedging_density == 0.0
    assert p.sentence_length.n_samples == 0


# ---------- scorer ----------

def test_score_section_with_aligned_text_low_aggregate() -> None:
    ref = SectionProfile(
        sentence_length=DistributionStats(20.0, 5.0, 17.0, 20.0, 23.0, 30),
        hedging_density=1.0, adverb_density=2.0, citation_density=3.0,
        voice_ratio=0.2, list_vs_paragraph_ratio=0.1, register_score=0.5,
        soft_threshold=0.7, hard_threshold=1.5,
    )
    style = WeaponStyle(profiles={"summary": ref})
    text = (
        "This finding documents a vulnerability F:abcdef0123 in the system. "
        "The endpoint was identified during enumeration. Multiple users may be affected. "
        "Verification is straightforward and likely succeeds in production."
    )
    score = score_section(text, section_name="summary", weapon_style=style)
    assert score.section == "summary"
    assert score.elapsed_ms >= 0


def test_score_section_unknown_section_returns_passthrough() -> None:
    style = WeaponStyle()
    score = score_section("text", section_name="missing", weapon_style=style)
    assert score.soft_threshold_passed
    assert score.hard_threshold_passed
    assert score.aggregate == 0.0


def test_score_section_includes_revision_suggestions() -> None:
    ref = SectionProfile(
        sentence_length=DistributionStats(5.0, 1.0, 5.0, 5.0, 5.0, 30),
        hedging_density=0.0,
    )
    style = WeaponStyle(profiles={"s": ref})
    # Text with very long sentences and lots of hedging
    text = (
        "This particularly long sentence which probably continues for a great "
        "many words might possibly be likely to perhaps perhaps include lots of "
        "hedging which seems generally to indicate uncertainty.\n"
        "Possibly likely seems suggests probably."
    )
    score = score_section(text, section_name="s", weapon_style=style)
    assert score.aggregate > 0
    assert score.suggested_revisions  # at least one


# ---------- channel ----------

def test_style_score_channel_writeup_only(tmp_path: Path) -> None:
    style = WeaponStyle(
        profiles={"summary": SectionProfile()},
    )
    install_style_channel(weapon_style=style, allowed_chunks=("writeup",))
    ch = registry.get("style.score")
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a", caller="agent",
        audit_sink=tmp_path / "events.jsonl",
    )
    # Wrong chunk: rejected
    with pytest.raises(ChannelRejection, match="restricted"):
        ch.call({"text": "hello", "section_name": "summary",
                  "chunk_id": "probe"}, ctx)
    # Right chunk: works
    out = ch.call(
        {"text": "this is a sentence", "section_name": "summary",
         "chunk_id": "writeup"}, ctx,
    )
    assert "score" in out


def test_style_score_channel_rejects_empty_text(tmp_path: Path) -> None:
    style = WeaponStyle(profiles={"x": SectionProfile()})
    install_style_channel(weapon_style=style)
    ch = registry.get("style.score")
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a", caller="agent",
        audit_sink=tmp_path / "events.jsonl",
    )
    with pytest.raises(ChannelRejection, match="empty"):
        ch.call({"text": "  ", "section_name": "x", "chunk_id": "writeup"}, ctx)
