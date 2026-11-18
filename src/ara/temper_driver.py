"""Iterative Tailor driver — pumps Needle source-by-source until coverage.

Why this exists (D4). ``hermes -z``/``chat`` produces one response per
invocation; a full multi-source Temper run needs several. The historical
single long session misread a few validation rejections as a "server
unreachable" outage and gave up with zero candidates. This driver runs Needle
in fresh contained turns focused on ONE source at a time, reads the accepted
candidate count fresh from the tailor session between turns, and bails a source
only after consecutive no-progress turns — so a rejected candidate costs one
turn, not the whole run. Mirrors ``forge/loop_driver.run_exhaust``.

The default agent invocation shells out to::

    ara launch --contain --state-root <state> -- hermes -z <prompt>

with the cert/env wiring a contained MCP session needs. Override via
``agent_invoke=`` (used by tests) to drive the loop without a live model.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ara import temper_lifecycle
from ara import temper_controls


_SOURCE_PROMPT_TEMPLATE = """\
You are running inside ARA as Tailor Robes with the Needle equipped.

Continue the active Tailor session. THIS turn, focus only on source:
{source}

You have emitted {accepted} useful candidate(s) for this source so far; aim
for about {target}. Use the ara-temper MCP tools only:

1. temper.mats_search / temper.mats_get to READ this source's manifest
   elements. Derive candidates only from text you actually read.
2. temper.candidate to emit one reviewable candidate per call. Set the
   destination argument and cite the exact manifest_element_id you read.
3. Set scope to one of: web_app, mobile, hardware, rf_research, anti_crime,
   global.

If a tool call returns an error it is a VALIDATION error (the server is
healthy) — read the correction and resend a fixed candidate. Do not stop
because of a validation rejection. Emit candidates now; no prose preamble.
"""


@dataclass
class SourceResult:
    source: str
    iterations: int
    accepted_before: int
    accepted_after: int
    rc_last: int = 0


@dataclass
class TailorDriverResult:
    iterations: int = 0
    accepted_total: int = 0
    sources: list[SourceResult] = field(default_factory=list)
    stopped_reason: str = ""


def _accepted_count(state_root: Path) -> int:
    record = temper_lifecycle.load_tailor_session(Path(state_root))
    if not record:
        return 0
    return len(record.get("candidates") or [])


def _build_source_prompt(
    record: dict, source: str, iteration: int, accepted: int, target: int,
) -> str:
    return _SOURCE_PROMPT_TEMPLATE.format(
        source=source, accepted=accepted, target=target,
    )


def _default_agent_invoke(
    state_root: Path, prompt: str, *, timeout_s: float = 420.0,
) -> int:
    """Spawn ``ara launch --contain -- hermes -z <prompt>`` for one turn."""
    here = os.path.dirname(os.path.dirname(__file__))  # the src/ root
    mitm_ca = Path(state_root) / "mitm" / "ca.crt"
    hermes_bin = shutil.which("hermes")
    if hermes_bin is None:
        candidate = Path.home() / ".local" / "bin" / "hermes"
        if candidate.is_file():
            hermes_bin = str(candidate)
    if hermes_bin is None:
        sys.stderr.write(
            "[tailor-loop] hermes binary not found on PATH or in ~/.local/bin\n"
        )
        return 127
    env = dict(os.environ)
    env["PYTHONPATH"] = here + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
    if mitm_ca.is_file():
        env.setdefault("SSL_CERT_FILE", str(mitm_ca))
        env.setdefault("REQUESTS_CA_BUNDLE", str(mitm_ca))
    cmd = [
        sys.executable, "-m", "ara.cli", "launch",
        "--contain", "--state-root", str(state_root),
        "--", hermes_bin, "-z", prompt,
    ]
    sys.stderr.write(
        f"[tailor-loop] invoke: {shlex.join(cmd[:8])} ... -z <{len(prompt)} chars>\n"
    )
    try:
        proc = subprocess.run(
            cmd, env=env, check=False, timeout=timeout_s,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("[tailor-loop] timeout — killing agent\n")
        return -1
    if proc.stderr:
        for line in proc.stderr.splitlines()[-20:]:
            sys.stderr.write(f"  | {line}\n")
    return proc.returncode


def run_tailor(
    state_root: Path,
    sources: list[str],
    *,
    target_per_source: int = 12,
    max_iterations_per_source: int = 4,
    no_progress_bail: int = 2,
    iteration_timeout_s: float = 420.0,
    agent_invoke: Callable[[Path, str], int] | None = None,
    prompt_builder: Callable[[dict, str, int, int, int], str] | None = None,
) -> TailorDriverResult:
    """Drive Needle across ``sources`` until each reaches ``target_per_source``
    accepted candidates (or its iteration / no-progress budget runs out).

    Resilient by construction: a per-turn agent failure or validation rejection
    just yields a no-progress turn; the loop continues. Never raises on a
    per-turn agent error."""
    state = Path(state_root)
    record = temper_lifecycle.load_tailor_session(state)
    if record is None or record.get("status") != "running":
        raise temper_lifecycle.TailorLifecycleError(
            "run_tailor requires a running tailor session"
        )
    if agent_invoke is None:
        agent_invoke = lambda sr, pr: _default_agent_invoke(  # noqa: E731
            sr, pr, timeout_s=iteration_timeout_s,
        )
    build = prompt_builder or _build_source_prompt

    result = TailorDriverResult()
    for source in sources:
        src_before = _accepted_count(state)
        consecutive_no_progress = 0
        iterations = 0
        rc_last = 0
        accepted_for_source = 0
        for _ in range(1, max_iterations_per_source + 1):
            if accepted_for_source >= target_per_source:
                break
            before = _accepted_count(state)
            record = temper_lifecycle.load_tailor_session(state) or record
            prompt = build(
                record, source, iterations + 1,
                accepted_for_source, target_per_source,
            )
            try:
                rc_last = agent_invoke(state, prompt)
            except Exception:  # noqa: BLE001 - per-turn failure is non-fatal
                rc_last = -2
            iterations += 1
            result.iterations += 1
            after = _accepted_count(state)
            accepted_for_source += max(0, after - before)
            if after <= before:
                consecutive_no_progress += 1
                if consecutive_no_progress >= no_progress_bail:
                    break
            else:
                consecutive_no_progress = 0
        result.sources.append(SourceResult(
            source=source,
            iterations=iterations,
            accepted_before=src_before,
            accepted_after=_accepted_count(state),
            rc_last=rc_last,
        ))
    result.accepted_total = _accepted_count(state)
    if not result.stopped_reason:
        result.stopped_reason = "sources_complete"
    return result


# ---------------------------------------------------------------------------
# Element-grounded exhaustion (the thorough driver).
#
# run_tailor() above does a shallow per-source pass: it asks Needle to emit
# ~N candidates per mat and lets the model decide what to read. It never walks
# the manifest, so most of a large source is never examined — yielding only a
# handful of guardrails from hundreds of pages.
#
# run_tailor_exhaust() mirrors forge/loop_driver.run_exhaust: it feeds Needle
# SPECIFIC manifest elements in batches (interleaved across mats) and advances
# a cursor until element-coverage hits the target, so EVERY element is examined
# once. Guardrails are naturally sparse (most security text is knowledge, not
# agent policy) — but nothing is skipped, so yield + coverage scale with the
# material instead of the model's search whims.
# ---------------------------------------------------------------------------

_EXHAUST_PROMPT_TEMPLATE = """\
You are running inside ARA as Tailor Robes with the Needle equipped.

Examine these specific source manifest elements (iteration {iteration};
{covered}/{total} elements examined so far) and derive reusable ARMOR
GUARDRAILS where — and ONLY where — the material justifies one. Most
security/methodology text is knowledge, not agent policy, and yields NO
candidate; that is expected and correct.

ELEMENTS THIS BATCH:
{element_block}

For any element that implies a durable agent guardrail (secret handling,
methodology-fit boundary, safety constraint, ask/reply redaction, approval
gate, canary, or alert), emit one temper.candidate that CITES that exact
manifest_element_id. Use temper.mats_get to read an element's full text first.
Set destination and scope on every candidate. Do NOT force a candidate per
element — skip pure-knowledge elements. Use the ara-temper MCP tools only; no
prose preamble; begin tool-calling now.
"""


def _interleave_by_mat(elements: list) -> list:
    """Round-robin elements across their source mat so a partial-coverage pass
    samples every mat proportionally instead of front-loading the first one."""
    by_mat: dict = {}
    for e in elements:
        by_mat.setdefault(getattr(e, "source_mat_id", ""), []).append(e)
    queues = list(by_mat.values())
    out: list = []
    i = 0
    while queues:
        q = queues[i % len(queues)]
        out.append(q.pop(0))
        if not q:
            queues.remove(q)
        else:
            i += 1
    return out


def _build_exhaust_prompt(record, batch, iteration, covered, total) -> str:
    lines = []
    for el in batch:
        preview = (getattr(el, "extracted_text", "") or "").replace("\n", " ").strip()
        if len(preview) > 220:
            preview = preview[:217] + "..."
        kind = getattr(getattr(el, "kind", ""), "value", getattr(el, "kind", ""))
        lines.append(f"- {el.id}  [{kind}, mat {el.source_mat_id}]  {preview}")
    return _EXHAUST_PROMPT_TEMPLATE.format(
        iteration=iteration, covered=covered, total=total,
        element_block="\n".join(lines),
    )


@dataclass
class ExhaustResult:
    iterations: int = 0
    elements_total: int = 0
    elements_examined: int = 0
    accepted_total: int = 0
    coverage: float = 0.0
    stopped_reason: str = ""


def run_tailor_exhaust(
    state_root: Path,
    *,
    target_coverage: float = 1.0,
    batch_size: int = 20,
    max_iterations: int = 80,
    iteration_timeout_s: float = 420.0,
    agent_invoke: Callable[[Path, str], int] | None = None,
    prompt_builder: Callable[[dict, list, int, int, int], str] | None = None,
) -> ExhaustResult:
    """Walk the source forge's manifest elements in batches until
    ``target_coverage`` of them have been examined. Every element is fed to
    Needle exactly once (no re-examination of barren elements), so coverage
    advances deterministically and yield scales with the material."""
    state = Path(state_root)
    record = temper_lifecycle.load_tailor_session(state)
    if record is None or record.get("status") != "running":
        raise temper_lifecycle.TailorLifecycleError(
            "run_tailor_exhaust requires a running tailor session"
        )
    elements = temper_lifecycle._source_manifest_elements(state, record)
    if not elements:
        raise temper_lifecycle.TailorLifecycleError(
            "run_tailor_exhaust requires a source forge with an enumerated "
            "manifest (set source_request.source_forge)"
        )
    ordered = _interleave_by_mat(list(elements))
    total = len(ordered)
    if agent_invoke is None:
        agent_invoke = lambda sr, pr: _default_agent_invoke(  # noqa: E731
            sr, pr, timeout_s=iteration_timeout_s,
        )
    build = prompt_builder or _build_exhaust_prompt

    result = ExhaustResult(elements_total=total)
    cursor = 0
    for iteration in range(1, max_iterations + 1):
        if cursor >= total:
            result.stopped_reason = "all_elements_examined"
            break
        if cursor / total >= target_coverage:
            result.stopped_reason = "target_coverage_reached"
            break
        batch = ordered[cursor:cursor + batch_size]
        record = temper_lifecycle.load_tailor_session(state) or record
        prompt = build(record, batch, iteration, cursor, total)
        try:
            agent_invoke(state, prompt)
        except Exception:  # noqa: BLE001 - per-turn failure is non-fatal
            pass
        cursor += len(batch)
        result.iterations += 1
        result.elements_examined = cursor
    result.accepted_total = _accepted_count(state)
    result.coverage = cursor / total if total else 1.0
    if not result.stopped_reason:
        result.stopped_reason = "max_iterations_reached"
    return result


__all__ = [
    "SourceResult",
    "TailorDriverResult",
    "ExhaustResult",
    "run_tailor",
    "run_tailor_exhaust",
]


# --- stateless per-element driver -----------------------------------------
# The agentic exhaust driver (above) runs Needle as one long contained-hermes
# turn per batch: it re-reads each element via MCP and the growing conversation
# is re-sent on every step, so late steps drag and turns time out. A temper
# forge is a MAP, not a conversation — each element yields (or not) on its own.
# This driver makes ONE direct, stateless model call per element (constant
# context), sets the citation to the element under examination (grounding is
# guaranteed — the model cannot fabricate a manifest id), and routes every
# candidate through the same ``add_tailor_candidate`` validation the MCP uses,
# with a single validate-retry that feeds the rejection reason back.

_STATELESS_SYSTEM_PROMPT = """\
You extract CROSS-CUTTING AGENT HARDENING from ONE security source element.

A temper hardens how the AGENT behaves REGARDLESS of the task — it is NOT the
task's tool list. So emit a candidate ONLY when the element implies a durable,
task-INDEPENDENT rule about one of:
  (A) OUTPUT HYGIENE — the agent must not leak secrets/credentials/PII/keys in
      its replies (canary to detect the shape, or rhema to redact it).
  (B) SAFETY DOCTRINE — a standing principle the agent must always honor (never
      disclose raw keys, never exfiltrate evidence, never destroy data).
  (C) DESTRUCTIVE-ACTION GATE — an irreversible/damaging action that should
      require operator approval no matter the engagement (flash/brick a device,
      wipe, factory-reset).

Do NOT emit:
  - per-tool DENY rules (deny jtag, deny esptool, deny adb...). Which tools an
    agent MAY run is the WEAPON's allowlist scoped by the TRIM, not a temper.
    A denylist is never complete and just gets re-allowed per task — skip it.
  - "adhere to WSTG/ISVS/ISTG methodology" boundaries. Methodology fit is the
    weapon/trim's job (what the task is expected to do), not armor doctrine.
  - anything describing what the TARGET should do (that is knowledge, not agent
    policy).

MOST elements imply no cross-cutting hardening -> return []. A whole methodology
section, a tool catalog entry, a single finding usually yields NOTHING. Only the
rare element about secret/credential exposure or an irreversible action yields a
candidate. Prefer 0 over a weak or task-specific one.

Output ONLY a JSON array (or []). Each candidate object:
{
 "destination": one of [canary, rhema, absolute, approval_gate],
 "thread_type": one of [secret_handling, prompt_mediation, safety_boundary, operator_approval],
 "title": short, "rationale": why this guards the AGENT across tasks,
 "risk": the agent misbehavior it prevents, "proposed_text": the guardrail,
 "control": <control object, below>,
 "scope": ["global"]   // cross-cutting hardening is almost always global
}
control by destination (pattern/trigger MUST be a regex, NEVER prose):
 - canary:  {"signal":"...","pattern":"(password|secret|api[_-]?key|token)\\\\s*[:=]\\\\s*\\\\S{8,}","severity":"high","trip_when":"...","response":"redact + flag"}
 - rhema:   {"action":"rewrite","layer":"reply","pattern":"<targeted secret-shaped regex>","rationale":"..."}
 - absolute:{"principle":"<standing safety doctrine>","allowed":[...],"restricted":[...],"exception_path":"..."}
 - approval_gate (ONLY for irreversible/destructive actions): {"surface":"exec","trigger":"<regex of the destructive op>","required_context":"...","approval_prompt":"..."}

Derive ONLY from the element text shown. Do NOT call tools. Do NOT fabricate.
Output ONLY the JSON array."""

_DEFAULT_THREAD_BY_DEST = {
    "bulwark": "runtime_boundary",
    "canary": "secret_handling",
    "absolute": "methodology_fit",
    "rhema": "prompt_mediation",
    "approval_gate": "operator_approval",
    "alert_hook": "alert_signal",
    "trim_template": "trim_fit",
}


@dataclass
class StatelessResult:
    elements_total: int = 0
    elements_examined: int = 0
    accepted_total: int = 0
    rejected_total: int = 0
    coverage: float = 0.0
    stopped_reason: str = ""


def _default_model_invoke(
    system: str, user: str, *, endpoint: str | None = None,
    model: str = "qwen/qwen3.6-35b-a3b", timeout_s: float = 90.0,
) -> str:
    """One stateless chat call. Returns the message content string."""
    import json
    import urllib.request

    url = (
        endpoint
        or os.environ.get("ARA_TEMPER_MODEL_ENDPOINT")
        or "http://127.0.0.1:1234/v1/chat/completions"
    )
    body = json.dumps({
        "model": model,
        "temperature": 0.2,
        "max_tokens": 1200,
        # vLLM honours this to disable qwen3 thinking; harmless elsewhere.
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read())
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def _parse_json_candidates(text: str) -> list:
    """Extract a JSON array (or a single object) from a model reply."""
    import json

    t = str(text or "").replace("```json", "").replace("```", "").strip()
    i, j = t.find("["), t.rfind("]")
    if 0 <= i < j:
        try:
            data = json.loads(t[i:j + 1])
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(t[i:j + 1])
            if isinstance(obj, dict):
                return [obj]
        except Exception:  # noqa: BLE001
            pass
    return []


_METHODOLOGY_LIST_FIELDS = ("expected", "unexpected", "requires_context",
                            "never_without_approval")
_SAFETY_LIST_FIELDS = ("surfaces", "allowed", "restricted", "requires_context",
                       "never_without_approval")


def _coerce_task_family(value: object, scope: list) -> str:
    """Map a model-emitted task_family onto the fixed vocabulary."""
    v = str(value or "").strip().lower()
    if v in temper_controls.SCOPE_TAGS:
        return v
    for tag, needles in (
        ("hardware", ("hardware", "firmware", "iot", "embedded", "device")),
        ("web_app", ("web", "http", "appsec", "wstg")),
        ("mobile", ("mobile", "android", "ios")),
        ("rf_research", ("rf", "radio", "wireless", "sdr")),
        ("anti_crime", ("crime", "surveillance", "alpr", "flock")),
    ):
        if any(n in v for n in needles):
            return tag
    for tag in scope or []:
        if str(tag).strip() in temper_controls.SCOPE_TAGS:
            return str(tag).strip()
    return "global"


def _as_str_list(value: object) -> list | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value] if value.strip() else None
    if isinstance(value, list):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out or None
    return None


def _coerce_context(mc: object, scope: list, *, list_fields, is_methodology) -> object:
    """Normalize methodology_context/safety_constraints into the validated
    shape: enum task_family, list-of-strings fields, empties dropped."""
    if not isinstance(mc, dict):
        return mc
    out = dict(mc)
    if is_methodology:
        out["task_family"] = _coerce_task_family(out.get("task_family"), scope)
    for field in list_fields:
        coerced = _as_str_list(out.get(field))
        if coerced is None:
            out.pop(field, None)
        else:
            out[field] = coerced
    return out


def _finalize_stateless_candidate(cand: dict, el) -> dict:
    """Fill boilerplate the model shouldn't invent and pin the citation to the
    element under examination (grounding is guaranteed, not model-asserted).
    Deterministically coerce the enum/list-shaped fields the strict validator
    rejects (task_family, list fields, thread_type) — the same job the MCP's
    validate-retry does, without an extra round-trip."""
    out = dict(cand)
    elem_id = getattr(el, "id", "") or ""
    out.setdefault("source", elem_id or getattr(el, "source_mat_id", ""))
    out.setdefault("default_decision", "keep_for_review")
    out["citations"] = [{"manifest_element_id": elem_id}]
    if out.get("thread_type") not in temper_controls.THREAD_TYPES:
        out["thread_type"] = _DEFAULT_THREAD_BY_DEST.get(
            out.get("destination"), "methodology_fit",
        )
    scope = out.get("scope") if isinstance(out.get("scope"), list) else []
    if "methodology_context" in out:
        out["methodology_context"] = _coerce_context(
            out["methodology_context"], scope,
            list_fields=_METHODOLOGY_LIST_FIELDS, is_methodology=True)
    if "safety_constraints" in out:
        out["safety_constraints"] = _coerce_context(
            out["safety_constraints"], scope,
            list_fields=_SAFETY_LIST_FIELDS, is_methodology=False)
    return out


def _existing_titles(state: Path) -> list:
    record = temper_lifecycle.load_tailor_session(state) or {}
    return [str(c.get("title") or "") for c in (record.get("candidates") or [])]


def run_tailor_stateless(
    state_root: Path,
    *,
    max_elements: int | None = None,
    element_text_cap: int = 6000,
    retry_on_reject: int = 1,
    model_invoke: Callable[[str, str], str] | None = None,
    model_endpoint: str | None = None,
    model_name: str = "qwen/qwen3.6-35b-a3b",
    timeout_s: float = 90.0,
) -> StatelessResult:
    """Stateless map over the source forge's manifest elements: one direct
    model call per element, constant context. Every candidate is grounded to
    the element under examination and validated via ``add_tailor_candidate``
    (with one validate-retry). Requires a running Tailor session.
    """
    state = Path(state_root)
    record = temper_lifecycle.load_tailor_session(state)
    if record is None or record.get("status") != "running":
        raise temper_lifecycle.TailorLifecycleError(
            "run_tailor_stateless requires a running tailor session"
        )
    elements = temper_lifecycle._source_manifest_elements(state, record)
    if not elements:
        raise temper_lifecycle.TailorLifecycleError(
            "run_tailor_stateless requires a source forge with an enumerated "
            "manifest (set source_request.source_forge)"
        )
    ordered = _interleave_by_mat(list(elements))
    if max_elements is not None:
        ordered = ordered[:max_elements]

    invoke = model_invoke or (
        lambda sys_p, usr_p: _default_model_invoke(
            sys_p, usr_p, endpoint=model_endpoint,
            model=model_name, timeout_s=timeout_s,
        )
    )

    result = StatelessResult(elements_total=len(ordered))
    seen_titles = _existing_titles(state)
    seen_norm = {t.strip().lower() for t in seen_titles if t.strip()}
    for el in ordered:
        text = (getattr(el, "extracted_text", "") or "")[:element_text_cap]
        user = _build_stateless_user(el, text, seen_titles)
        try:
            raw = invoke(_STATELESS_SYSTEM_PROMPT, user)
        except Exception:  # noqa: BLE001 — a per-element failure is non-fatal
            raw = ""
        for cand in _parse_json_candidates(raw):
            if not isinstance(cand, dict):
                continue
            final = _finalize_stateless_candidate(cand, el)
            norm = str(final.get("title") or "").strip().lower()
            if norm and norm in seen_norm:
                continue  # skip duplicate titles (within run + prior candidates)
            if _add_stateless_candidate(state, final, el, invoke, retry_on_reject):
                result.accepted_total += 1
                if norm:
                    seen_norm.add(norm)
                    seen_titles.append(str(final["title"]))
            else:
                result.rejected_total += 1
        result.elements_examined += 1

    result.coverage = (
        result.elements_examined / len(ordered) if ordered else 1.0
    )
    result.stopped_reason = "all_elements_examined"
    return result


def _build_stateless_user(el, text: str, seen_titles: list) -> str:
    return (
        f"manifest_element_id: {getattr(el, 'id', '')}\n\n"
        f"ELEMENT TEXT:\n{text}\n\n"
        f"Already-emitted titles (do NOT duplicate): {seen_titles[-12:]}\n\n"
        "Output the JSON array now."
    )


def _add_stateless_candidate(
    state: Path, cand: dict, el, invoke: Callable[[str, str], str], retries: int,
) -> bool:
    """Add via the authoritative validator; on rejection, feed the reason back
    to the model once (mirrors the MCP validate-retry) and try the fix."""
    try:
        temper_lifecycle.add_tailor_candidate(state, cand)
        return True
    except temper_lifecycle.TailorCandidateRejected as exc:
        if retries <= 0:
            return False
        fix_user = (
            f"Your candidate titled {cand.get('title')!r} for element "
            f"{getattr(el, 'id', '')} was REJECTED by validation:\n{exc}\n\n"
            "Return a single corrected JSON object (same schema) that fixes the "
            "error. Output ONLY the JSON object."
        )
        try:
            raw = invoke(_STATELESS_SYSTEM_PROMPT, fix_user)
        except Exception:  # noqa: BLE001
            return False
        for fixed in _parse_json_candidates(raw):
            if not isinstance(fixed, dict):
                continue
            final = _finalize_stateless_candidate(fixed, el)
            try:
                temper_lifecycle.add_tailor_candidate(state, final)
                return True
            except temper_lifecycle.TailorCandidateRejected:
                continue
        return False
    except temper_lifecycle.TailorLifecycleError:
        return False
