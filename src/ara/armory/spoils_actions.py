"""Operator mutations for the Spoils page — edit finding/lead fields and
attach files. These are operator-driven edits (the human has final say over
findings), applied directly to the append-only stores via ``replace`` +
``append`` (same pattern as finding.sign_off).
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from .forge_v1_views import _spoils_session_dir as _resolve_session_dir

_SEVERITY_SCORE = {
    "info": 0.5, "low": 2.5, "medium": 5.0, "high": 7.5, "critical": 9.0,
}
_VALID_DISPOSITIONS = {
    "confirmed", "false_positive", "non_exploitable",
    "non_reproducible", "unsure",
}


def _s(form: dict, key: str) -> str:
    v = form.get(key)
    return v.strip() if isinstance(v, str) else ""


def edit_finding(state_root: Path, finding_id: str,
                 form: dict) -> tuple[bool, str]:
    """Apply operator field edits to a finding."""
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "no active session"
    from ..runtime.findings.store import FindingStore
    store = FindingStore(sd)
    f = store.by_id(finding_id)
    if f is None:
        return False, f"finding {finding_id} not found"

    updates: dict = {}
    title = _s(form, "title")
    if title:
        updates["title"] = title[:120]
    sev = _s(form, "severity").lower()
    if sev in _SEVERITY_SCORE:
        updates["severity_name"] = sev
        updates["severity_score"] = _SEVERITY_SCORE[sev]
    cat = _s(form, "category")
    if cat:
        updates["category"] = cat
    if "disposition" in form:
        disp = _s(form, "disposition")
        updates["disposition"] = disp if disp in _VALID_DISPOSITIONS else None
        updates["disposition_reason"] = _s(form, "disposition_reason") or None
        updates["disposition_by"] = "operator"
        updates["disposition_at"] = time.time()
    if "affected_assets" in form:
        aa = _s(form, "affected_assets")
        updates["affected_assets"] = [
            x.strip() for x in aa.split(",") if x.strip()
        ]
    if "notes" in form:
        updates["notes"] = (_s(form, "notes") or None)
    if "claim" in form:
        claim = _s(form, "claim")
        ver = dict(f.verification) if isinstance(f.verification, dict) else {}
        if claim:
            ver["claim"] = claim
        else:
            ver.pop("claim", None)
        updates["verification"] = ver or None
    if "impact_note" in form:
        txt = _s(form, "impact_note")
        imp = dict(f.impact) if isinstance(f.impact, dict) else {}
        if txt:
            imp["summary"] = txt
        else:
            imp.pop("summary", None)
        updates["impact"] = imp or None
    if "remediation" in form:
        txt = _s(form, "remediation")
        fc = dict(f.fix_custom) if isinstance(f.fix_custom, dict) else {}
        if txt:
            fc["summary"] = txt
        else:
            fc.pop("summary", None)
        updates["fix_custom"] = fc or None
    try:
        store.append(replace(f, **updates))
    except Exception as exc:  # noqa: BLE001
        return False, f"edit failed: {type(exc).__name__}: {exc}"
    return True, f"Updated finding {finding_id}"


def attach_file(state_root: Path, finding_id: str, *, filename: str,
                data: bytes, summary: str = "") -> tuple[bool, str]:
    """Store an operator-uploaded file as evidence and link it to the finding."""
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "no active session"
    if not data:
        return False, "empty file"
    from ..runtime.findings.store import FindingStore
    from ..runtime.binder.api import EvidenceBinder
    store = FindingStore(sd)
    f = store.by_id(finding_id)
    if f is None:
        return False, f"finding {finding_id} not found"
    binder = EvidenceBinder(sd)
    try:
        rec = binder.put(
            data, source="operator:attachment",
            source_metadata={"filename": filename or "attachment"},
            summary=(summary or filename or "operator attachment"),
            activation_id=(getattr(f, "activation_id", None) or "operator"),
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"attach failed: {type(exc).__name__}: {exc}"
    finally:
        try:
            binder.close()
        except Exception:  # noqa: BLE001
            pass
    refs = list(f.evidence_refs or []) + [rec.id]
    try:
        store.append(replace(f, evidence_refs=refs))
    except Exception as exc:  # noqa: BLE001
        return False, f"link failed: {type(exc).__name__}: {exc}"
    return True, f"Attached {filename or 'file'} → {rec.id}"


def edit_lead(state_root: Path, lead_id: str, form: dict) -> tuple[bool, str]:
    """Apply operator field edits to a lead."""
    sd = _resolve_session_dir(state_root)
    if sd is None:
        return False, "no active session"
    try:
        from ..runtime.leads.store import LeadStore
    except Exception as exc:  # noqa: BLE001
        return False, f"leads unavailable: {exc}"
    store = LeadStore(sd)
    ld = store.by_id(lead_id)
    if ld is None:
        return False, f"lead {lead_id} not found"
    updates: dict = {}
    title = _s(form, "title")
    if title:
        updates["title"] = title[:200]
    status = _s(form, "status")
    if status:
        updates["status"] = status
    if "reasoning" in form:
        field = "reasoning" if hasattr(ld, "reasoning") else (
            "note" if hasattr(ld, "note") else None)
        if field:
            updates[field] = _s(form, "reasoning")
    try:
        store.append(replace(ld, **updates))
    except Exception as exc:  # noqa: BLE001
        return False, f"edit failed: {type(exc).__name__}: {exc}"
    return True, f"Updated lead {lead_id}"
