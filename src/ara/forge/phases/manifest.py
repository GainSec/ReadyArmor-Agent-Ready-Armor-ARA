"""MANIFEST phase: enumerate mats → manifest.jsonl + coverage report.

Re-running on an existing forge preserves element ids for unchanged
content (orchestrator §5.1 — content-hash-based ids ensure stability).
Operator-dropped elements survive a re-manifest.
"""
from __future__ import annotations

import time
from pathlib import Path

from ara.forge import state as fstate
from ara.forge.manifest.element import (
    ManifestElement,
    read_manifest,
    write_manifest,
)
from ara.forge.manifest.enumerate import (
    coverage,
    enumerate_mats,
    write_coverage,
)
from ara.forge.session import ForgePhase, ForgeSession
from ara.runtime import audit as raudit


def run_manifest(
    sess: ForgeSession, *, advance: bool = True,
) -> list[ManifestElement]:
    """Enumerate mats → write manifest.jsonl + coverage report.

    By default (``advance=True``) auto-transitions MANIFEST → REVIEW
    after a successful generation, since callers nearly always want
    that. Pass ``advance=False`` to keep the session in MANIFEST (e.g.
    for re-runs while already in REVIEW).
    """
    if sess.phase not in (ForgePhase.MANIFEST, ForgePhase.REVIEW):
        raise ValueError(
            f"run_manifest requires MANIFEST or REVIEW phase "
            f"(current: {sess.phase.value})"
        )
    mats_dir = fstate.forge_mats_dir(sess.state_root, sess.forge_id)
    keywords = _pack_keywords(sess)
    fresh = enumerate_mats(
        mats_dir, report_section_keywords=keywords,
    )
    manifest_path = fstate.forge_manifest_path(sess.state_root, sess.forge_id)
    prior = read_manifest(manifest_path)
    merged = _merge_with_prior_drops(fresh, prior)
    write_manifest(manifest_path, merged)
    cov = coverage(merged)
    cov_path = manifest_path.with_name("manifest_coverage.json")
    write_coverage(cov_path, cov)
    raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {
            "ts": time.time(),
            "total_elements": cov.total_elements,
            "total_mats": cov.total_mats,
            "by_kind": cov.by_kind,
        },
        kind="manifest.generated",
    )
    if advance and sess.phase == ForgePhase.MANIFEST:
        sess.transition(ForgePhase.REVIEW, reason="manifest generated",
                        meta={"elements": cov.total_elements})
    return merged


def _merge_with_prior_drops(
    fresh: list[ManifestElement],
    prior: list[ManifestElement],
) -> list[ManifestElement]:
    """Preserve operator-set ``dropped_by_operator`` flags from prior runs.

    Element ids are content-hash stable, so a fresh element with the same
    id as a prior dropped element keeps the dropped flag.
    """
    drops = {e.id for e in prior if e.dropped_by_operator}
    if not drops:
        return fresh
    out: list[ManifestElement] = []
    for el in fresh:
        if el.id in drops:
            out.append(ManifestElement(**{**el.__dict__, "dropped_by_operator": True}))
        else:
            out.append(el)
    return out


def drop_element(sess: ForgeSession, element_id: str) -> None:
    """Operator drops an element. Manifest is rewritten in place."""
    if sess.phase not in (ForgePhase.REVIEW, ForgePhase.MANIFEST):
        raise ValueError("drop_element only valid during MANIFEST/REVIEW")
    manifest_path = fstate.forge_manifest_path(sess.state_root, sess.forge_id)
    elements = read_manifest(manifest_path)
    found = False
    out: list[ManifestElement] = []
    for el in elements:
        if el.id == element_id:
            out.append(ManifestElement(**{**el.__dict__, "dropped_by_operator": True}))
            found = True
        else:
            out.append(el)
    if not found:
        raise KeyError(f"unknown element id: {element_id}")
    write_manifest(manifest_path, out)
    raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {"element_id": element_id}, kind="manifest.dropped",
    )


def undrop_element(sess: ForgeSession, element_id: str) -> None:
    """Operator reinstates a previously dropped element."""
    if sess.phase not in (ForgePhase.REVIEW, ForgePhase.MANIFEST):
        raise ValueError("undrop_element only valid during MANIFEST/REVIEW")
    manifest_path = fstate.forge_manifest_path(sess.state_root, sess.forge_id)
    elements = read_manifest(manifest_path)
    found = False
    out: list[ManifestElement] = []
    for el in elements:
        if el.id == element_id:
            out.append(ManifestElement(**{**el.__dict__, "dropped_by_operator": False}))
            found = True
        else:
            out.append(el)
    if not found:
        raise KeyError(f"unknown element id: {element_id}")
    write_manifest(manifest_path, out)
    raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {"element_id": element_id}, kind="manifest.undropped",
    )


def manifest_path_for(state_root: Path, forge_id: str) -> Path:
    return fstate.forge_manifest_path(state_root, forge_id)


def _pack_keywords(sess: ForgeSession) -> tuple[str, ...] | None:
    """Resolve the pack's REPORT_SECTION keywords for this forge.

    Returns None when no pack is configured (falls back to defaults)
    or when the pack declares no keywords.
    """
    pack_name = sess.config.alphabet_pack
    if not pack_name:
        return None
    try:
        from ara.forge.alphabets.pack import resolve_pack
        pack = resolve_pack(pack_name, sess.state_root)
    except (FileNotFoundError, Exception):
        return None
    kws = pack.manifest_detection.report_section_keywords
    if not kws:
        return None
    return tuple(kws)


def approve_manifest(sess: ForgeSession) -> None:
    """Operator approves manifest → transition into EXHAUST.

    Caller should be in REVIEW. Goes REVIEW → EXHAUST.
    """
    if sess.phase != ForgePhase.REVIEW:
        raise ValueError(
            f"approve_manifest requires REVIEW phase (current: {sess.phase.value})"
        )
    sess.transition(ForgePhase.EXHAUST, reason="manifest approved")
