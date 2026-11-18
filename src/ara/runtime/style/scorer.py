"""Style scoring: compare actual section profile against a reference."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from ara.runtime.style.extractor import extract_section_profile
from ara.runtime.style.profile import (
    DistributionStats,
    SectionProfile,
    WeaponStyle,
)


@dataclass(frozen=True)
class StyleScore:
    section: str
    aggregate: float
    features: dict[str, float]
    soft_threshold_passed: bool
    hard_threshold_passed: bool
    suggested_revisions: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_jsonable(self) -> dict:
        return asdict(self)


def _z(actual: float, ref: float, std: float) -> float:
    if std <= 1e-9:
        return abs(actual - ref) * 1.0  # treat as raw diff when no spread
    return (actual - ref) / std


def _z_for_dist(actual: DistributionStats, ref: DistributionStats) -> float:
    return _z(actual.mean, ref.mean, ref.std or 1.0)


def _z_for_density(actual: float, ref: float) -> float:
    # Use a heuristic std equal to max(0.5, 0.25*ref) when no real std available
    std = max(0.5, abs(ref) * 0.25)
    return _z(actual, ref, std)


def score_section(
    text: str, *, section_name: str, weapon_style: WeaponStyle,
) -> StyleScore:
    start = time.time()
    ref = weapon_style.profiles.get(section_name)
    if ref is None:
        return StyleScore(
            section=section_name, aggregate=0.0, features={},
            soft_threshold_passed=True, hard_threshold_passed=True,
            suggested_revisions=[
                f"no reference profile for section {section_name!r} (bootstrap?)",
            ],
            elapsed_ms=(time.time() - start) * 1000.0,
        )
    actual = extract_section_profile(text, section_name=section_name)
    z: dict[str, float] = {
        "sentence_length": _z_for_dist(actual.sentence_length, ref.sentence_length),
        "paragraph_length": _z_for_dist(actual.paragraph_length, ref.paragraph_length),
        "hedging_density": _z_for_density(actual.hedging_density, ref.hedging_density),
        "adverb_density": _z_for_density(actual.adverb_density, ref.adverb_density),
        "citation_density": _z_for_density(actual.citation_density, ref.citation_density),
        "voice_ratio": _z_for_density(actual.voice_ratio, ref.voice_ratio),
        "list_vs_paragraph_ratio": _z_for_density(
            actual.list_vs_paragraph_ratio, ref.list_vs_paragraph_ratio,
        ),
        "register_score": _z_for_density(actual.register_score, ref.register_score),
    }
    weights = weapon_style.weights.as_dict()
    aggregate = sum(weights.get(k, 0.0) * abs(v) for k, v in z.items())
    revisions = _suggest_revisions(z)
    elapsed = (time.time() - start) * 1000.0
    return StyleScore(
        section=section_name, aggregate=aggregate, features=z,
        soft_threshold_passed=aggregate <= ref.soft_threshold,
        hard_threshold_passed=aggregate <= ref.hard_threshold,
        suggested_revisions=revisions, elapsed_ms=elapsed,
    )


def _suggest_revisions(z_scores: dict[str, float]) -> list[str]:
    out: list[str] = []
    sorted_items = sorted(z_scores.items(), key=lambda kv: -abs(kv[1]))
    for name, z in sorted_items[:3]:
        if abs(z) < 0.5:
            continue
        direction = "reduce" if z > 0 else "increase"
        out.append(f"{direction} {name.replace('_', ' ')}: z={z:+.2f}")
    return out
