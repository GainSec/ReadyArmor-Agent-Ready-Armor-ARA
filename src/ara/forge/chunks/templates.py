"""Forge-default chunk templates."""
from __future__ import annotations

from ara.forge.chunks.chunkdef import ChunkDef, ExitTarget, GemRef


PROBE_TEMPLATE = ChunkDef(
    id="probe",
    title="Probe — investigative exploration",
    prompt_fragment=(
        "You are in probe mode. Your job is to investigate freely within "
        "scope — fetch, observe, capture evidence, build hypotheses. "
        "You are NOT here to file findings. Findings come later, in writeup."
    ),
    gems=[
        GemRef(layer="fs", rule_subset="probe_fs"),
        GemRef(layer="exec", rule_subset="probe_exec"),
        GemRef(layer="net", rule_subset="scope_strict"),
    ],
    tool_surface=[
        "evidence.search", "evidence.get",
        "facts.query", "facts.neighbors", "facts.history",
        "fact.assert", "lead.file", "mats.search",
        # NOT in surface: finding.file, finding.sign_off
    ],
    exits={
        "<hypothesis_filed>": ExitTarget(
            tag="<hypothesis_filed>", next_chunk_id="probe",
            requires_caller="agent",
            description="Filed a lead; continue probing",
        ),
        "<need_more_evidence>": ExitTarget(
            tag="<need_more_evidence>", next_chunk_id="probe",
            requires_caller="agent",
        ),
        "<investigation_done>": ExitTarget(
            tag="<investigation_done>", next_chunk_id="enum",
            requires_caller="agent",
            description="Wrap up; return to enum",
        ),
        "<readied_for_writeup>": ExitTarget(
            tag="<readied_for_writeup>", next_chunk_id="intel",
            requires_caller="agent",
            description="Enough evidence; advance to intel",
        ),
        "<pause_for_operator>": ExitTarget(
            tag="<pause_for_operator>", next_chunk_id="operator_review",
            requires_caller="agent",
        ),
    },
    can_emit_finding=False,
    is_terminal=False,
    is_entry=False,
)
