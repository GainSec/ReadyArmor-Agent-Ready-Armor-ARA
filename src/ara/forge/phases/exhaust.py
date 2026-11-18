"""EXHAUST phase: extraction recording + coverage gate.

The agent-side extraction loop (which actually invokes the model and
emits via typed channels) lives outside this module — items 1.1, 1.4,
etc. own those channels. EXHAUST here owns:

- :func:`record_extraction` — single entry point for typed-channel
  extractions, validates manifest_element_id against the manifest,
  appends to extractions.jsonl.
- :func:`coverage_state` — computes element-coverage stats.
- :func:`advance_to_synthesize` — checked transition guarded by
  coverage threshold (or operator override).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ara.forge import state as fstate
from ara.forge.manifest.element import read_manifest
from ara.forge.session import ForgePhase, ForgeSession
from ara.runtime import audit as raudit


_EXTRACT_CHANNELS = {
    "fact.assert",
    "extract.proc",
    "extract.conc",
    "extract.fact_shape",
    "extract.store",
    "extract.resource",
    "extract.line",
    # Policy-extraction channels — these compose into the weapon's
    # policy/{rhema,bulwark,WALLS}.md at synthesize time.
    "extract.rhema_rule",
    "extract.bulwark_rule",
    "extract.walls_path",
    # Pack-forge schema-declaration channels (the `pen` weapon).
    "propose.alphabet",
    "propose.binding",
    "propose.chunk",
    "propose.detection",
}


@dataclass(frozen=True)
class ExtractionEvent:
    ts: float
    manifest_element_id: str
    channel: str
    body: dict
    extraction_id: str
    citation: dict = field(default_factory=dict)

    def to_jsonable(self) -> dict:
        return asdict(self)


class CoverageError(Exception):
    pass


def record_extraction(
    sess: ForgeSession,
    *,
    manifest_element_id: str,
    channel: str,
    body: dict,
    extraction_id: str,
    citation: dict | None = None,
) -> ExtractionEvent:
    if sess.phase != ForgePhase.EXHAUST:
        raise ValueError(
            f"record_extraction requires EXHAUST (current: {sess.phase.value})"
        )
    if channel not in _EXTRACT_CHANNELS:
        raise ValueError(f"unknown extraction channel: {channel!r}")
    manifest_path = fstate.forge_manifest_path(sess.state_root, sess.forge_id)
    elements = read_manifest(manifest_path)
    valid_ids = {e.id for e in elements if not e.dropped_by_operator}
    if manifest_element_id not in valid_ids:
        raise ValueError(
            f"manifest_element_id {manifest_element_id!r} not in manifest"
        )
    evt = ExtractionEvent(
        ts=time.time(),
        manifest_element_id=manifest_element_id,
        channel=channel,
        body=body,
        extraction_id=extraction_id,
        citation=citation or {"manifest_element_id": manifest_element_id},
    )
    out_path = fstate.forge_extractions_path(sess.state_root, sess.forge_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a") as f:
        f.write(json.dumps(evt.to_jsonable(), sort_keys=True) + "\n")
    raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {"extraction_id": extraction_id, "channel": channel,
         "manifest_element_id": manifest_element_id},
        kind=f"extract.{channel}",
    )
    return evt


def read_extractions(state_root: Path, forge_id: str) -> list[ExtractionEvent]:
    path = fstate.forge_extractions_path(state_root, forge_id)
    if not path.exists():
        return []
    out: list[ExtractionEvent] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(ExtractionEvent(
                ts=d["ts"],
                manifest_element_id=d["manifest_element_id"],
                channel=d["channel"],
                body=d["body"],
                extraction_id=d["extraction_id"],
                citation=d.get("citation") or {},
            ))
    return out


@dataclass(frozen=True)
class CoverageState:
    total_elements: int
    elements_with_extractions: int
    fraction: float
    pending: list[str]


def coverage_state(sess: ForgeSession) -> CoverageState:
    manifest_path = fstate.forge_manifest_path(sess.state_root, sess.forge_id)
    elements = [e for e in read_manifest(manifest_path) if not e.dropped_by_operator]
    extractions = read_extractions(sess.state_root, sess.forge_id)
    covered = {e.manifest_element_id for e in extractions}
    total = len(elements)
    hit = sum(1 for e in elements if e.id in covered)
    pending = [e.id for e in elements if e.id not in covered]
    return CoverageState(
        total_elements=total,
        elements_with_extractions=hit,
        fraction=(hit / total) if total else 1.0,
        pending=pending,
    )


def advance_to_review(
    sess: ForgeSession,
    *,
    operator_force: bool = False,
) -> CoverageState:
    if sess.phase != ForgePhase.EXHAUST:
        raise ValueError(
            f"advance_to_review requires EXHAUST (current: {sess.phase.value})"
        )
    cov = coverage_state(sess)
    threshold = sess.config.coverage_threshold
    if cov.fraction < threshold and not operator_force:
        raise CoverageError(
            f"coverage {cov.fraction:.2%} < threshold {threshold:.2%}; "
            f"{len(cov.pending)} elements still pending"
        )
    sess.transition(
        ForgePhase.REVIEW, reason="exhaust complete",
        meta={
            "coverage_fraction": cov.fraction,
            "elements_covered": cov.elements_with_extractions,
            "elements_total": cov.total_elements,
            "operator_forced": operator_force,
        },
    )
    return cov
