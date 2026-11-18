"""Findings typed channels: file, sign_off, rescind, enrich, enrichment_review.

The ``FindingFileChannel`` runs the full validator (returns ALL rule
failures, not first-fail). Each non-empty failure list is raised as a
single :class:`ChannelRejection` with the joined messages.

When a finding is signed off, all referenced leads (status investigating
or promoted) are auto-confirmed via the ``lead.confirm`` channel.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, fields as _dc_fields
from pathlib import Path
from typing import Any

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection
from ara.runtime.facts.store import FactStore
from ara.runtime.findings.alphabets import Alphabets, load_alphabets
from ara.runtime.findings.record import (
    FieldEnrichment,
    Finding,
    compute_content_hash,
    mint_finding_id,
    severity_name_from_score,
)
from ara.runtime.findings.store import FindingStore
from ara.runtime.findings.validator import (
    ValidatorContext,
    validate_for_draft,
    validate_for_signoff,
)
from ara.runtime.leads.store import LeadStore


# ---------- bodies ----------

@dataclass
class FindingFileBody:
    title: str
    category: str = ""  # advisory; coerced to a valid enum value / "other"
    # Forensic-metadata fields — OPTIONAL. An LLM agent emits a *natural*
    # finding (title/severity/description/evidence); these structured fields
    # are derived or left empty rather than fabricated. The mechanical core
    # (evidence_refs resolve+fresh, CLAIM_VERIFICATION) stays strict.
    severity_cell: list | None = None  # [data_class, access]; optional
    impact: dict | None = None
    provenance_chain: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    fact_refs: list[str] = field(default_factory=list)
    lead_refs: list[str] = field(default_factory=list)
    affected_assets: list[str] = field(default_factory=list)
    fix_template_id: str | None = None
    fix_custom: dict | None = None
    origin: str = "agent_derived"
    category_proposed: str | None = None
    notes: str | None = None
    severity_score_override: float | None = None  # rare — bypass rubric for tests/manual
    # Natural fields the model actually emits (normalized in execute()):
    #   severity: a word ("low"/"medium"/"high"/"critical") -> score
    #   description: prose -> folded into notes
    #   confirmation_method: prose "how I confirmed it" -> verification.claim,
    #     from which CLAIM_VERIFICATION auto-derives markers to check against
    #     the cited evidence.
    severity: str | None = None
    description: str | None = None
    confirmation_method: str | None = None
    # CLAIM_VERIFICATION inputs (validator.py _rule_claim_verification):
    # a mechanically-testable finding either asserts explicit markers its cited
    # evidence demonstrates, carries a prose ``claim`` we auto-derive markers
    # from, or is honestly flagged not_mechanically_testable.
    verification: dict | None = None
    not_mechanically_testable: bool = False


@dataclass
class FindingSignOffBody:
    finding_id: str
    operator_id: str | None = None
    note: str | None = None


@dataclass
class FindingRescindBody:
    finding_id: str
    reason: str
    superseded_by: str | None = None


@dataclass
class FindingQueueForReviewBody:
    finding_id: str
    note: str | None = None


@dataclass
class FindingReturnToDraftBody:
    finding_id: str
    note: str | None = None


@dataclass
class FindingEnrichBody:
    finding_id: str
    enrichments: list[dict]   # FieldEnrichment dicts with status="pending"


@dataclass
class FindingEnrichmentReviewBody:
    finding_id: str
    field: str
    decision: str
    note: str | None = None


# ---------- helpers ----------

def _session_dir(ctx: ChannelContext, sessions_root: Path) -> Path:
    if not ctx.engagement_id:
        raise ChannelRejection("ChannelContext.engagement_id required")
    return sessions_root / ctx.engagement_id


def _check_stop_conditions_and_latch(
    sessions_root: Path, ctx: "ChannelContext", store: FindingStore,
) -> None:
    """v0.1 §10 fast-fail hook. Called after a finding lands in the
    store. Builds a FindingsSummary from the live store and asks the
    stop-watcher whether any condition tripped. The latch is per-trim-
    engagement (not per-session), so the next tool call across any
    session for this engagement will see the tripped state.

    No-op when no trim is applied or the engagement has no
    stop_conditions configured. Lazy imports keep this off the hot
    path for runtimes that aren't using v0.1.
    """
    state_root = Path(sessions_root).parent  # <state>/sessions/ → <state>
    try:
        from ara import armors as _armors
        from ara import trims as _trims
        from ara.runtime import stop_watcher as _sw
    except Exception:
        return

    armor_name = _armors.active_armor_name(state_root)
    if not armor_name:
        return
    trim_id = _trims.active_trim_id(state_root, armor_name)
    if trim_id is None:
        return
    meta = _trims.load_trim(state_root, armor_name, trim_id)
    if meta is None or not meta.stop_conditions:
        return
    eid = meta.engagement_id
    if not eid:
        return

    findings = store.all()
    by_sev: dict[str, int] = {}
    for f in findings:
        n = getattr(f, "severity_name", None) or "info"
        by_sev[n] = by_sev.get(n, 0) + 1
    summary = _sw.FindingsSummary(total=len(findings), by_severity=by_sev)

    window_start = None
    if meta.test_window_start:
        try:
            from datetime import datetime, timezone
            window_start = datetime.strptime(
                meta.test_window_start, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            window_start = None

    _sw.evaluate_and_latch(
        state_root, eid,
        conditions=meta.stop_conditions,
        summary=summary,
        now_epoch=time.time(),
        window_start_epoch=window_start,
    )


def _load_impact_axes(weapon_dir: Path | None) -> list[tuple[str, int, int]] | None:
    """Read the pack-declared impact alphabet (if present) from the
    weapon's ``alphabets/impact.json`` and return its axes as
    (name, min, max) tuples. Returns None when no impact alphabet is
    present, in which case the validator falls back to defaults.
    """
    if weapon_dir is None:
        return None
    import json as _json
    path = Path(weapon_dir) / "alphabets" / "impact.json"
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text())
    except Exception:
        return None
    if data.get("shape") != "vector":
        return None
    axes_raw = data.get("axes") or []
    out: list[tuple[str, int, int]] = []
    for ax in axes_raw:
        try:
            out.append((str(ax["name"]),
                        int(ax.get("min", 0)),
                        int(ax.get("max", 3))))
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def _load_field_expectations(weapon_dir: Path | None) -> dict | None:
    """Read the pack-declared per-field content bar from the weapon's
    ``alphabets/field_expectations.json``. Returns None when absent, in which
    case the validator applies its baseline defaults."""
    if weapon_dir is None:
        return None
    import json as _json
    path = Path(weapon_dir) / "alphabets" / "field_expectations.json"
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) and data else None


def _build_validator_ctx(
    *, sessions_root: Path, weapon_dir: Path | None,
    ctx: ChannelContext, in_scope_assets: set[str] | None = None,
) -> ValidatorContext:
    sd = _session_dir(ctx, sessions_root)
    alphabets = load_alphabets(weapon_dir) if weapon_dir else Alphabets(
        categories=__import__("ara.runtime.findings.alphabets",
                              fromlist=["CategoryEnum"]).CategoryEnum(),
        rubric=__import__("ara.runtime.findings.alphabets",
                          fromlist=["SeverityRubric"]).SeverityRubric(),
        fix_library=__import__("ara.runtime.findings.alphabets",
                               fromlist=["FixLibrary"]).FixLibrary(),
    )
    return ValidatorContext(
        alphabets=alphabets,
        binder=EvidenceBinder(sd),
        fact_store=FactStore(sd),
        lead_store=LeadStore(sd),
        activation_id=ctx.activation_id,
        in_scope_assets=in_scope_assets,
        audit_log_path=ctx.audit_sink,
        impact_axes=_load_impact_axes(weapon_dir),
        field_expectations=_load_field_expectations(weapon_dir),
    )


# ---------- natural-finding normalization ----------

_SEVERITY_WORD_SCORE = {
    "info": 0.5, "informational": 0.5, "none": 0.5,
    "low": 2.5, "minor": 2.5,
    "medium": 5.0, "moderate": 5.0, "med": 5.0,
    "high": 7.5, "important": 7.5,
    "critical": 9.0, "crit": 9.0, "severe": 9.0,
}


def _normalize_natural_finding(body: dict) -> None:
    """Map the *natural* finding an LLM emits onto the canonical schema,
    in place. An agent produces title / severity-word / description / prose
    'how I confirmed it' + cited evidence; this folds those into the
    structured fields the store and validator use, WITHOUT fabricating the
    forensic-metadata fields left optional (severity_cell, impact, ...).

    - ``severity`` word  -> ``severity_score_override`` (when no cell/override)
    - ``description``     -> appended to ``notes``
    - ``confirmation_method`` (or a string ``verification``) -> ``verification``
      carrying a prose ``claim`` that CLAIM_VERIFICATION auto-derives markers
      from and checks against the cited evidence.
    """
    if not isinstance(body, dict):
        return
    # severity word -> score (only if the agent gave no explicit cell/override)
    sev = body.get("severity")
    if (isinstance(sev, str) and sev.strip()
            and body.get("severity_score_override") is None
            and not body.get("severity_cell")):
        s = _SEVERITY_WORD_SCORE.get(sev.strip().lower())
        if s is not None:
            body["severity_score_override"] = s
    # description -> notes (keep the model's prose; don't discard it)
    desc = body.get("description")
    if isinstance(desc, str) and desc.strip():
        existing = body.get("notes") or ""
        body["notes"] = (existing + ("\n" if existing else "") + desc.strip())[:4000]
    # verification: accept explicit markers dict; else fold a prose claim
    ver = body.get("verification")
    if isinstance(ver, str) and ver.strip():
        ver = {"claim": ver.strip()}
        body["verification"] = ver
    claim = None
    cm = body.get("confirmation_method")
    if isinstance(cm, str) and cm.strip():
        claim = cm.strip()
    has_markers = isinstance(ver, dict) and (
        ver.get("must_contain") or ver.get("must_not_contain") or ver.get("claim"))
    if claim and not has_markers:
        base = dict(ver) if isinstance(ver, dict) else {}
        base["claim"] = claim
        body["verification"] = base
    # impact prose -> impact.summary (the readable field the Spoils/report UI
    # and the sign-off gate read). An already-structured impact dict is kept.
    imp = body.get("impact")
    if isinstance(imp, str) and imp.strip():
        body["impact"] = {"summary": imp.strip()[:2000]}
    # remediation prose -> fix_custom (+ a verification_method so the fix passes
    # the fix_custom validity rule).
    rem = body.pop("remediation", None)
    if isinstance(rem, str) and rem.strip():
        fc = (dict(body["fix_custom"])
              if isinstance(body.get("fix_custom"), dict) else {})
        fc.setdefault("summary", rem.strip()[:2000])
        fc.setdefault("what_changed", rem.strip()[:2000])
        fc.setdefault("verification_method",
                      "confirmed via the cited evidence / confirmation_method")
        body["fix_custom"] = fc


# ---------- channels ----------

class FindingFileChannel(Channel):
    name = "finding.file"
    schema = FindingFileBody
    sink_subsystem = "findings.jsonl"

    def __init__(self, *, sessions_root: Path,
                 weapon_dir: Path | None = None,
                 in_scope_assets: set[str] | None = None) -> None:
        self.sessions_root = Path(sessions_root)
        self.weapon_dir = Path(weapon_dir) if weapon_dir else None
        self.in_scope_assets = in_scope_assets

    def validate(self, body: dict) -> None:
        # Normalize the natural finding, then TOLERATE stray fields: drop any
        # key not in the schema instead of hard-rejecting (one unknown field
        # must never nuke an otherwise-valid finding). Then the base dataclass
        # check + semantic rules run on the cleaned body.
        _normalize_natural_finding(body)
        allowed = {f.name for f in _dc_fields(FindingFileBody)}
        for k in [k for k in body if k not in allowed]:
            body.pop(k, None)
        super().validate(body)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        if not ctx.activation_id:
            raise ChannelRejection("ChannelContext.activation_id required")
        v_ctx = _build_validator_ctx(
            sessions_root=self.sessions_root,
            weapon_dir=self.weapon_dir, ctx=ctx,
            in_scope_assets=self.in_scope_assets,
        )
        # Category coercion: an unknown category is mapped to "other" (recording
        # the model's word as category_proposed) so a novel category name is
        # kept, not rejected — consistent with forensic fields being advisory.
        cat = body.get("category") or ""
        cats = v_ctx.alphabets.categories
        if cats.entries and cats.has("other") and (not cat or not cats.has(cat)):
            # Unknown/blank category -> "other" with the model's word (or the
            # title) recorded as the proposed category.
            body["category_proposed"] = (
                body.get("category_proposed") or cat
                or str(body.get("title") or "")[:60] or "unspecified")
            body["category"] = "other"

        # Provenance enrichment (additive, best-effort): auto-correlate the
        # response-bearing primary from this activation (so the primary-evidence
        # gate is self-satisfying) and attach connection + authorization
        # provenance. Runs BEFORE validation so the gate sees the bound primary.
        try:
            from ara.runtime import provenance as _prov
            _prov.enrich_evidence(
                body, binder=v_ctx.binder, ctx=ctx,
                session_dir=_session_dir(ctx, self.sessions_root),
                state_root=self.sessions_root.parent,
            )
        except Exception:  # noqa: BLE001
            pass

        rejections = validate_for_draft(body, v_ctx)
        try:
            from ara import debug_log as _dbg
            _gr = next((r for r in rejections
                        if r.rule == "PRIMARY_EVIDENCE_RESPONSE_BEARING"), None)
            _dbg.emit(
                "finding-gate", "decision",
                state_root=self.sessions_root.parent, session_id=ctx.engagement_id,
                kind="finding", verdict=("reject" if _gr else "accept"),
                reason=(_gr.message if _gr else ""), activation_id=ctx.activation_id,
            )
        except Exception:  # noqa: BLE001
            pass
        if rejections:
            joined = "; ".join(f"{r.rule}: {r.message}" for r in rejections)
            raise ChannelRejection(joined)

        # Severity score: explicit override (incl. normalized severity word) wins;
        # else rubric lookup on a provided cell; else default medium.
        cell = body.get("severity_cell")
        cell_ok = isinstance(cell, (list, tuple)) and len(cell) == 2
        if body.get("severity_score_override") is not None:
            score = float(body["severity_score_override"])
        elif cell_ok and v_ctx.alphabets.rubric.matrix:
            rcell = v_ctx.alphabets.rubric.lookup(cell[0], cell[1])
            score = rcell.score if rcell is not None else 5.0
        else:
            score = 5.0  # neither cell nor severity word given -> medium
        sev_name = severity_name_from_score(score)
        cell_tuple = (cell[0], cell[1]) if cell_ok else ("", "")

        now = time.time()
        finding = Finding(
            id=mint_finding_id(),
            title=body["title"],
            category=body["category"],
            severity_cell=cell_tuple,
            severity_score=score,
            severity_name=sev_name,
            fix_template_id=body.get("fix_template_id"),
            fix_custom=body.get("fix_custom"),
            evidence_refs=list(body.get("evidence_refs") or []),
            fact_refs=list(body.get("fact_refs") or []),
            lead_refs=list(body.get("lead_refs") or []),
            provenance_chain=list(body.get("provenance_chain") or []),
            impact=dict(body.get("impact") or {}),
            affected_assets=list(body.get("affected_assets") or []),
            origin=body.get("origin", "agent_derived"),
            status="draft",
            activation_id=ctx.activation_id,
            created_at=now,
            category_proposed=body.get("category_proposed"),
            notes=body.get("notes"),
            verification=body.get("verification"),
            not_mechanically_testable=bool(body.get("not_mechanically_testable", False)),
        )
        sess_dir = _session_dir(ctx, self.sessions_root)
        store = FindingStore(sess_dir)
        store.append(finding)
        # Proposal 2: content-address each field value as its own immutable
        # hashed object (impact:/remediation:/claim:/reason:/asset: ids) and
        # record which objects this finding points at. ADDITIVE + best-effort:
        # the finding still inlines its values (every reader keeps working);
        # this is a parallel provenance / tamper-evidence / diff layer. Never
        # let it fail the file — a missing provenance object must not block a
        # finding.
        try:
            from .field_store import FieldStore
            fc = finding.fix_custom if isinstance(finding.fix_custom, dict) else {}
            ver = finding.verification if isinstance(finding.verification, dict) else {}
            FieldStore(sess_dir).record_finding_fields(finding.id, {
                "impact": (finding.impact or {}).get("summary") or "",
                "remediation": (fc.get("summary") or fc.get("what_changed") or ""),
                "confirmation": (ver.get("claim") or ""),
                "reason": finding.notes or "",
                "affected_assets": list(finding.affected_assets or []),
            })
        except Exception:
            pass
        # v0.1 §10: fast-fail stop-watcher. Without this, stop conditions
        # only get checked on the next tool dispatch — meaning the
        # tripping finding's session continues spawning crumbs until
        # the next supervisor query. The hook makes the latch trip
        # synchronously so the next tool call sees a tripped state.
        # Best-effort: any exception is swallowed (the channel must
        # never fail because of post-execute hooks).
        try:
            _check_stop_conditions_and_latch(
                self.sessions_root, ctx, store,
            )
        except Exception:
            pass
        return {"finding": finding.to_jsonable()}


class FindingSignOffChannel(Channel):
    name = "finding.sign_off"
    schema = FindingSignOffBody
    sink_subsystem = "findings.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path,
                 weapon_dir: Path | None = None,
                 in_scope_assets: set[str] | None = None) -> None:
        self.sessions_root = Path(sessions_root)
        self.weapon_dir = Path(weapon_dir) if weapon_dir else None
        self.in_scope_assets = in_scope_assets

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        sd = _session_dir(ctx, self.sessions_root)
        store = FindingStore(sd)
        f = store.by_id(body["finding_id"])
        if f is None:
            raise ChannelRejection(f"finding not found: {body['finding_id']!r}")
        if f.status not in ("draft", "operator_review"):
            raise ChannelRejection(f"can't sign off status={f.status}")
        v_ctx = _build_validator_ctx(
            sessions_root=self.sessions_root,
            weapon_dir=self.weapon_dir, ctx=ctx,
            in_scope_assets=self.in_scope_assets,
        )
        rejections = validate_for_signoff(f, v_ctx)
        if rejections:
            joined = "; ".join(f"{r.rule}: {r.message}" for r in rejections)
            raise ChannelRejection(f"signoff blocked: {joined}")
        from dataclasses import replace
        signed = replace(
            f,
            status="signed_off",
            signed_off_at=time.time(),
            signed_off_by=body.get("operator_id") or ctx.caller,
        )
        signed = replace(signed, content_hash=compute_content_hash(signed))
        store.append(signed)

        # Auto-confirm referenced leads via lead.confirm system channel.
        try:
            confirm = registry.get("lead.confirm")
        except KeyError:
            confirm = None
        if confirm is not None:
            sys_ctx = ChannelContext(
                engagement_id=ctx.engagement_id,
                activation_id=ctx.activation_id,
                forge_id=ctx.forge_id,
                caller="system",
                audit_sink=ctx.audit_sink,
            )
            for lid in signed.lead_refs:
                try:
                    confirm.call({"lead_id": lid, "finding_id": signed.id}, sys_ctx)
                except Exception:
                    pass  # lead may already be in a closed state
        return {"finding": signed.to_jsonable()}


class FindingRescindChannel(Channel):
    name = "finding.rescind"
    schema = FindingRescindBody
    sink_subsystem = "findings.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        sd = _session_dir(ctx, self.sessions_root)
        store = FindingStore(sd)
        f = store.by_id(body["finding_id"])
        if f is None:
            raise ChannelRejection(f"finding not found: {body['finding_id']!r}")
        if f.status not in ("draft", "operator_review", "signed_off"):
            raise ChannelRejection(f"can't rescind status={f.status}")
        from dataclasses import replace
        rescinded = replace(
            f, status="rescinded",
            rescinded_at=time.time(), rescinded_by=ctx.caller,
            superseded_by=body.get("superseded_by"),
            notes=(f.notes or "") + f"\n[rescind] {body['reason']}",
        )
        store.append(rescinded)
        return {"finding": rescinded.to_jsonable()}


class FindingQueueForReviewChannel(Channel):
    """Operator-only: move a draft finding into the review queue."""
    name = "finding.queue_for_review"
    schema = FindingQueueForReviewBody
    sink_subsystem = "findings.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        sd = _session_dir(ctx, self.sessions_root)
        store = FindingStore(sd)
        f = store.by_id(body["finding_id"])
        if f is None:
            raise ChannelRejection(f"finding not found: {body['finding_id']!r}")
        if f.status != "draft":
            raise ChannelRejection(
                f"can't queue for review status={f.status}"
            )
        from dataclasses import replace
        note = body.get("note")
        queued = replace(
            f, status="operator_review",
            notes=(f.notes or "") + (
                f"\n[queue_for_review] {note}" if note else
                "\n[queue_for_review]"
            ),
        )
        store.append(queued)
        return {"finding": queued.to_jsonable()}


class FindingReturnToDraftChannel(Channel):
    """Operator-only: bounce a queued finding back to draft so the
    agent (or another operator) can keep working on it."""
    name = "finding.return_to_draft"
    schema = FindingReturnToDraftBody
    sink_subsystem = "findings.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        sd = _session_dir(ctx, self.sessions_root)
        store = FindingStore(sd)
        f = store.by_id(body["finding_id"])
        if f is None:
            raise ChannelRejection(f"finding not found: {body['finding_id']!r}")
        if f.status != "operator_review":
            raise ChannelRejection(
                f"can't return to draft status={f.status}"
            )
        from dataclasses import replace
        note = body.get("note")
        bounced = replace(
            f, status="draft",
            notes=(f.notes or "") + (
                f"\n[return_to_draft] {note}" if note else
                "\n[return_to_draft]"
            ),
        )
        store.append(bounced)
        return {"finding": bounced.to_jsonable()}


class FindingEnrichChannel(Channel):
    name = "finding.enrich"
    schema = FindingEnrichBody
    sink_subsystem = "findings.jsonl"
    allowed_callers = ("agent",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        sd = _session_dir(ctx, self.sessions_root)
        store = FindingStore(sd)
        f = store.by_id(body["finding_id"])
        if f is None:
            raise ChannelRejection(f"finding not found: {body['finding_id']!r}")
        if f.status != "draft":
            raise ChannelRejection(f"can't enrich status={f.status}")
        if f.origin not in ("operator_input", "operator_seeded_agent_enriched"):
            raise ChannelRejection(
                f"enrichment requires origin operator_input or operator_seeded_agent_enriched (got {f.origin})"
            )
        new_enrichments = []
        now = time.time()
        for e in body["enrichments"]:
            new_enrichments.append(FieldEnrichment(
                field=e["field"],
                proposed_value=e["proposed_value"],
                rationale=e.get("rationale", ""),
                proposed_at=now,
                status="pending",
            ))
        from dataclasses import replace
        updated = replace(
            f,
            enrichments=list(f.enrichments) + new_enrichments,
            origin="operator_seeded_agent_enriched",
        )
        store.append(updated)
        return {"finding": updated.to_jsonable(),
                "added_enrichments": len(new_enrichments)}


class FindingEnrichmentReviewChannel(Channel):
    name = "finding.enrichment_review"
    schema = FindingEnrichmentReviewBody
    sink_subsystem = "findings.jsonl"
    allowed_callers = ("operator",)

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        decision = body["decision"]
        if decision not in ("approved", "denied", "follow_up"):
            raise ChannelRejection(f"unknown decision: {decision!r}")
        sd = _session_dir(ctx, self.sessions_root)
        store = FindingStore(sd)
        f = store.by_id(body["finding_id"])
        if f is None:
            raise ChannelRejection(f"finding not found: {body['finding_id']!r}")
        target_field = body["field"]
        from dataclasses import replace
        new_enrichments = []
        found = False
        now = time.time()
        for e in f.enrichments:
            if e.field == target_field and e.status == "pending":
                new_enrichments.append(FieldEnrichment(
                    field=e.field, proposed_value=e.proposed_value,
                    rationale=e.rationale, proposed_at=e.proposed_at,
                    status=decision,
                    reviewed_at=now, reviewed_by=ctx.caller,
                    review_note=body.get("note"),
                ))
                found = True
            else:
                new_enrichments.append(e)
        if not found:
            raise ChannelRejection(
                f"no pending enrichment found for field {target_field!r}"
            )
        updated = replace(f, enrichments=new_enrichments)
        store.append(updated)
        return {"finding": updated.to_jsonable()}


for _cls in (FindingFileChannel, FindingSignOffChannel, FindingRescindChannel,
             FindingEnrichChannel, FindingEnrichmentReviewChannel):
    registry.register_class(_cls)


def install_finding_channels(
    *, sessions_root: Path,
    weapon_dir: Path | None = None,
    in_scope_assets: set[str] | None = None,
) -> dict[str, Channel]:
    builders = {
        "finding.file": lambda: FindingFileChannel(
            sessions_root=sessions_root, weapon_dir=weapon_dir,
            in_scope_assets=in_scope_assets,
        ),
        "finding.sign_off": lambda: FindingSignOffChannel(
            sessions_root=sessions_root, weapon_dir=weapon_dir,
            in_scope_assets=in_scope_assets,
        ),
        "finding.rescind": lambda: FindingRescindChannel(sessions_root=sessions_root),
        "finding.queue_for_review": lambda: FindingQueueForReviewChannel(
            sessions_root=sessions_root,
        ),
        "finding.return_to_draft": lambda: FindingReturnToDraftChannel(
            sessions_root=sessions_root,
        ),
        "finding.enrich": lambda: FindingEnrichChannel(sessions_root=sessions_root),
        "finding.enrichment_review": lambda: FindingEnrichmentReviewChannel(sessions_root=sessions_root),
    }
    chans: dict[str, Channel] = {}
    for name, build in builders.items():
        if registry.has(name):
            chans[name] = registry.get(name)
        else:
            ch = build()
            registry.register(ch)
            chans[name] = ch
    return chans


# Silence unused-import warnings reserved for future use
_ = (Any,)
