"""WeaponStyle + SectionProfile schemas (2.4)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DistributionStats:
    mean: float = 0.0
    std: float = 0.0
    p25: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    n_samples: int = 0


@dataclass(frozen=True)
class ScoringWeights:
    sentence_length: float = 0.20
    paragraph_length: float = 0.10
    hedging_density: float = 0.20
    adverb_density: float = 0.10
    citation_density: float = 0.10
    voice_ratio: float = 0.15
    list_vs_paragraph_ratio: float = 0.05
    register_score: float = 0.10

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class SectionProfile:
    sentence_length: DistributionStats = field(default_factory=DistributionStats)
    paragraph_length: DistributionStats = field(default_factory=DistributionStats)
    hedging_density: float = 0.0
    hedging_markers: list[str] = field(default_factory=list)
    adverb_density: float = 0.0
    citation_density: float = 0.0
    voice_ratio: float = 0.0
    list_vs_paragraph_ratio: float = 0.0
    heading_depth_distribution: dict[int, int] = field(default_factory=dict)
    canonical_terminology: list[str] = field(default_factory=list)
    register_score: float = 0.0
    soft_threshold: float = 0.7
    hard_threshold: float = 1.0
    iteration_cap: int = 5


@dataclass(frozen=True)
class WeaponStyle:
    reference_mat_id: str | None = None
    extracted_at: float = 0.0
    bootstrap_mode: bool = False
    profiles: dict[str, SectionProfile] = field(default_factory=dict)
    weights: ScoringWeights = field(default_factory=ScoringWeights)


def write(path: Path, style: WeaponStyle) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reference_mat_id": style.reference_mat_id,
        "extracted_at": style.extracted_at,
        "bootstrap_mode": style.bootstrap_mode,
        "profiles": {
            name: _profile_to_jsonable(p) for name, p in style.profiles.items()
        },
        "weights": style.weights.as_dict(),
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2))


def read(path: Path) -> WeaponStyle:
    if not path.exists():
        return WeaponStyle()
    d = json.loads(path.read_text())
    profiles = {
        name: _profile_from_jsonable(p) for name, p in (d.get("profiles") or {}).items()
    }
    weights_raw = d.get("weights") or {}
    return WeaponStyle(
        reference_mat_id=d.get("reference_mat_id"),
        extracted_at=d.get("extracted_at", 0.0),
        bootstrap_mode=bool(d.get("bootstrap_mode", False)),
        profiles=profiles,
        weights=ScoringWeights(**{
            k: float(v) for k, v in weights_raw.items()
            if k in ScoringWeights.__dataclass_fields__
        }) if weights_raw else ScoringWeights(),
    )


def _profile_to_jsonable(p: SectionProfile) -> dict:
    return {
        "sentence_length": asdict(p.sentence_length),
        "paragraph_length": asdict(p.paragraph_length),
        "hedging_density": p.hedging_density,
        "hedging_markers": list(p.hedging_markers),
        "adverb_density": p.adverb_density,
        "citation_density": p.citation_density,
        "voice_ratio": p.voice_ratio,
        "list_vs_paragraph_ratio": p.list_vs_paragraph_ratio,
        "heading_depth_distribution": {str(k): v for k, v in p.heading_depth_distribution.items()},
        "canonical_terminology": list(p.canonical_terminology),
        "register_score": p.register_score,
        "soft_threshold": p.soft_threshold,
        "hard_threshold": p.hard_threshold,
        "iteration_cap": p.iteration_cap,
    }


def _profile_from_jsonable(d: dict) -> SectionProfile:
    return SectionProfile(
        sentence_length=DistributionStats(**(d.get("sentence_length") or {})),
        paragraph_length=DistributionStats(**(d.get("paragraph_length") or {})),
        hedging_density=float(d.get("hedging_density", 0.0)),
        hedging_markers=list(d.get("hedging_markers") or []),
        adverb_density=float(d.get("adverb_density", 0.0)),
        citation_density=float(d.get("citation_density", 0.0)),
        voice_ratio=float(d.get("voice_ratio", 0.0)),
        list_vs_paragraph_ratio=float(d.get("list_vs_paragraph_ratio", 0.0)),
        heading_depth_distribution={
            int(k): int(v) for k, v in (d.get("heading_depth_distribution") or {}).items()
        },
        canonical_terminology=list(d.get("canonical_terminology") or []),
        register_score=float(d.get("register_score", 0.0)),
        soft_threshold=float(d.get("soft_threshold", 0.7)),
        hard_threshold=float(d.get("hard_threshold", 1.0)),
        iteration_cap=int(d.get("iteration_cap", 5)),
    )
