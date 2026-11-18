"""Forge-time alphabet auto-extractor.

Today the agent records ``extract.conc`` events by hand to populate
the offsec pack's alphabets (category / severity_rubric /
fix_library). This module produces the same events automatically by
reading REPORT_SECTION manifest elements and pattern-matching their
text against pack-known cues.

Architecture:

- :class:`AutoExtractor` is the protocol. Each pack ships its own
  extractor. v1 ships :class:`HeuristicOffsecExtractor` for the
  built-in offsec pack — pure regex, deterministic, no LLM.
- :func:`run_auto_extract` is the orchestrator: iterates the forge
  session's REPORT_SECTION manifest elements, asks the extractor for
  CONC bodies per element, calls
  ``forge.phases.exhaust.record_extraction`` for each.

v1.5 will add an LLM-driven extractor (same protocol, different
implementation) that dispatches via the supervisor's channel_call IPC.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from ara.forge import state as fstate
from ara.forge.alphabets.pack import (
    AlphabetPack,
    load_builtin_pack,
)
from ara.forge.manifest.element import (
    ManifestElement,
    ManifestKind,
    read_manifest,
)
from ara.forge.phases.exhaust import record_extraction
from ara.forge.session import ForgePhase, ForgeSession


@dataclass(frozen=True)
class ExtractedConcBody:
    """One auto-extracted CONC body — what would be the body of an
    ``extract.conc`` event. Carries the source manifest_element_id so
    the orchestrator's record_extraction call has citation provenance.
    """
    alphabet_name: str
    body: dict          # entry / coords+cell / etc., matching the alphabet's shape
    source_element_id: str


class AutoExtractor(Protocol):
    """Protocol every pack's auto-extractor satisfies."""

    pack_name: str

    def extract(
        self, *,
        elements: list[ManifestElement],
        mats_dir: Path,
        pack: AlphabetPack,
    ) -> list[ExtractedConcBody]: ...


# ---------- offsec heuristic extractor ----------

# Category keyword → canonical id. Order matters: more specific first
# so "xss.reflected" wins over "xss". Mapping intentionally limited to
# the offsec pack's starter vocab; unknowns fall through to "other".
_CATEGORY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"reflected.{0,20}xss|xss.{0,20}reflected", re.I), "xss.reflected"),
    (re.compile(r"stored.{0,20}xss|xss.{0,20}stored|persistent.{0,20}xss", re.I), "xss.stored"),
    (re.compile(r"\bdom[\s-]?based\b.{0,20}xss|xss.{0,20}dom", re.I), "xss.dom"),
    (re.compile(r"\bsql\s*injection\b|\bsqli\b", re.I), "sqli"),
    (re.compile(r"\bidor\b|insecure direct object reference", re.I), "idor"),
    (re.compile(r"\bssrf\b|server[- ]side request forgery", re.I), "ssrf"),
    (re.compile(r"\bxxe\b|xml.{0,20}external.{0,20}entity", re.I), "xxe"),
    (re.compile(r"\bcsrf\b|cross[- ]site request forgery", re.I), "csrf"),
    (re.compile(r"prototype.{0,5}pollution", re.I), "prototype_pollution"),
    (re.compile(r"\bmfa\b.{0,20}bypass", re.I), "auth.mfa_bypass"),
    (re.compile(r"session.{0,5}fixation", re.I), "auth.session_fixation"),
    (re.compile(r"auth(entication)?.{0,20}bypass", re.I), "auth.bypass"),
    (re.compile(r"missing.{0,20}auth(orization|z)?\s*check|rbac.{0,20}miss", re.I),
     "rbac.missing_check"),
    (re.compile(r"secret.{0,20}(in|leak).{0,20}response", re.I), "secret.in_response"),
    (re.compile(r"secret.{0,20}(in|leak).{0,20}log", re.I), "secret.in_log"),
    (re.compile(r"secret.{0,20}(in|leak).{0,20}repo", re.I), "secret.in_repo"),
    (re.compile(r"\bdos\b.{0,20}amplif", re.I), "dos.amplification"),
    # Generic fallbacks (less specific):
    (re.compile(r"\bxss\b|cross[- ]site script", re.I), "xss.reflected"),
]

# Severity name → score table mirroring the offsec pack's bucket
# thresholds. Used when the extractor finds an explicit "Severity:
# High" without a CVSS score.
_SEVERITY_NAME_TO_SCORE = {
    "info": 0.5, "informational": 0.5,
    "low": 2.5, "medium": 5.0, "moderate": 5.0,
    "high": 8.0, "critical": 9.5,
}

_BUCKET_FOR_SCORE = [
    (9.0, "critical"), (7.0, "high"), (4.0, "medium"),
    (1.0, "low"), (0.0, "info"),
]


def _bucket_for(score: float) -> str:
    for thresh, name in _BUCKET_FOR_SCORE:
        if score >= thresh:
            return name
    return "info"


_SEVERITY_LINE_RE = re.compile(
    r"(?:^|\n)\s*\**\s*(severity|risk|impact rating)\s*:?\s*\**\s*"
    r"([A-Za-z]+)",
    re.I,
)
_CVSS_RE = re.compile(r"(?:cvss|score)\s*:?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


# Data-class hints in finding bodies — heuristic only. Order matters.
_DATA_CLASS_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcredential|password|api[\s_]?key|secret\b", re.I), "credentials"),
    (re.compile(r"\bPII\b|personally identifiable|email|phone|name|address", re.I), "PII"),
    (re.compile(r"infra(structure)?|metadata|topology|ssh\s*key|dns", re.I), "infra_metadata"),
    (re.compile(r"internal\s*data|tenant\s*data", re.I), "internal_data"),
    (re.compile(r"public(ly)?\s*(visible|accessible|exposed)", re.I), "public_data"),
]

# Access hints — order matters; "admin" wins over "write" wins over "read"
# when multiple hints fire. Keywords kept loose; "admin" anywhere in
# the body is enough to escalate the cell.
_ACCESS_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(
        r"\badmin(istrator)?\b|privilege\s*escalation|account\s+takeover|"
        r"\broot\s*access\b|elevated\s+(privilege|access)",
        re.I,
    ), "admin"),
    (re.compile(r"(modify|write|update|delete|tamper|alter|inject)", re.I), "write"),
    (re.compile(r"(read|leak|expose|disclos|view|enumerate)", re.I), "read"),
]


_FIX_SECTION_RE = re.compile(
    r"(?:^|\n)\s*#{1,4}\s*(remediation|fix|mitigation|recommendation)\s*\n+"
    r"([\s\S]+?)(?=\n#{1,4}\s|\Z)",
    re.I,
)
_VERIFICATION_RE = re.compile(
    r"(?:^|\n)\s*#{1,4}\s*(verification|how to verify|test plan)\s*\n+"
    r"([\s\S]+?)(?=\n#{1,4}\s|\Z)",
    re.I,
)


def _fingerprint(text: str) -> str:
    """Short stable fingerprint for synthesizing ids from extracted text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


@dataclass
class HeuristicOffsecExtractor:
    """Pure-regex extractor for the built-in offsec pack.

    Reads REPORT_SECTION manifest elements (the example-report finding
    sections), pattern-matches each against the pack's category /
    severity / fix shapes. Deterministic; same mats produce the same
    output every time.

    Limitations (documented intentionally):
    - Doesn't infer novel categories; falls back to ``other`` with a
      proposed_id when no keyword pattern matches.
    - Severity cell mapping is best-effort; uneven mat phrasing yields
      conservative ``(internal_data, read)`` defaults.
    - Doesn't extract anti-patterns or alternative fixes; only one fix
      template per matched category.

    These limitations are why the protocol exists — operators who want
    higher-quality extraction can plug in an LLM-driven extractor later.
    """
    pack_name: str = "offsec"

    def extract(
        self, *,
        elements: list[ManifestElement],
        mats_dir: Path,
        pack: AlphabetPack,
    ) -> list[ExtractedConcBody]:
        out: list[ExtractedConcBody] = []
        for el in elements:
            if el.kind != ManifestKind.REPORT_SECTION:
                continue
            if el.dropped_by_operator:
                continue
            text = self._read_section_text(el, mats_dir)
            if not text.strip():
                continue
            heading = (el.metadata or {}).get("heading") or ""
            search_text = f"{heading}\n{text}"
            cat_id, cat_proposed = _resolve_category(search_text, pack)
            if cat_id is not None:
                entry: dict[str, Any] = {"id": cat_id}
                if cat_proposed:
                    entry["description"] = cat_proposed[:240]
                out.append(ExtractedConcBody(
                    alphabet_name="category",
                    body={"alphabet_name": "category", "entry": entry},
                    source_element_id=el.id,
                ))
            cell = _resolve_rubric_cell(search_text)
            if cell is not None:
                coords, cell_body = cell
                out.append(ExtractedConcBody(
                    alphabet_name="severity_rubric",
                    body={"alphabet_name": "severity_rubric",
                          "coords": coords, "cell": cell_body},
                    source_element_id=el.id,
                ))
            fix = _resolve_fix(search_text, cat_id)
            if fix is not None:
                out.append(ExtractedConcBody(
                    alphabet_name="fix_library",
                    body={"alphabet_name": "fix_library", "entry": fix},
                    source_element_id=el.id,
                ))
        return _dedupe(out)

    @staticmethod
    def _read_section_text(el: ManifestElement, mats_dir: Path) -> str:
        """Read the section's text by combining manifest's extracted_text
        with a re-read from the source mat when needed for the byte range."""
        if el.extracted_text and len(el.extracted_text) > 200:
            return el.extracted_text
        # Try reading from the mat for the full byte_range
        for candidate in mats_dir.glob("*"):
            if not candidate.is_file():
                continue
            if (candidate.name + ":") in (el.metadata or {}).get("filename", ""):
                pass
            try:
                data = candidate.read_bytes()
            except OSError:
                continue
            offset, length = el.byte_range
            if offset >= 0 and offset < len(data):
                slice_ = data[offset: offset + length]
                if hashlib.sha256(slice_).hexdigest() == el.sha256:
                    return slice_.decode("utf-8", errors="replace")
        return el.extracted_text or ""


def _resolve_category(text: str, pack: AlphabetPack) -> tuple[str | None, str | None]:
    """Returns (canonical_id, proposed_description_or_None).

    Falls back to pack's "other" when no keyword pattern matches AND
    the pack declares an "other" entry; in that case the returned
    description is the leading phrase of the heading for operator
    review during reforge.
    """
    for pat, cat_id in _CATEGORY_PATTERNS:
        if pat.search(text):
            return cat_id, None
    cat_alpha = pack.get("category")
    if cat_alpha is None:
        return None, None
    if any((e.get("id") == "other") for e in cat_alpha.starter):
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        return "other", first_line[:120]
    return None, None


def _resolve_rubric_cell(text: str) -> tuple[dict, dict] | None:
    """Returns ((coords, cell)) or None. coords keys are
    {data_class, access}; cell keys are {score, bucket}."""
    score: float | None = None
    bucket: str | None = None
    cvss_match = _CVSS_RE.search(text)
    if cvss_match:
        try:
            score = float(cvss_match.group(1))
            bucket = _bucket_for(score)
        except ValueError:
            pass
    if score is None:
        sev_match = _SEVERITY_LINE_RE.search(text)
        if sev_match:
            name = sev_match.group(2).lower()
            score = _SEVERITY_NAME_TO_SCORE.get(name)
            if score is not None:
                bucket = _bucket_for(score)
    if score is None or bucket is None:
        return None

    data_class = "internal_data"  # conservative default
    for pat, dc in _DATA_CLASS_HINTS:
        if pat.search(text):
            data_class = dc
            break
    access = "read"  # conservative default
    for pat, acc in _ACCESS_HINTS:
        if pat.search(text):
            access = acc
            break
    return (
        {"data_class": data_class, "access": access},
        {"score": score, "bucket": bucket},
    )


def _resolve_fix(text: str, category_id: str | None) -> dict | None:
    if category_id is None:
        return None
    m = _FIX_SECTION_RE.search(text)
    if m is None:
        return None
    body_text = m.group(2).strip()
    if not body_text:
        return None
    fp = _fingerprint(body_text)
    fix_id = f"fix.{category_id}.{fp}"
    return {
        "id": fix_id,
        "title": (body_text.splitlines()[0] or category_id)[:120],
        "applicable_categories": [category_id],
        "body": body_text[:2000],
    }


def _dedupe(items: list[ExtractedConcBody]) -> list[ExtractedConcBody]:
    """Drop later-duplicates by (alphabet_name, key). Two extractions
    over different elements that propose the same category id collapse;
    the first cite wins.
    """
    seen: set[tuple[str, str]] = set()
    out: list[ExtractedConcBody] = []
    for it in items:
        key: tuple[str, str]
        if it.alphabet_name == "category":
            key = ("category", it.body["entry"]["id"])
        elif it.alphabet_name == "severity_rubric":
            c = it.body["coords"]
            key = ("severity_rubric",
                   f"{c['data_class']}/{c['access']}")
        elif it.alphabet_name == "fix_library":
            key = ("fix_library", it.body["entry"]["id"])
        else:
            key = (it.alphabet_name, str(id(it)))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ---------- registry of pack → extractor ----------

_EXTRACTORS: dict[str, AutoExtractor] = {
    "offsec": HeuristicOffsecExtractor(),
}


def get_extractor(pack_name: str) -> AutoExtractor | None:
    return _EXTRACTORS.get(pack_name)


def register_extractor(extractor: AutoExtractor) -> None:
    _EXTRACTORS[extractor.pack_name] = extractor


# ---------- orchestrator ----------

@dataclass(frozen=True)
class AutoExtractReport:
    pack_name: str
    elements_scanned: int
    bodies_emitted: int
    extractions_recorded: int
    skipped_existing: int
    by_alphabet: dict[str, int]


def run_auto_extract(
    sess: ForgeSession, *,
    extractor: AutoExtractor | None = None,
    pack: AlphabetPack | None = None,
    skip_existing: bool = True,
) -> AutoExtractReport:
    """Run the auto-extractor for the given session's REPORT_SECTION
    elements; record an ``extract.conc`` event per emitted body.

    Must run while the session is in EXHAUST phase (same as manual
    record_extraction calls).

    ``skip_existing=True`` (default) skips alphabet keys that already
    have an extraction in the forge log — operators can re-run safely
    without duplicating events.
    """
    if sess.phase != ForgePhase.EXHAUST:
        raise ValueError(
            f"run_auto_extract requires EXHAUST phase "
            f"(current: {sess.phase.value})"
        )
    pack_name = sess.config.alphabet_pack
    if not pack_name:
        return AutoExtractReport(
            pack_name="", elements_scanned=0, bodies_emitted=0,
            extractions_recorded=0, skipped_existing=0, by_alphabet={},
        )
    if pack is None:
        from ara.forge.alphabets.pack import resolve_pack
        pack = resolve_pack(pack_name, sess.state_root)
    if extractor is None:
        extractor = get_extractor(pack_name)
        if extractor is None:
            raise ValueError(
                f"no auto-extractor registered for pack {pack_name!r}; "
                f"available: {sorted(_EXTRACTORS)}"
            )

    manifest_path = fstate.forge_manifest_path(sess.state_root, sess.forge_id)
    elements = read_manifest(manifest_path)
    mats_dir = fstate.forge_mats_dir(sess.state_root, sess.forge_id)
    bodies = extractor.extract(
        elements=elements, mats_dir=mats_dir, pack=pack,
    )

    existing_keys = (
        _existing_alphabet_keys(sess) if skip_existing else set()
    )

    recorded = 0
    skipped = 0
    by_alphabet: dict[str, int] = {}
    for i, eb in enumerate(bodies):
        key = _body_key(eb.body)
        if key in existing_keys:
            skipped += 1
            continue
        record_extraction(
            sess,
            manifest_element_id=eb.source_element_id,
            channel="extract.conc",
            body=eb.body,
            extraction_id=_make_extraction_id(eb, i),
            citation={
                "manifest_element_id": eb.source_element_id,
                "extractor": getattr(extractor, "pack_name", "unknown"),
                "auto": True,
                "ts": time.time(),
            },
        )
        recorded += 1
        by_alphabet[eb.alphabet_name] = by_alphabet.get(eb.alphabet_name, 0) + 1
    return AutoExtractReport(
        pack_name=pack_name,
        elements_scanned=sum(
            1 for e in elements
            if e.kind == ManifestKind.REPORT_SECTION and not e.dropped_by_operator
        ),
        bodies_emitted=len(bodies),
        extractions_recorded=recorded,
        skipped_existing=skipped,
        by_alphabet=by_alphabet,
    )


def _existing_alphabet_keys(sess: ForgeSession) -> set[tuple[str, str]]:
    """Pull (alphabet, key) tuples from the forge's already-recorded
    CONC extractions so re-runs don't duplicate."""
    from ara.forge.phases.exhaust import read_extractions
    out: set[tuple[str, str]] = set()
    for evt in read_extractions(sess.state_root, sess.forge_id):
        if evt.channel != "extract.conc":
            continue
        body = evt.body or {}
        name = body.get("alphabet_name")
        if not name:
            continue
        if name == "category" or name == "fix_library":
            entry = body.get("entry") or {}
            if entry.get("id"):
                out.add((name, entry["id"]))
        elif name == "severity_rubric":
            coords = body.get("coords") or {}
            dc = coords.get("data_class")
            ac = coords.get("access")
            if dc and ac:
                out.add((name, f"{dc}/{ac}"))
    return out


def _body_key(body: dict) -> tuple[str, str]:
    name = body["alphabet_name"]
    if name == "severity_rubric":
        c = body["coords"]
        return (name, f"{c['data_class']}/{c['access']}")
    entry = body.get("entry") or {}
    return (name, entry.get("id", ""))


def _make_extraction_id(eb: ExtractedConcBody, idx: int) -> str:
    h = hashlib.sha256(
        f"{eb.alphabet_name}:{eb.source_element_id}:{idx}".encode()
    ).hexdigest()[:10]
    return f"x_auto_{h}"


# Silence unused
_ = (Iterable,)
