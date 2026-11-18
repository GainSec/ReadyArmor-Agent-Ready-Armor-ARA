"""Deterministic feature extraction for SectionProfile."""
from __future__ import annotations

import re
from collections import Counter

from ara.runtime.style.profile import DistributionStats, SectionProfile


_HEDGING_MARKERS = frozenset({
    "probably", "likely", "may", "might", "could", "perhaps", "possibly",
    "seems", "appears", "suggests", "suggestive", "potentially", "presumably",
    "arguably", "tentatively", "approximately", "roughly", "somewhat",
    "generally", "typically", "often", "sometimes", "occasionally", "rarely",
    "presumed", "assumed", "ostensibly", "purportedly", "allegedly",
})

_COMMON_ADVERBS = frozenset({
    "actually", "basically", "carefully", "clearly", "completely", "easily",
    "effectively", "essentially", "exactly", "especially", "extremely",
    "finally", "frequently", "fully", "genuinely", "generally", "highly",
    "immediately", "importantly", "literally", "mainly", "merely", "mostly",
    "naturally", "necessarily", "normally", "obviously", "particularly",
    "perfectly", "physically", "practically", "primarily", "probably",
    "quickly", "really", "recently", "relatively", "remarkably", "roughly",
    "seriously", "significantly", "similarly", "simply", "slightly",
    "specifically", "strongly", "successfully", "suddenly", "supposedly",
    "surely", "totally", "typically", "ultimately", "usually", "virtually",
    "absolutely", "approximately", "consistently",
})

_FORMAL_MARKERS = frozenset({
    "therefore", "thus", "moreover", "furthermore", "however", "nevertheless",
    "consequently", "accordingly", "hence", "whereas", "subsequently",
    "henceforth", "concomitantly", "ergo", "ipso", "facto",
})

_INFORMAL_MARKERS = frozenset({
    "i", "me", "we", "you", "you're", "you'll", "don't", "can't", "won't",
    "isn't", "aren't", "shouldn't", "couldn't", "wouldn't", "i'm", "i'll",
    "i've", "let's", "ok", "okay",
})

_CITATION_RE = re.compile(r"\b(F:[a-f0-9]{10}|f:[a-f0-9]{10}|L:[a-f0-9]{10}|e:[a-f0-9]{10})\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s+", re.M)
_LIST_LINE_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+", re.M)
_PASSIVE_RE = re.compile(
    r"\b(was|were|been|being|is|are|am)\s+(\w+ed|\w+en)\b", re.I
)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_'\-]+", text.lower())


def _split_sentences(text: str) -> list[str]:
    if not text.strip():
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return [p for p in parts if p.strip()]


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _dist_stats(values: list[int]) -> DistributionStats:
    if not values:
        return DistributionStats()
    sorted_v = sorted(values)
    n = len(sorted_v)

    def pct(p: float) -> float:
        if n == 0:
            return 0.0
        idx = max(0, min(n - 1, int(round(p * (n - 1)))))
        return float(sorted_v[idx])

    mean = sum(sorted_v) / n
    var = sum((v - mean) ** 2 for v in sorted_v) / n
    std = var ** 0.5
    return DistributionStats(
        mean=mean, std=std,
        p25=pct(0.25), p50=pct(0.50), p75=pct(0.75),
        n_samples=n,
    )


def _hedging_count(words: list[str]) -> int:
    return sum(1 for w in words if w in _HEDGING_MARKERS)


def _hedging_markers_found(words: list[str]) -> list[str]:
    counts: Counter = Counter(w for w in words if w in _HEDGING_MARKERS)
    return [w for w, _ in counts.most_common(20)]


def _adverb_count(words: list[str]) -> int:
    return sum(1 for w in words if w in _COMMON_ADVERBS or w.endswith("ly"))


def _passive_ratio(sentences: list[str]) -> float:
    if not sentences:
        return 0.0
    passive = sum(1 for s in sentences if _PASSIVE_RE.search(s))
    return passive / len(sentences)


def _list_vs_paragraph(text: str) -> float:
    list_lines = len(_LIST_LINE_RE.findall(text))
    para_lines = len([p for p in text.splitlines() if p.strip()])
    if para_lines == 0:
        return 0.0
    return list_lines / para_lines


def _heading_depths(text: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for m in _HEADING_RE.finditer(text):
        depth = len(m.group(1))
        out[depth] = out.get(depth, 0) + 1
    return out


def _citation_density(text: str, n_words: int) -> float:
    if n_words == 0:
        return 0.0
    return len(_CITATION_RE.findall(text)) / n_words * 100.0


def _register_score(words: list[str]) -> float:
    if not words:
        return 0.5
    formal = sum(1 for w in words if w in _FORMAL_MARKERS)
    informal = sum(1 for w in words if w in _INFORMAL_MARKERS)
    if formal + informal == 0:
        return 0.5
    return formal / (formal + informal)


def _canonical_terms(text: str) -> list[str]:
    """Pick noun phrases that occur >=3 times. Crude — counts capitalized
    bigrams + lowercase repeated words.
    """
    bigrams = re.findall(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b", text)
    counts: Counter = Counter(bigrams)
    return [b for b, n in counts.items() if n >= 2][:30]


def extract_section_profile(text: str, *, section_name: str = "") -> SectionProfile:
    sentences = _split_sentences(text)
    paragraphs = _split_paragraphs(text)
    words = _tokenize(text)
    n_words = max(1, len(words))

    return SectionProfile(
        sentence_length=_dist_stats([len(s.split()) for s in sentences]),
        paragraph_length=_dist_stats([len(p.split()) for p in paragraphs]),
        hedging_density=_hedging_count(words) / n_words * 100.0,
        hedging_markers=_hedging_markers_found(words),
        adverb_density=_adverb_count(words) / n_words * 100.0,
        citation_density=_citation_density(text, n_words),
        voice_ratio=_passive_ratio(sentences),
        list_vs_paragraph_ratio=_list_vs_paragraph(text),
        heading_depth_distribution=_heading_depths(text),
        canonical_terminology=_canonical_terms(text),
        register_score=_register_score(words),
    )
