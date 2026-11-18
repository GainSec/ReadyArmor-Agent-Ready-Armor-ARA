"""Tailor Robes + Needle lifecycle for Temper generation."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from uuid import uuid4

from ara import temper_controls


TAILOR_ARMOR = "tailor-robes"
NEEDLE_WEAPON = "needle"
TAILOR_SESSION_REL = Path("temper") / "tailor-session.json"
TAILOR_CANDIDATE_DESTINATIONS = frozenset({
    "absolute",
    "bulwark",
    "canary",
    "rhema",
    "approval_gate",
    "alert_hook",
    "trim_template",
})
TAILOR_MIN_CANDIDATES_PER_SOURCE = 12
TAILOR_CANDIDATE_REQUIRED = (
    "thread_type",
    "destination",
    "title",
    "source",
    "rationale",
    "risk",
    "default_decision",
    "proposed_text",
    "control",
    "citations",
)
_GENERIC_INSTRUCTION_BAN_RE = re.compile(
    r"\b(no|do\s+not|must\s+not)\b.{0,120}"
    r"\b(instructions?|playbooks?|workflows?|steps?)\b",
    re.IGNORECASE | re.DOTALL,
)
_BOUNDARY_SIGNAL_RE = re.compile(
    r"("
    r"(/etc|/root|/dev|/sbin|~/\.ssh|/var/log|/proc|/sys)(?:/|\b)"
    r"|(?:approval|deny)\s+(?:fs|exec|net|url|env|perm)\b"
    r"|\b(binwalk|flashrom|esptool|adb|rfcat|firmware extraction)\b"
    r"|raw block-device reads?"
    r")",
    re.IGNORECASE,
)
_METHODOLOGY_LIST_FIELDS = (
    "expected",
    "unexpected",
    "requires_context",
    "never_without_approval",
)
_SAFETY_LIST_FIELDS = (
    "surfaces",
    "allowed",
    "restricted",
    "requires_context",
    "never_without_approval",
)
_WEB_APP_EXPECTED_TOOL_RE = re.compile(
    r"("
    r"\bbrowser automation\b"
    r"|\bweb(?:[- ]app)? scanning\b"
    r"|\bweb port scanners?\b"
    r"|\bxss testing(?: frameworks?)?\b"
    r"|\bsql injection(?: testing| tools?)?\b"
    r"|\bproxy-based web testing\b"
    r"|\bhttp request/response (?:inspection|review)\b"
    r"|\bapi testing\b"
    r")",
    re.IGNORECASE,
)
_WEB_APP_RESTRICTION_QUALIFIER_RE = re.compile(
    r"("
    r"\bout[- ]of[- ]scope\b"
    r"|\boutside\b"
    r"|\bnon[- ]target\b"
    r"|\bunauthori[sz]ed\b"
    r"|\bwithout (?:operator )?approval\b"
    r"|\bwithout matching\b"
    r"|\bdestructive\b"
    r"|\bcredential stuffing\b"
    r")",
    re.IGNORECASE,
)


class TailorLifecycleError(RuntimeError):
    pass


class TailorCandidateRejected(TailorLifecycleError):
    def __init__(self, message: str, rejection: dict) -> None:
        super().__init__(message)
        self.rejection = rejection


def tailor_session_path(state_root: Path) -> Path:
    return Path(state_root) / TAILOR_SESSION_REL


def load_tailor_session(state_root: Path) -> dict | None:
    path = tailor_session_path(state_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_tailor_session(state_root: Path, record: dict) -> dict:
    path = tailor_session_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return record


def _json_safe(value: object) -> object:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return repr(value)


def _candidate_correction(destination: str, error: str) -> dict:
    return {
        "instruction": (
            "Candidate rejected before proposal promotion. Preserve the useful "
            "idea, correct and resubmit through the same Temper tool."
        ),
        "error": error,
        "rejected_fields": _rejected_fields_from_error(error),
        "destination": destination,
        "expected_control": temper_controls.describe_control_contract(destination),
        "required_candidate_fields": list(TAILOR_CANDIDATE_REQUIRED),
    }


def _rejected_fields_from_error(error: str) -> list[str]:
    fields: list[str] = []
    if "thread_type" in error:
        fields.append("thread_type")
    if "replacement" in error:
        fields.append("control.replacement")
    if "missing required fields:" in error:
        tail = error.split("missing required fields:", 1)[1]
        fields.extend(
            f"control.{field.strip()}"
            for field in tail.split(",")
            if field.strip()
        )
    return sorted(set(fields))


def record_tailor_candidate_rejection(
    state_root: Path,
    candidate: dict,
    error: str,
    *,
    tool_name: str | None = None,
) -> dict:
    state = Path(state_root)
    record = load_tailor_session(state)
    if record is None:
        raise TailorLifecycleError("no tailor session record found")
    if record.get("status") != "running":
        raise TailorLifecycleError(
            f"tailor session is not running: {record.get('status')!r}"
        )
    attempted = dict(candidate)
    destination = str(attempted.get("destination") or "").strip()
    rejection = {
        "rejection_id": f"temper-reject-{uuid4().hex[:12]}",
        "session_id": record.get("session_id"),
        "created_at": time.time(),
        "tool_name": tool_name,
        "destination": destination,
        "error": str(error),
        "attempt": _json_safe(attempted),
        "correction": _candidate_correction(destination, str(error)),
    }
    rejected = list(record.get("rejected_candidates") or [])
    rejected.append(rejection)
    record["rejected_candidates"] = rejected
    _write_tailor_session(state, record)
    return rejection


def _validate_tailor_candidate(candidate: dict) -> None:
    missing = [
        name for name in TAILOR_CANDIDATE_REQUIRED
        if name not in candidate or candidate.get(name) in (None, "")
    ]
    if missing:
        raise TailorLifecycleError(
            "tailor candidate missing required fields: " + ", ".join(missing)
        )
    destination = candidate.get("destination")
    if destination not in TAILOR_CANDIDATE_DESTINATIONS:
        raise TailorLifecycleError(
            f"invalid tailor candidate destination: {destination!r}"
        )
    citations = candidate.get("citations")
    if not isinstance(citations, list) or not citations:
        raise TailorLifecycleError("tailor candidate citations must be a non-empty list")
    for citation in citations:
        if not isinstance(citation, dict) or not citation:
            raise TailorLifecycleError(
                "tailor candidate citations must be non-empty objects"
            )
    try:
        candidate["thread_type"] = temper_controls.validate_thread_type(
            candidate.get("thread_type")
        )
        candidate["control"] = temper_controls.validate_control(
            str(destination), candidate.get("control")
        )
        # D5: rhema reply-rewrite patterns must be real, targeted matchers —
        # not dead labels or over-broad regexes that gut legitimate output.
        if destination == "rhema":
            temper_controls.validate_rhema_pattern(
                (candidate.get("control") or {}).get("pattern")
            )
    except temper_controls.TemperControlError as exc:
        raise TailorLifecycleError(str(exc)) from exc
    _validate_scope(candidate)
    _validate_candidate_contexts(candidate)


def _validate_scope(candidate: dict) -> None:
    """D5: every candidate must declare a routing scope from the fixed
    vocabulary, so it routes deterministically at task projection time
    instead of silently being excluded (the historical scope=None bug)."""
    scope = candidate.get("scope")
    if not isinstance(scope, list) or not scope:
        raise TailorLifecycleError(
            "tailor candidate must declare a non-empty scope; use one or more "
            "of: " + ", ".join(sorted(temper_controls.SCOPE_TAGS))
        )
    bad = [
        str(tag) for tag in scope
        if str(tag).strip() not in temper_controls.SCOPE_TAGS
    ]
    if bad:
        raise TailorLifecycleError(
            "tailor candidate scope has unknown tags: " + ", ".join(bad)
            + "; allowed: " + ", ".join(sorted(temper_controls.SCOPE_TAGS))
        )


def _validate_candidate_contexts(candidate: dict) -> None:
    methodology = candidate.get("methodology_context")
    safety = candidate.get("safety_constraints")
    text = "\n".join(
        str(candidate.get(field) or "")
        for field in ("title", "rationale", "risk", "proposed_text")
    )
    if _GENERIC_INSTRUCTION_BAN_RE.search(text) and not (methodology or safety):
        raise TailorLifecycleError(
            "tailor candidate uses a generic instruction/playbook ban; "
            "provide methodology_context or safety_constraints with expected, "
            "unexpected/restricted, requires_context, and exception_path details"
        )
    if _BOUNDARY_SIGNAL_RE.search(text) and not (methodology or safety):
        raise TailorLifecycleError(
            "tailor candidate defines a tool/path/action boundary; provide "
            "methodology_context or safety_constraints"
        )
    _validate_methodology_context(methodology)
    _validate_safety_constraints(safety)
    _validate_web_app_expected_tools_not_broadly_restricted(candidate)


def _validate_web_app_expected_tools_not_broadly_restricted(candidate: dict) -> None:
    methodology = candidate.get("methodology_context")
    if not isinstance(methodology, dict):
        return
    if str(methodology.get("task_family") or "").strip().lower() != "web_app":
        return
    control = candidate.get("control")
    safety = candidate.get("safety_constraints")
    values: list[str] = []
    for field in ("unexpected", "never_without_approval"):
        values.extend(str(value) for value in list(methodology.get(field) or []))
    if isinstance(control, dict):
        values.extend(str(value) for value in list(control.get("restricted") or []))
    if isinstance(safety, dict):
        for field in ("restricted", "never_without_approval"):
            values.extend(str(value) for value in list(safety.get(field) or []))
    broad = [
        value for value in values
        if _WEB_APP_EXPECTED_TOOL_RE.search(value)
        and not _WEB_APP_RESTRICTION_QUALIFIER_RE.search(value)
    ]
    if broad:
        raise TailorLifecycleError(
            "web_app methodology cannot broadly restrict expected web testing "
            "tools/actions; restrict specific out-of-scope, unapproved, "
            "destructive, or cross-context use instead: " + ", ".join(broad[:4])
        )


def _validate_methodology_context(context: object) -> None:
    if context is None:
        return
    if not isinstance(context, dict):
        raise TailorLifecycleError("methodology_context must be an object")
    task_family = str(context.get("task_family") or "").strip()
    if not task_family:
        raise TailorLifecycleError("methodology_context requires task_family")
    # D5: task_family shares the routing vocabulary so methodology context and
    # scope projection can't disagree (the historical mobile_ios / mixed /
    # investigation_workflow values routed to nothing).
    if task_family not in temper_controls.SCOPE_TAGS:
        raise TailorLifecycleError(
            f"methodology_context.task_family {task_family!r} is not a known "
            "task family; use one of: "
            + ", ".join(sorted(temper_controls.SCOPE_TAGS))
        )
    has_action_context = False
    for field in _METHODOLOGY_LIST_FIELDS:
        values = context.get(field)
        if values is None:
            continue
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value.strip()
            for value in values
        ):
            raise TailorLifecycleError(
                f"methodology_context.{field} must be a list of strings"
            )
        if values:
            has_action_context = True
    if not has_action_context:
        raise TailorLifecycleError(
            "methodology_context requires at least one expected, unexpected, "
            "requires_context, or never_without_approval entry"
        )
    if not str(context.get("exception_path") or "").strip():
        raise TailorLifecycleError("methodology_context requires exception_path")


def _validate_safety_constraints(constraints: object) -> None:
    if constraints is None:
        return
    if not isinstance(constraints, dict):
        raise TailorLifecycleError("safety_constraints must be an object")
    surfaces = constraints.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise TailorLifecycleError("safety_constraints requires surfaces")
    has_boundary = False
    for field in _SAFETY_LIST_FIELDS:
        values = constraints.get(field)
        if values is None:
            continue
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value.strip()
            for value in values
        ):
            raise TailorLifecycleError(
                f"safety_constraints.{field} must be a list of strings"
            )
        if field != "surfaces" and values:
            has_boundary = True
    if not has_boundary:
        raise TailorLifecycleError(
            "safety_constraints requires at least one allowed, restricted, "
            "requires_context, or never_without_approval entry"
        )
    if not str(constraints.get("exception_path") or "").strip():
        raise TailorLifecycleError("safety_constraints requires exception_path")


def _captured_prior(state_root: Path) -> tuple[str | None, str | None]:
    from ara import armors as armors_mod
    from ara import weapons as weapons_mod

    prior_armor = armors_mod.active_armor_name(state_root)
    prior_weapon = weapons_mod.equipped_weapon_id(state_root)
    if prior_armor == TAILOR_ARMOR:
        prior_armor = None
    if prior_weapon == NEEDLE_WEAPON:
        prior_weapon = None
    return prior_armor, prior_weapon


def equip_tailor(state_root: Path) -> dict:
    """Materialize and equip Tailor Robes + Needle.

    Returns a dict shaped like the Forge blacksmith equip report, with
    additional ``prior_armor`` / ``prior_weapon`` fields so callers can
    persist and restore the operator's previous loadout.
    """
    from ara import armors as armors_mod
    from ara import weapons as weapons_mod
    from ara.seed import ensure_seed

    state = Path(state_root)
    prior_armor, prior_weapon = _captured_prior(state)
    seed_report = ensure_seed(
        state,
        armors=(TAILOR_ARMOR,),
        weapons=(NEEDLE_WEAPON,),
    )
    armors_mod.equip_armor(state, TAILOR_ARMOR)
    weapons_mod.set_equipped_weapon(state, NEEDLE_WEAPON)
    equip_summary = weapons_mod.equip_policy(state, NEEDLE_WEAPON)
    return {
        "armor": TAILOR_ARMOR,
        "weapon": NEEDLE_WEAPON,
        "prior_armor": prior_armor,
        "prior_weapon": prior_weapon,
        "policy_overlaid": equip_summary.get("overlaid", []),
        "policy_kept": equip_summary.get("kept", []),
        "seeded_armors": seed_report.copied_armors,
        "seeded_weapons": seed_report.copied_weapons,
        "bulwark_section_applied": (
            TAILOR_ARMOR in seed_report.bulwark_sections_applied
        ),
    }


def start_tailor_session(
    state_root: Path,
    *,
    source_request: dict | None = None,
    session_id: str | None = None,
) -> dict:
    """Start an active Tailor run and persist its restore metadata."""
    state = Path(state_root)
    existing = load_tailor_session(state)
    if existing and existing.get("status") == "running":
        raise TailorLifecycleError(
            f"tailor session already running: {existing.get('session_id')}"
        )

    equipped = equip_tailor(state)
    now = time.time()
    record = {
        "session_id": session_id or f"tailor-{uuid4().hex[:10]}",
        "status": "running",
        "created_at": now,
        "restored_at": None,
        "prior_armor": equipped.get("prior_armor"),
        "prior_weapon": equipped.get("prior_weapon"),
        "source_request": dict(source_request or {}),
        "equipped": equipped,
        "restore_result": None,
        "candidates": [],
        "rejected_candidates": [],
    }
    return _write_tailor_session(state, record)


def _source_manifest_elements(state_root: Path, record: dict) -> list:
    """Load the source forge's manifest elements for the active session.

    Grounding spine (D2/D3): Temper is forged from a source forge whose mats
    were enumerated into a manifest. Returns [] when no source forge / manifest
    is available (deterministic trim/weapon paths, or test fixtures) so those
    flows keep working without grounding."""
    src = dict(record.get("source_request") or {})
    forge_id = str(src.get("source_forge") or "").strip()
    if not forge_id:
        return []
    try:
        from ara.forge import state as _fstate
        from ara.forge.manifest.element import read_manifest
        return read_manifest(
            _fstate.forge_manifest_path(Path(state_root), forge_id)
        )
    except Exception:  # noqa: BLE001 - missing/unenumerated forge means no grounding
        return []


def _element_kind(element) -> str:
    kind = getattr(element, "kind", "")
    return kind.value if hasattr(kind, "value") else str(kind)


def _verify_candidate_citations(
    state_root: Path, record: dict, candidate: dict,
) -> None:
    """D3: every candidate must cite a REAL manifest element of the source
    forge, verified at record time (mirrors forge ``record_extraction``).
    Skipped when no source manifest is available."""
    elements = _source_manifest_elements(state_root, record)
    if not elements:
        return
    valid = {e.id for e in elements}
    unknown: list[str] = []
    for citation in candidate.get("citations") or []:
        mid = str((citation or {}).get("manifest_element_id") or "").strip()
        if not mid:
            raise TailorLifecycleError(
                "tailor candidate citation must reference a manifest_element_id "
                "from the source forge manifest"
            )
        if mid not in valid:
            unknown.append(mid)
    if unknown:
        raise TailorLifecycleError(
            "tailor candidate cites manifest elements not in the source forge "
            "manifest: " + ", ".join(unknown[:5])
        )


def search_source_mats(
    state_root: Path, query: str, *, limit: int = 12,
) -> dict:
    """mats.search for Needle: find source manifest elements by text."""
    state = Path(state_root)
    record = load_tailor_session(state)
    if record is None:
        raise TailorLifecycleError("no tailor session record found")
    elements = _source_manifest_elements(state, record)
    q = str(query or "").strip().lower()
    out: list[dict] = []
    for e in elements:
        text = e.extracted_text or ""
        if not q or q in text.lower() or q in e.id.lower():
            out.append({
                "manifest_element_id": e.id,
                "kind": _element_kind(e),
                "source_mat_id": e.source_mat_id,
                "preview": " ".join(text.split())[:240],
            })
        if len(out) >= max(1, int(limit)):
            break
    return {"ok": True, "query": query, "count": len(out), "results": out}


def get_source_mat(state_root: Path, manifest_element_id: str) -> dict:
    """mats.get for Needle: fetch one source manifest element's text."""
    state = Path(state_root)
    record = load_tailor_session(state)
    if record is None:
        raise TailorLifecycleError("no tailor session record found")
    mid = str(manifest_element_id or "").strip()
    for e in _source_manifest_elements(state, record):
        if e.id == mid:
            return {
                "ok": True,
                "manifest_element_id": e.id,
                "kind": _element_kind(e),
                "source_mat_id": e.source_mat_id,
                "text": e.extracted_text or "",
            }
    raise TailorLifecycleError(f"unknown manifest_element_id: {mid!r}")


def add_tailor_candidate(state_root: Path, candidate: dict) -> dict:
    """Append a reviewable Temper candidate to the active Tailor session."""
    state = Path(state_root)
    record = load_tailor_session(state)
    if record is None:
        raise TailorLifecycleError("no tailor session record found")
    if record.get("status") != "running":
        raise TailorLifecycleError(
            f"tailor session is not running: {record.get('status')!r}"
        )

    pending = list(record.get("candidates") or [])
    normalized = dict(candidate)
    try:
        _validate_tailor_candidate(normalized)
        _verify_candidate_citations(state, record, normalized)
    except TailorLifecycleError as exc:
        rejection = record_tailor_candidate_rejection(
            state, normalized, str(exc),
        )
        raise TailorCandidateRejected(str(exc), rejection) from exc
    normalized["candidate_id"] = (
        normalized.get("candidate_id") or f"temper-{uuid4().hex[:12]}"
    )
    normalized["created_at"] = normalized.get("created_at") or time.time()
    pending.append(normalized)
    record["candidates"] = pending
    _write_tailor_session(state, record)
    return {
        "ok": True,
        "session_id": record.get("session_id"),
        "candidate_count": len(pending),
        "candidate": normalized,
    }


def add_tailor_coverage_summary(state_root: Path, summary: dict) -> dict:
    """Store the model's structured source/destination coverage summary."""
    state = Path(state_root)
    record = load_tailor_session(state)
    if record is None:
        raise TailorLifecycleError("no tailor session record found")
    if record.get("status") != "running":
        raise TailorLifecycleError(
            f"tailor session is not running: {record.get('status')!r}"
        )
    if not isinstance(summary, dict):
        raise TailorLifecycleError("tailor coverage summary must be an object")
    sources = summary.get("sources")
    if not isinstance(sources, list) or not sources:
        raise TailorLifecycleError("tailor coverage summary requires sources")
    normalized = dict(summary)
    normalized["created_at"] = normalized.get("created_at") or time.time()
    trial = dict(record)
    trial["coverage_summary"] = normalized
    try:
        validate_tailor_coverage_for_proposal(trial)
    except ValueError as exc:
        raise TailorLifecycleError(str(exc)) from exc
    record["coverage_summary"] = normalized
    _write_tailor_session(state, record)
    return {
        "ok": True,
        "session_id": record.get("session_id"),
        "coverage_source_count": len(sources),
        "coverage_summary": normalized,
    }


def validate_tailor_coverage_for_proposal(record: dict) -> None:
    """Reject proposal promotion until Tailor wrote machine-checkable coverage.

    Candidate count alone is not a completion signal. The coverage summary must
    account for every source against every Temper destination, either by naming
    emitted candidate ids or by giving a skip reason.
    """
    coverage = record.get("coverage_summary")
    if not isinstance(coverage, dict):
        raise ValueError("tailor coverage summary is required")
    sources = coverage.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("tailor coverage summary must include sources")
    candidates = list(record.get("candidates") or [])
    candidate_by_id = {
        str(candidate.get("candidate_id")): candidate
        for candidate in candidates
        if candidate.get("candidate_id")
    }
    required_destinations = set(TAILOR_CANDIDATE_DESTINATIONS)
    seen_candidate_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("tailor coverage source entries must be objects")
        source_id = str(source.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("tailor coverage source missing source_id")
        destinations = source.get("destinations")
        if not isinstance(destinations, dict):
            raise ValueError(f"tailor coverage for {source_id} missing destinations")
        destination_names = set(destinations)
        missing = sorted(required_destinations - destination_names)
        extra = sorted(destination_names - required_destinations)
        if missing:
            raise ValueError(
                f"tailor coverage for {source_id} missing destinations: "
                + ", ".join(missing)
            )
        if extra:
            raise ValueError(
                f"tailor coverage for {source_id} has unknown destinations: "
                + ", ".join(extra)
            )
        emitted_count = 0
        for destination in sorted(required_destinations):
            cell = destinations.get(destination)
            if not isinstance(cell, dict):
                raise ValueError(
                    f"tailor coverage for {source_id}/{destination} must be an object"
                )
            status = str(cell.get("status") or "").strip()
            if status == "emitted":
                ids = cell.get("candidate_ids")
                if not isinstance(ids, list) or not ids:
                    raise ValueError(
                        f"tailor coverage for {source_id}/{destination} "
                        "requires candidate_ids"
                    )
                cell_ids = [str(candidate_id) for candidate_id in ids]
                if len(set(cell_ids)) != len(cell_ids):
                    raise ValueError(
                        f"tailor coverage for {source_id}/{destination} "
                        "references duplicate candidate ids: "
                        + ", ".join(sorted({
                            candidate_id for candidate_id in cell_ids
                            if cell_ids.count(candidate_id) > 1
                        }))
                    )
                unknown = [
                    str(candidate_id) for candidate_id in ids
                    if str(candidate_id) not in candidate_by_id
                ]
                if unknown:
                    raise ValueError(
                        f"tailor coverage for {source_id}/{destination} "
                        "references unknown candidates: " + ", ".join(unknown)
                    )
                duplicate = [
                    str(candidate_id) for candidate_id in ids
                    if str(candidate_id) in seen_candidate_ids
                ]
                if duplicate:
                    raise ValueError(
                        f"tailor coverage for {source_id}/{destination} "
                        "references duplicate candidate ids: "
                        + ", ".join(sorted(set(duplicate)))
                    )
                mismatched = []
                for candidate_id in ids:
                    candidate = candidate_by_id[str(candidate_id)]
                    candidate_source = str(candidate.get("source") or "").strip()
                    candidate_destination = str(
                        candidate.get("destination") or ""
                    ).strip()
                    if candidate_source != source_id:
                        mismatched.append(
                            f"{candidate_id} source={candidate_source!r}"
                        )
                    if candidate_destination != destination:
                        mismatched.append(
                            f"{candidate_id} destination={candidate_destination!r}"
                        )
                if mismatched:
                    raise ValueError(
                        f"tailor coverage for {source_id}/{destination} "
                        "has candidate source/destination mismatch: "
                        + "; ".join(mismatched)
                    )
                seen_candidate_ids.update(str(candidate_id) for candidate_id in ids)
                emitted_count += len(ids)
            elif status == "skipped":
                reason = str(cell.get("reason") or "").strip()
                if not reason:
                    raise ValueError(
                        f"tailor coverage for {source_id}/{destination} "
                        "requires a skipped reason"
                    )
            else:
                raise ValueError(
                    f"tailor coverage for {source_id}/{destination} has "
                    f"invalid status: {status!r}"
                )
        if emitted_count < TAILOR_MIN_CANDIDATES_PER_SOURCE:
            exception = str(source.get("minimum_exception_reason") or "").strip()
            if not exception:
                raise ValueError(
                    f"tailor coverage for {source_id} emitted {emitted_count} "
                    f"candidates; expected at least "
                    f"{TAILOR_MIN_CANDIDATES_PER_SOURCE}"
                )
    unreferenced = sorted(set(candidate_by_id) - seen_candidate_ids)
    if unreferenced:
        raise ValueError(
            "tailor coverage has unreferenced candidate ids: "
            + ", ".join(unreferenced)
        )


def restore_tailor(
    state_root: Path,
    *,
    prior_armor: str | None,
    prior_weapon: str | None,
) -> dict:
    """Strip Needle policy and restore a previously captured loadout."""
    from ara import armors as armors_mod
    from ara import weapons as weapons_mod

    state = Path(state_root)
    weapons_mod.unequip_policy(state)
    weapons_mod.set_equipped_weapon(state, prior_weapon)
    if prior_weapon:
        try:
            weapons_mod.equip_policy(state, prior_weapon)
        except Exception:  # noqa: BLE001 - prior weapon may have been removed
            pass
    if prior_armor:
        armors_mod.equip_armor(state, prior_armor)
    else:
        armors_mod.set_active_armor(state, None)
    return {
        "restored_armor": prior_armor,
        "restored_weapon": prior_weapon,
    }


def _complete_tailor_session(state_root: Path, status: str) -> dict:
    if status not in {"finished", "aborted"}:
        raise ValueError(f"invalid tailor session completion status: {status!r}")
    state = Path(state_root)
    record = load_tailor_session(state)
    if record is None:
        raise TailorLifecycleError("no tailor session record found")
    if record.get("status") != "running":
        return record

    result = restore_tailor(
        state,
        prior_armor=record.get("prior_armor"),
        prior_weapon=record.get("prior_weapon"),
    )
    record["status"] = status
    record["restored_at"] = time.time()
    record["restore_result"] = result
    return _write_tailor_session(state, record)


def finish_tailor_session(state_root: Path) -> dict:
    return _complete_tailor_session(state_root, "finished")


def abort_tailor_session(state_root: Path) -> dict:
    return _complete_tailor_session(state_root, "aborted")


__all__ = [
    "TAILOR_ARMOR",
    "NEEDLE_WEAPON",
    "TAILOR_SESSION_REL",
    "TAILOR_CANDIDATE_DESTINATIONS",
    "TAILOR_CANDIDATE_REQUIRED",
    "TAILOR_MIN_CANDIDATES_PER_SOURCE",
    "TailorCandidateRejected",
    "TailorLifecycleError",
    "tailor_session_path",
    "load_tailor_session",
    "equip_tailor",
    "restore_tailor",
    "start_tailor_session",
    "record_tailor_candidate_rejection",
    "add_tailor_candidate",
    "add_tailor_coverage_summary",
    "validate_tailor_coverage_for_proposal",
    "search_source_mats",
    "get_source_mat",
    "finish_tailor_session",
    "abort_tailor_session",
]
