"""Hard-rejection rules for finding.file (and re-checked at finding.sign_off).

Each rule is a function that returns a :class:`Rejection` or None.
Validator runs ALL rules and returns the full list — operator/agent
sees every issue, not just the first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pathlib import Path

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.binder import sources as _sources
from ara.runtime.chunks.freshness import evidence_in_current_activation
from ara.runtime.facts.store import FactStore
from ara.runtime.findings.alphabets import Alphabets
from ara.runtime.findings.record import Finding
from ara.runtime.leads.store import LeadStore

# Impact axes are pack-declared. The validator looks for an
# ``impact_axes`` attribute on ValidatorContext (a list of (name, min, max)
# tuples) and falls back to the offsec CIA defaults if absent.
_DEFAULT_IMPACT_AXES = [
    ("confidentiality", 0, 3),
    ("integrity", 0, 3),
    ("availability", 0, 3),
]


@dataclass(frozen=True)
class Rejection:
    rule: str
    message: str


@dataclass
class ValidatorContext:
    alphabets: Alphabets
    binder: EvidenceBinder
    fact_store: FactStore
    lead_store: LeadStore
    activation_id: str | None = None
    in_scope_assets: set[str] | None = None  # populated from weapon LINES if available
    audit_log_path: Path | None = None       # session events.jsonl, for freshness rule
    enforce_freshness: bool = True           # off → tolerate stale evidence (e.g. operator-input findings)
    impact_axes: list[tuple[str, int, int]] | None = None  # pack-declared (name, min, max); defaults to offsec CIA
    field_expectations: dict | None = None  # pack-declared per-field content bar; defaults to _DEFAULT_FIELD_EXPECTATIONS


# ---------- rules ----------

def _rule_title_length(body: dict, _ctx: ValidatorContext) -> Rejection | None:
    if len(body.get("title") or "") > 120:
        return Rejection("TITLE_LENGTH", "title exceeds 120 chars")
    if not (body.get("title") or "").strip():
        return Rejection("TITLE_LENGTH", "title is empty")
    return None


def _rule_category_in_enum(body: dict, ctx: ValidatorContext) -> Rejection | None:
    cat = body.get("category", "")
    if not ctx.alphabets.categories.entries:
        # Empty alphabet — alphabets not yet populated; tolerated for v1
        return None
    if not ctx.alphabets.categories.has(cat):
        # Advisory (forensic) — the channel coerces an unknown category to
        # "other" with category_proposed set; if the enum has no "other" we
        # tolerate the model's word rather than reject the whole finding.
        return None
    return None


def _rule_category_other_needs_proposed(body: dict, ctx: ValidatorContext) -> Rejection | None:
    cat = body.get("category", "")
    if ctx.alphabets.categories.is_other(cat):
        if not (body.get("category_proposed") or "").strip():
            return Rejection("CATEGORY_OTHER_NEEDS_PROPOSED",
                             "category=other requires category_proposed")
    return None


def _rule_severity_cell_valid(body: dict, ctx: ValidatorContext) -> Rejection | None:
    cell = body.get("severity_cell")
    # OPTIONAL forensic field: a natural finding gives a severity *word*
    # (normalized to a score), not a rubric cell. Only validate a cell that is
    # actually present.
    if cell is None or cell == [] or cell == ():
        return None
    if not (isinstance(cell, (list, tuple)) and len(cell) == 2):
        return Rejection("SEVERITY_CELL_VALID",
                         "severity_cell must be (data_class, access)")
    if not ctx.alphabets.rubric.matrix:
        # Empty rubric — tolerated for v1 (caller may set severity_score directly)
        return None
    dc, ax = cell
    if not ctx.alphabets.rubric.has_cell(dc, ax):
        return Rejection("SEVERITY_CELL_VALID",
                         f"({dc}, {ax}) not in rubric matrix")
    return None


def _rule_evidence_refs_resolve(body: dict, ctx: ValidatorContext) -> Rejection | None:
    missing = [
        e for e in (body.get("evidence_refs") or [])
        if ctx.binder.get_record(e) is None
    ]
    if missing:
        return Rejection("EVIDENCE_REFS_RESOLVE",
                         f"unresolved evidence: {missing}")
    return None


def _rule_primary_evidence_response_bearing(
    body: dict, ctx: ValidatorContext
) -> Rejection | None:
    """Primary-evidence gate: when a finding cites evidence directly, at least one
    cited ref must resolve to a RESPONSE-BEARING source — the actual output/
    response that substantiates the claim, not a command-only ``shim:spawn``. A
    finding that cites only a lead defers to that lead's own primary. This retires
    the 'evidence is just the command' class (e.g. F:95b00a3e88)."""
    refs = body.get("evidence_refs") or []
    if not refs:
        return None  # no direct evidence → covered by lead_ref path / other rules
    for e in refs:
        rec = ctx.binder.get_record(e)
        if rec is None:
            continue  # unresolved refs handled by EVIDENCE_REFS_RESOLVE
        if _sources.is_response_bearing(rec.source or ""):
            return None
    return Rejection(
        "PRIMARY_EVIDENCE_RESPONSE_BEARING",
        "cited evidence is command/connection-only (no response-bearing record); "
        "cite the tool output or HTTP response that substantiates the claim "
        "(source tool:*, mitm:request, or shim:spawn_output)",
    )


def _rule_fact_refs_exist(body: dict, ctx: ValidatorContext) -> Rejection | None:
    missing = [
        f for f in (body.get("fact_refs") or [])
        if ctx.fact_store.by_id(f) is None
    ]
    if missing:
        return Rejection("FACT_REFS_EXIST", f"facts not found: {missing}")
    return None


def _rule_lead_refs_valid_status(body: dict, ctx: ValidatorContext) -> Rejection | None:
    valid_statuses = {"open", "investigating", "promoted"}
    bad: list[str] = []
    for lid in body.get("lead_refs") or []:
        ld = ctx.lead_store.by_id(lid)
        if ld is None:
            bad.append(f"{lid}:not_found")
            continue
        if ld.status not in valid_statuses:
            bad.append(f"{lid}:status={ld.status}")
    if bad:
        return Rejection("LEAD_REFS_VALID_STATUS", f"bad lead refs: {bad}")
    return None


def _rule_origin_agent_requires_lead_refs(body: dict, _ctx: ValidatorContext) -> Rejection | None:
    # Relaxed: an agent finding must show provenance, but cited EVIDENCE now
    # satisfies that just as a lead_ref does (the mechanical core requires
    # evidence anyway). Only reject when neither a lead nor evidence is cited.
    if body.get("origin") == "agent_derived":
        if not body.get("lead_refs") and not body.get("evidence_refs"):
            return Rejection("ORIGIN_AGENT_REQUIRES_LEAD_REFS",
                             "agent_derived findings must cite ≥1 lead_ref or evidence_ref")
    return None


def _rule_fix_template_or_custom(body: dict, _ctx: ValidatorContext) -> Rejection | None:
    # Relaxed: a fix is OPTIONAL forensic detail. Only reject the contradiction
    # of specifying BOTH a template and a custom fix; neither is fine.
    has_t = bool(body.get("fix_template_id"))
    has_c = bool(body.get("fix_custom"))
    if has_t and has_c:
        return Rejection("FIX_TEMPLATE_OR_CUSTOM",
                         "specify at most one of fix_template_id or fix_custom")
    return None


def _rule_fix_template_exists(body: dict, ctx: ValidatorContext) -> Rejection | None:
    fid = body.get("fix_template_id")
    if not fid:
        return None
    if not ctx.alphabets.fix_library.templates:
        return None  # empty library — tolerated for v1
    if not ctx.alphabets.fix_library.has(fid):
        return Rejection("FIX_TEMPLATE_EXISTS",
                         f"fix_template_id {fid!r} not in fix library")
    return None


def _rule_fix_template_applicable(body: dict, ctx: ValidatorContext) -> Rejection | None:
    fid = body.get("fix_template_id")
    cat = body.get("category", "")
    if not fid or not ctx.alphabets.fix_library.has(fid):
        return None
    if not ctx.alphabets.fix_library.applicable(fid, cat):
        return Rejection("FIX_TEMPLATE_APPLICABLE",
                         f"fix_template {fid!r} not applicable to category {cat!r}")
    return None


def _rule_fix_custom_has_verification(body: dict, _ctx: ValidatorContext) -> Rejection | None:
    fc = body.get("fix_custom")
    if fc is None:
        return None
    if not (isinstance(fc, dict) and (fc.get("verification_method") or "").strip()):
        return Rejection("FIX_CUSTOM_HAS_VERIFICATION",
                         "fix_custom must include verification_method")
    return None


def _rule_provenance_chain_valid(body: dict, _ctx: ValidatorContext) -> Rejection | None:
    # OPTIONAL forensic field — provenance is reconstructable from the cited
    # evidence/leads; an empty chain no longer blocks a finding.
    return None


def _rule_impact_fields_valid(body: dict, ctx: ValidatorContext) -> Rejection | None:
    """Pack-driven impact validation.

    The pack's impact alphabet (vector shape) declares which axes a
    finding's ``impact`` field must cover and the int range per axis.
    Offsec ships ``confidentiality / integrity / availability`` 0..3;
    a code-review pack might ship ``correctness / maintainability /
    performance``.

    Falls back to offsec CIA defaults when ctx.impact_axes is None
    (back-compat with callers that don't propagate pack info).
    """
    axes = ctx.impact_axes if ctx.impact_axes is not None else _DEFAULT_IMPACT_AXES
    expected_keys = {name for name, _, _ in axes}
    impact = body.get("impact") or {}
    # OPTIONAL forensic field: a natural finding often omits the structured
    # impact vector. Only validate the shape when the agent actually provides
    # one; an absent/empty impact no longer blocks the finding.
    if not impact:
        return None
    # A readable ``summary`` is a valid impact on its own (the Spoils / report
    # field). Validate only the CIA axes that are actually present.
    axis_impact = {k: v for k, v in impact.items() if k != "summary"}
    if not axis_impact:
        return None
    actual_keys = set(axis_impact)
    if actual_keys != expected_keys:
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if extra:
            parts.append(f"unexpected {sorted(extra)}")
        return Rejection(
            "IMPACT_FIELDS_VALID",
            f"impact axis mismatch: {', '.join(parts)}",
        )
    by_name = {name: (lo, hi) for name, lo, hi in axes}
    for k, v in axis_impact.items():
        lo, hi = by_name[k]
        if not isinstance(v, int) or v < lo or v > hi:
            return Rejection(
                "IMPACT_FIELDS_VALID",
                f"impact.{k} must be int in {lo}..{hi} (got {v!r})",
            )
    return None


def _rule_evidence_freshness(body: dict, ctx: ValidatorContext) -> Rejection | None:
    """2.3 freshness: at least one evidence_ref must trace to a capture
    or evidence.get/search call made during the current activation.

    Skipped when:
    - ``enforce_freshness=False`` on the context (operator-input findings)
    - no ``activation_id`` known (legacy / no chunk runtime)
    - the finding is mat_derived or operator_input (those don't run
      against live evidence by definition)
    """
    if not ctx.enforce_freshness:
        return None
    if not ctx.activation_id:
        return None
    origin = body.get("origin", "")
    if origin in ("mat_derived", "operator_input"):
        return None
    refs = body.get("evidence_refs") or []
    if not refs:
        return None  # other rules already require ≥1 evidence; not our job
    fresh = evidence_in_current_activation(
        body,
        binder=ctx.binder,
        audit_log_path=ctx.audit_log_path,
        current_activation_id=ctx.activation_id,
    )
    if not fresh:
        return Rejection(
            "EVIDENCE_FRESHNESS_RULE",
            f"no evidence_ref captured or accessed in current activation "
            f"({ctx.activation_id})",
        )
    return None


def _rule_affected_assets_in_scope(body: dict, ctx: ValidatorContext) -> Rejection | None:
    assets = body.get("affected_assets") or []
    if ctx.in_scope_assets is None:
        return None  # weapon scope not configured — tolerated
    out_of_scope = [a for a in assets if a not in ctx.in_scope_assets]
    if out_of_scope:
        return Rejection("AFFECTED_ASSETS_IN_SCOPE",
                         f"out-of-scope assets: {out_of_scope}")
    return None


_AUTH_UNAUTH_CLAIM_MARKERS = (
    "unauthenticat", "without auth", "without authentication", "no authentication",
    "no login", "not require auth", "does not require auth", "unauthorized access",
    "bypass auth", "auth bypass", "pre-auth", "without logging in",
    "without credentials", "no credentials", "anonymous access", "publicly accessible",
    "no authorization", "without authorization",
)
# Conservative, login-page-specific markers. Kept HTML-form-specific so a JSON
# data leak (which may legitimately contain the word "password" as a field) is
# NOT falsely refuted — only an actual login form trips these.
_LOGIN_FORM_MARKERS = (
    'type="password"', "type='password'", 'name="password"', "please log in",
    "please sign in", "log in to your account", "sign in to continue",
    "enter your password", "login required", "authentication required",
    "sign in to your account",
)


def _derive_verification_markers(claim: str) -> tuple[list[str], list[str]]:
    """Auto-derive checkable markers from a prose claim (the model's
    ``confirmation_method``). must_contain = distinctive identifiers the claim
    cites (URL paths, quoted strings, an ``@`` when it mentions email) that
    should appear in the cited evidence; must_not_contain = login-form markers
    when the claim asserts something is *unauthenticated* (the /admin
    false-positive guard). Best-effort — callers treat derived must_contain
    leniently (≥1 present) and derived must_not_contain strictly.
    """
    import re
    text = claim or ""
    low = text.lower()
    must_contain: list[str] = []
    for m in re.findall(r"/[A-Za-z0-9_][A-Za-z0-9_\-./]{2,}", text):
        if m not in must_contain:
            must_contain.append(m)
    for m in re.findall(r"""["']([A-Za-z0-9_@:/.\- ]{3,40})["']""", text):
        m = m.strip()
        if m and m not in must_contain:
            must_contain.append(m)
    if "email" in low and not any("@" in x for x in must_contain):
        must_contain.append("@")
    must_not_contain: list[str] = []
    if any(k in low for k in _AUTH_UNAUTH_CLAIM_MARKERS):
        must_not_contain = list(_LOGIN_FORM_MARKERS)
    return must_contain[:8], must_not_contain


def _rule_claim_verification(body: dict, ctx: ValidatorContext) -> Rejection | None:
    """Mechanical claim-verification gate.

    A mechanically-testable finding must carry a ``verification`` object
    asserting markers its cited evidence must / must not contain, and that
    assertion must actually hold against the cited evidence *bytes*. This is
    the gate that kills evidence-backed-but-wrong findings — e.g. an
    "unauthenticated /admin access" claim whose own captured response contains
    a login form. The check is deterministic and runs on ``finding.file`` (and
    is re-checked at sign-off); it does not trust the agent's narration.

    Findings honestly marked ``not_mechanically_testable`` are exempt here (a
    separate lifecycle flags them as unverified rather than confirmed).
    """
    if body.get("not_mechanically_testable"):
        return None
    verification = body.get("verification")
    if isinstance(verification, str):
        verification = {"claim": verification}
    if not isinstance(verification, dict):
        verification = {}
    explicit_mc = [str(x) for x in (verification.get("must_contain") or [])]
    explicit_mnc = [str(x) for x in (verification.get("must_not_contain") or [])]
    # Prose claim to auto-derive from (falls back to the natural fields).
    claim = str(verification.get("claim") or "").strip()
    if not claim:
        claim = str(body.get("confirmation_method")
                    or body.get("description") or "").strip()

    derived = False
    must_contain, must_not_contain = explicit_mc, explicit_mnc
    if not explicit_mc and not explicit_mnc:
        if not claim:
            return Rejection(
                "CLAIM_VERIFICATION_MISSING",
                "mechanically-testable finding must include a 'verification' "
                "object (explicit must_contain/must_not_contain markers, or a "
                "'claim'/confirmation_method to auto-derive them from) — or set "
                "not_mechanically_testable=true",
            )
        must_contain, must_not_contain = _derive_verification_markers(claim)
        derived = True

    refs = [str(r) for r in (body.get("evidence_refs") or [])]
    if not refs:
        return Rejection(
            "CLAIM_VERIFICATION_NO_EVIDENCE",
            "verification requires evidence_refs to check the claim against",
        )
    blobs: list[str] = []
    for ref in refs:
        try:
            data = ctx.binder.get(ref)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, bytes):
            blobs.append(data.decode("utf-8", errors="replace"))
        elif data:
            blobs.append(str(data))
    if not blobs:
        return Rejection(
            "CLAIM_VERIFICATION_EVIDENCE_UNREADABLE",
            "could not read any cited evidence to verify the claim",
        )
    haystack = "\n".join(blobs).lower()

    # must_not_contain (contradiction) is enforced strictly whether explicit or
    # derived — this is the anti-false-positive guard (login form on a page
    # claimed unauthenticated).
    for marker in must_not_contain:
        if marker and marker.lower() in haystack:
            return Rejection(
                "CLAIM_VERIFICATION_REFUTED",
                f"cited evidence contains disqualifying marker {marker!r} — the "
                "claim is refuted by its own evidence (e.g. a login form on a "
                "page claimed to be unauthenticated)",
            )

    if not derived:
        # Explicit markers: every required marker must be present (strict).
        for marker in must_contain:
            if marker and marker.lower() not in haystack:
                return Rejection(
                    "CLAIM_VERIFICATION_FAILED",
                    f"cited evidence does not contain required marker {marker!r} "
                    "— the evidence does not demonstrate the claim",
                )
        return None

    # Derived markers are best-effort guesses: require AT LEAST ONE of the
    # distinctive tokens to appear in the evidence (evidence is actually about
    # the claim). If none could be derived, fall back to reproducibility alone
    # (evidence retrievable + non-empty, already established) rather than
    # false-reject a prose-only claim.
    if must_contain:
        if not any(m and m.lower() in haystack for m in must_contain):
            return Rejection(
                "CLAIM_VERIFICATION_FAILED",
                "none of the claim's distinctive tokens "
                f"{must_contain!r} appear in the cited evidence — the evidence "
                "does not demonstrate the claim",
            )
    return None


_RULES: list[tuple[str, Callable[[dict, ValidatorContext], Any]]] = [
    ("TITLE_LENGTH", _rule_title_length),
    ("CATEGORY_IN_ENUM", _rule_category_in_enum),
    ("CATEGORY_OTHER_NEEDS_PROPOSED", _rule_category_other_needs_proposed),
    ("SEVERITY_CELL_VALID", _rule_severity_cell_valid),
    ("EVIDENCE_REFS_RESOLVE", _rule_evidence_refs_resolve),
    ("PRIMARY_EVIDENCE_RESPONSE_BEARING", _rule_primary_evidence_response_bearing),
    ("FACT_REFS_EXIST", _rule_fact_refs_exist),
    ("LEAD_REFS_VALID_STATUS", _rule_lead_refs_valid_status),
    ("ORIGIN_AGENT_REQUIRES_LEAD_REFS", _rule_origin_agent_requires_lead_refs),
    ("FIX_TEMPLATE_OR_CUSTOM", _rule_fix_template_or_custom),
    ("FIX_TEMPLATE_EXISTS", _rule_fix_template_exists),
    ("FIX_TEMPLATE_APPLICABLE", _rule_fix_template_applicable),
    ("FIX_CUSTOM_HAS_VERIFICATION", _rule_fix_custom_has_verification),
    ("PROVENANCE_CHAIN_VALID", _rule_provenance_chain_valid),
    ("IMPACT_FIELDS_VALID", _rule_impact_fields_valid),
    ("AFFECTED_ASSETS_IN_SCOPE", _rule_affected_assets_in_scope),
    ("EVIDENCE_FRESHNESS_RULE", _rule_evidence_freshness),
    ("CLAIM_VERIFICATION", _rule_claim_verification),
]


def validate_for_draft(body: dict, ctx: ValidatorContext) -> list[Rejection]:
    out: list[Rejection] = []
    for _, fn in _RULES:
        result = fn(body, ctx)
        if result is not None:
            out.append(result)
    return out


# ── Sign-off (promotion) gate ───────────────────────────────────────────────
# Draft findings may be incomplete (work in progress). PROMOTING one to a
# signed-off finding mechanically REQUIRES the full evidentiary record: the
# reason, the confirmed impact, a remediation/fix, the affected assets, how it
# was confirmed, and raw evidence. These block sign-off; they never block a draft.

def _require_reason(body: dict, _ctx: ValidatorContext) -> "Rejection | None":
    if not (str(body.get("notes") or "").strip()
            or str(body.get("title") or "").strip()):
        return Rejection("SIGNOFF_REASON_REQUIRED",
                         "promoted finding must state the reason / description")
    return None


def _require_impact(body: dict, _ctx: ValidatorContext) -> "Rejection | None":
    if not (body.get("impact") or {}):
        return Rejection(
            "SIGNOFF_IMPACT_REQUIRED",
            "promoted finding must carry a structured impact (not just notes)")
    return None


def _require_fix(body: dict, _ctx: ValidatorContext) -> "Rejection | None":
    if not (body.get("fix_custom") or body.get("fix_template_id")):
        return Rejection("SIGNOFF_FIX_REQUIRED",
                         "promoted finding must include a remediation / fix")
    return None


def _require_affected_assets(body: dict, _ctx: ValidatorContext) -> "Rejection | None":
    if not (body.get("affected_assets") or []):
        return Rejection("SIGNOFF_AFFECTED_ASSETS_REQUIRED",
                         "promoted finding must list affected_assets")
    return None


def _require_confirmation(body: dict, _ctx: ValidatorContext) -> "Rejection | None":
    v = body.get("verification") or {}
    fc = body.get("fix_custom") or {}
    has_v = bool(v) or (isinstance(fc, dict)
                        and bool(str(fc.get("verification_method") or "").strip()))
    if not has_v:
        return Rejection(
            "SIGNOFF_CONFIRMATION_REQUIRED",
            "promoted finding must record how it was confirmed (verification / "
            "claim)")
    return None


def _require_evidence(body: dict, _ctx: ValidatorContext) -> "Rejection | None":
    if not (body.get("evidence_refs") or []):
        return Rejection(
            "SIGNOFF_EVIDENCE_REQUIRED",
            "promoted finding must cite raw evidence (logs / output / artifacts)")
    return None


_SIGNOFF_REQUIRED = (
    _require_reason, _require_impact, _require_fix,
    _require_affected_assets, _require_confirmation, _require_evidence,
)


# ── Pack-declared per-field CONTENT expectations ────────────────────────────
# Presence is enforced by _SIGNOFF_REQUIRED; these check the CONTENTS meet a
# mechanical bar. Deterministic only (length / keyword / list / in-scope) — no
# LLM judgment. Packs override via alphabets/field_expectations.json; this is
# the baseline applied when a pack ships none.
_DEFAULT_FIELD_EXPECTATIONS: dict = {
    "impact": {
        "min_chars": 40,
        "must_match_any": [
            "gain", "read", "write", "execute", "run", "exfiltrat", "escalat",
            "bypass", "denial", "disclos", "compromise", "control", "access",
            "leak", "overwrite", "delete", "modify", "impersonat", "takeover",
            "inject", "steal", "reset", "brick",
        ],
    },
    "remediation": {"min_chars": 25},
    "confirmation": {"min_chars": 20},
    # affected_assets: presence is required by the gate; opt-in in-scope check
    # only fires when a pack declares "each_in_scope": true.
    "affected_assets": {},
}


def _field_readable(body: dict, field: str) -> str:
    """Extract the readable value of a finding field (mirrors the Spoils view)."""
    if field == "impact":
        imp = body.get("impact")
        if isinstance(imp, dict):
            if imp.get("summary"):
                return str(imp["summary"])
            return ", ".join(
                f"{k}={imp[k]}" for k in
                ("confidentiality", "integrity", "availability") if k in imp)
        return str(imp or "")
    if field == "remediation":
        fc = body.get("fix_custom")
        if isinstance(fc, dict):
            return str(fc.get("summary") or fc.get("what_changed")
                       or fc.get("why_it_fixes") or "")
        return str(body.get("fix_template_id") or "")
    if field == "confirmation":
        v = body.get("verification")
        fc = body.get("fix_custom")
        claim = v.get("claim") if isinstance(v, dict) else ""
        vm = fc.get("verification_method") if isinstance(fc, dict) else ""
        return str(claim or vm or "")
    if field == "reason":
        return str(body.get("notes") or body.get("title") or "")
    return ""


def _asset_in_scope(asset: str, in_scope: "set[str]") -> bool:
    a = str(asset).lower()
    for s in in_scope:
        host = (str(s).lower().replace("https://", "").replace("http://", "")
                .split("/")[0].split(":")[0].strip())
        if host and host in a:
            return True
    return False


def _field_expectation_rejections(
        body: dict, ctx: ValidatorContext) -> list[Rejection]:
    exp = getattr(ctx, "field_expectations", None) or _DEFAULT_FIELD_EXPECTATIONS
    out: list[Rejection] = []
    for field, spec in exp.items():
        if not isinstance(spec, dict):
            continue
        if field == "affected_assets":
            assets = body.get("affected_assets") or []
            if (assets and spec.get("each_in_scope") and ctx.in_scope_assets):
                bad = [a for a in assets
                       if not _asset_in_scope(a, ctx.in_scope_assets)]
                if bad:
                    out.append(Rejection(
                        "FIELD_AFFECTED_ASSETS_SCOPE",
                        f"affected_assets not in engagement scope: {bad[:3]}"))
            continue
        text = _field_readable(body, field).strip()
        if not text:
            continue  # empties are the presence gate's job, not the quality bar
        mc = int(spec.get("min_chars", 0) or 0)
        if mc and len(text) < mc:
            out.append(Rejection(
                f"FIELD_{field.upper()}_TOO_SHORT",
                f"{field} is too vague (< {mc} chars) — state concrete detail"))
            continue
        kws = spec.get("must_match_any")
        if kws and not any(str(k).lower() in text.lower() for k in kws):
            out.append(Rejection(
                f"FIELD_{field.upper()}_VAGUE",
                f"{field} must name a concrete effect "
                f"(e.g. {', '.join(str(k) for k in kws[:5])}…)"))
    return out


def validate_for_signoff(finding: Finding, ctx: ValidatorContext) -> list[Rejection]:
    body = finding.to_jsonable()
    out = validate_for_draft(body, ctx)
    for fn in _SIGNOFF_REQUIRED:
        r = fn(body, ctx)
        if r is not None:
            out.append(r)
    out.extend(_field_expectation_rejections(body, ctx))
    return out
