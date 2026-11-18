"""Operator "Polish" actions — build the focused directive an operator dispatches
against one finding / lead / fact.

Pure string assembly (no I/O) so it is trivially testable. The caller loads the
item dict from the session store and hands it here; the returned string becomes
the ``prompt`` (Operator initial message) of a short, budgeted focused task run.
"""
from __future__ import annotations

# Default wall-clock budget for a focused task (seconds). The agent can request
# more at runtime via `ara task-extend` (see _EXT_NOTE).
DEFAULT_BUDGET_SECONDS = 600  # 10 minutes

# (item_type, mode) -> (button label, task_depth, goal-block builder key)
MODES: dict = {
    ("finding", "confirm"): ("Confirm & write up", "exploit_validation"),
    ("finding", "deep_verify"): ("Deep-verify (refute)", "exploit_validation"),
    ("finding", "retest"): ("Re-test", "safe_mutation"),
    ("finding", "exploitability"): ("Assess exploitability", "exploit_validation"),
    ("lead", "investigate"): ("Investigate", "full"),
    ("lead", "confirm_lead"): ("Confirm legit", "passive_probe"),
    ("fact", "escalate"): ("Escalate to lead", "full"),
    ("fact", "corroborate"): ("Corroborate", "passive_probe"),
}

_EXT_NOTE = (
    "\n\nBUDGET: you have ~10 minutes for this focused task. If you genuinely "
    "need more time, request it WITH A REASON by running:\n"
    "    python3.9 -m ara.cli task-extend --minutes N --justification \"...\"\n"
    "Small extensions are auto-granted up to a cap; larger ones need operator "
    "approval. Do not block waiting on it — keep working and wrap up cleanly."
)

_FOCUS = (
    "\n\nDo NOT enumerate broadly, chase other leads, or start new surface. "
    "Focus ONLY on the item above. When you have committed the outcome, stop."
)


def is_valid(item_type: str, mode: str) -> bool:
    return (item_type, mode) in MODES


def task_depth_for(item_type: str, mode: str) -> str:
    return MODES.get((item_type, mode), ("", "full"))[1]


def label_for(item_type: str, mode: str) -> str:
    return MODES.get((item_type, mode), (mode, ""))[0]


def _g(item: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = item.get(k)
        if v not in (None, "", [], {}):
            return str(v)
    return default


def _finding_ctx(item: dict) -> str:
    ev = item.get("evidence_refs") or []
    ev_s = ", ".join(str(x) for x in ev) if ev else "(none cited)"
    return (
        f"Finding {_g(item, 'id')}: \"{_g(item, 'title')}\"\n"
        f"  severity={_g(item, 'severity_name', 'severity', default='?')}  "
        f"status={_g(item, 'status', default='?')}  "
        f"category={_g(item, 'category', default='?')}\n"
        f"  cited evidence: {ev_s}\n"
        f"  current confirmation_method: {_g(item, 'confirmation_method', default='(none)')}"
    )


def _lead_ctx(item: dict) -> str:
    return (
        f"Lead {_g(item, 'id')}: \"{_g(item, 'title')}\"\n"
        f"  status={_g(item, 'status', default='?')}  "
        f"priority={_g(item, 'priority_operator_override', 'priority_computed', default='?')}\n"
        f"  reasoning: {_g(item, 'reasoning', 'note', 'detail', default='(none)')}"
    )


def _fact_ctx(item: dict) -> str:
    return (
        f"Fact {_g(item, 'id')}: {_g(item, 'note', 'title', 'statement')}\n"
        f"  cited evidence: {', '.join(str(x) for x in (item.get('evidence_refs') or [])) or '(none)'}"
    )


_GOALS: dict = {
    ("finding", "confirm"): (
        "COMMIT-FIRST. Your ONE required deliverable is an updated record for "
        "THIS finding — not a new engagement, not broad enumeration. Nothing "
        "else counts as done.\n"
        " 1. Re-test this finding against its own endpoint(s) NOW to reproduce "
        "it (reuse its cited evidence + the endpoints in its title/notes).\n"
        " 2. THE MOMENT you have a reproducing response, IMMEDIATELY call "
        "`mcp_ara_forge_finding_file` to UPDATE this finding — in your FIRST or "
        "SECOND turn, do NOT defer to a later 'writeup' phase. Provide ALL of:\n"
        "      - title (keep/refine it)\n"
        "      - impact: what an attacker actually gains in this environment\n"
        "      - affected_assets: the exact vulnerable URLs / endpoints / params\n"
        "      - remediation: the concrete fix\n"
        "      - evidence_refs: ids of the reproducing captures\n"
        "      - confirmation_method: the checkable identifiers you reproduced\n"
        "        (URL, HTTP status, the exact injected marker in the response)\n"
        " 3. If it does NOT reproduce, call `finding.file` marking it "
        "false_positive or non_exploitable with the reason. Do not pad it.\n"
        "Commit the finding update EARLY. Only after it is filed may you "
        "strengthen it if budget remains. If you cannot finish before the "
        "deadline, request more time (below) BEFORE the deadline — but file "
        "the finding update first."
    ),
    ("finding", "deep_verify"): (
        "GOAL: adversarially REFUTE this finding. Assume it is a false positive\n"
        "and actively try to prove it is not real or not exploitable. Only if you\n"
        "cannot refute it, confirm it with fresh reproducing evidence. Record the\n"
        "refutation attempt either way."
    ),
    ("finding", "retest"): (
        "GOAL: re-run ONLY the reproduction for this finding and report whether it\n"
        "is still present or now fixed. Refresh the cited evidence. Do not change\n"
        "the severity unless it clearly no longer reproduces."
    ),
    ("finding", "exploitability"): (
        "GOAL: determine the real-world exploitability/impact of this finding\n"
        "within scope (what an attacker actually gains, chainability). Update the\n"
        "severity and impact note with concrete justification."
    ),
    ("lead", "investigate"): (
        "GOAL: investigate this lead to a decision.\n"
        " - If it leads to a real issue: file a finding (with evidence) and mark\n"
        "   the lead promoted.\n"
        " - If it is a dead end: close it (dismiss) with the reason.\n"
        "Do not leave it open."
    ),
    ("lead", "confirm_lead"): (
        "GOAL: decide whether this lead is a legitimate avenue worth a finding.\n"
        "Gather just enough evidence to judge. Mark it confirmed (worth pursuing)\n"
        "or leave it open with a clear note on what would confirm it."
    ),
    ("fact", "escalate"): (
        "GOAL: turn this observation into a concrete, testable vulnerability\n"
        "hypothesis and file it as a lead (lead.file) linked to this fact."
    ),
    ("fact", "corroborate"): (
        "GOAL: gather additional evidence that supports or refutes this fact.\n"
        "Append the evidence and note whether it still holds."
    ),
}

_CTX_FN = {"finding": _finding_ctx, "lead": _lead_ctx, "fact": _fact_ctx}


def build_directive(item_type: str, item: dict, mode: str,
                    instructions: str = "") -> str:
    """Assemble the focused-task directive (the injected Operator initial message)."""
    ctx_fn = _CTX_FN.get(item_type)
    goal = _GOALS.get((item_type, mode))
    if ctx_fn is None or goal is None:
        raise ValueError(f"unknown polish action: {item_type}/{mode}")
    parts = [
        "FOCUSED OPERATOR TASK — do ONLY this, then stop.",
        "",
        ctx_fn(item),
        "",
        goal,
    ]
    instructions = (instructions or "").strip()
    if instructions:
        parts.append(f"\nOPERATOR CUSTOM INSTRUCTIONS: {instructions}")
    parts.append(_FOCUS)
    parts.append(_EXT_NOTE)
    return "\n".join(parts)
