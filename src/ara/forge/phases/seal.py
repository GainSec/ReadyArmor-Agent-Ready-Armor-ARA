"""SEAL phase: validators + weapon write.

Validators run in order; first hard-fail aborts the seal and returns
the session to REVIEW. Soft warnings collected and emitted to audit but
do not block.

Downstream skeletons hook in additional validators by appending to
:data:`SEAL_VALIDATORS`. v1 ships with the orchestrator-owned core
validators only:

- ``validate_manifest_coverage``  (orchestrator §4.5 #1)
- ``validate_citation_integrity`` (orchestrator §4.5 #2)
- ``validate_weapon_target_id``   (basic sanity)

Item-owned validators (chunk-graph linter 2.3, channel catalog 4.1,
search index 3.1, severity rubric 1.4, style profile 2.4, evidence
shape 3.2) register on import via :func:`register_validator`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ara.forge import state as fstate
from ara.forge.alphabets.pack import load_builtin_pack, resolve_pack
from ara.forge.alphabets.projector import (
    merge_starter,
    project as project_alphabets,
    validate_foreign_keys,
    write_derived,
)
from ara.forge.catalog.doc import write_catalog_md
from ara.forge.catalog.generator import (
    detect_isolated,
    generate_catalog,
    write_catalog_json,
)
from ara.forge.index.builder import (
    FTSIndex,
    index_weapon_components,
)
from ara.forge.phases.exhaust import coverage_state, read_extractions
from ara.forge.phases.synthesize import WeaponDraft, load_draft
from ara.forge.session import ForgePhase, ForgeSession, ForgeKind
from ara.runtime import audit as raudit

# Side-effect imports: each subsystem's channel module registers its
# Channel subclasses with the class registry on import. The catalog
# generator (4.1) introspects that registry, so these imports must
# happen before any seal runs. Without them, the weapon's channel
# catalog ships empty — which is the v1 gap this addresses.
import ara.runtime.facts.channels  # noqa: F401
import ara.runtime.leads.channels  # noqa: F401
import ara.runtime.findings.channels  # noqa: F401
import ara.runtime.anomaly.channels  # noqa: F401
import ara.runtime.style.channels  # noqa: F401


@dataclass
class SealResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    weapon_id: str | None = None
    weapon_hash: str | None = None
    seal_audit_hash: str | None = None


# ---------- validator framework ----------

ValidatorFn = Callable[[ForgeSession, WeaponDraft], "ValidatorReport"]


@dataclass
class ValidatorReport:
    name: str
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_VALIDATORS: list[ValidatorFn] = []


def register_validator(fn: ValidatorFn) -> ValidatorFn:
    if fn not in _VALIDATORS:
        _VALIDATORS.append(fn)
    return fn


def all_validators() -> list[ValidatorFn]:
    return list(_VALIDATORS)


def reset_validators_for_tests() -> None:
    _VALIDATORS.clear()


# ---------- core validators (orchestrator-owned) ----------

def validate_manifest_coverage(sess: ForgeSession, _draft: WeaponDraft) -> ValidatorReport:
    rep = ValidatorReport(name="manifest_coverage")
    cov = coverage_state(sess)
    threshold = sess.config.coverage_threshold
    if cov.fraction < threshold:
        rep.failures.append(
            f"manifest coverage {cov.fraction:.2%} < threshold {threshold:.2%}; "
            f"{len(cov.pending)} elements pending"
        )
    return rep


def validate_citation_integrity(sess: ForgeSession, draft: WeaponDraft) -> ValidatorReport:
    rep = ValidatorReport(name="citation_integrity")
    buckets = (
        draft.crumbs, draft.shapes, draft.fact_shapes,
        draft.stores, draft.lines, draft.seed_facts,
    )
    missing: list[str] = []
    for bucket in buckets:
        for comp in bucket:
            cite = comp.get("citation")
            if not cite or not cite.get("manifest_element_id"):
                missing.append(comp.get("extraction_id", "<no-id>"))
    if missing:
        rep.failures.append(f"missing citations on {len(missing)} components")
    return rep


def validate_weapon_target_id(sess: ForgeSession, _draft: WeaponDraft) -> ValidatorReport:
    rep = ValidatorReport(name="weapon_target_id")
    if not sess.weapon_target_id or not sess.weapon_target_id.strip():
        rep.failures.append("empty weapon_target_id")
    return rep


# Register the core validators on import (idempotent).
register_validator(validate_weapon_target_id)
register_validator(validate_manifest_coverage)
register_validator(validate_citation_integrity)


# ---------- weapon hashing ----------

def _hash_weapon_dir(weapon_dir: Path) -> str:
    """sha256 over canonicalized (sorted) tree of weapon dir contents."""
    h = hashlib.sha256()
    for p in sorted(weapon_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(weapon_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        with p.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


# ---------- seal entrypoint ----------

def _matrix_conflicts(alpha) -> list:
    """Flag a matrix where the same coordinate carries CONFLICTING cell
    values — a symptom of the agent conflating two source tables (e.g. two
    different 'CRITICAL' severity weights). Duplicate coords with the SAME
    value are fine (dedup); different values are a genuine contradiction that
    would otherwise be silently resolved first-wins by merge_starter."""
    import json
    fails: list = []
    seen: dict = {}
    for row in alpha.starter or []:
        if not isinstance(row, dict):
            continue
        coords = row.get("coords") or {}
        cell = row.get("cell") or {}
        key = json.dumps(coords, sort_keys=True)
        val = json.dumps(cell, sort_keys=True)
        if key in seen and seen[key] != val:
            fails.append(
                f"matrix {alpha.name!r} has conflicting cells at {coords}: "
                f"{seen[key]} vs {val} (agent likely merged two source "
                f"tables — split them or drop one)"
            )
        else:
            seen.setdefault(key, val)
    return fails


def _validate_pack(pack, *, force: bool = False) -> list:
    """Structural checks a forged pack must pass before seal."""
    fails: list = []
    if not pack.alphabets:
        fails.append("pack has no alphabets")
    names = {a.name for a in pack.alphabets}
    for b in pack.validator_bindings:
        if b.alphabet not in names:
            fails.append(
                f"validator binding field {b.field!r} -> unknown alphabet {b.alphabet!r}")
    from ara.forge.alphabets.pack import ShapeKind
    for a in pack.alphabets:
        if a.shape == ShapeKind.VECTOR:
            continue  # vector ships axes, not starter entries
        if not a.starter and not force:
            fails.append(f"alphabet {a.name!r} is empty (use force=True to allow)")
        if a.shape == ShapeKind.MATRIX:
            fails.extend(_matrix_conflicts(a))
    if pack.chunk_templates:
        entry_ct = sum(1 for c in pack.chunk_templates if c.get("is_entry"))
        if entry_ct != 1:
            fails.append(f"chunk graph needs exactly one entry node (got {entry_ct})")
        ids = {c.get("id") for c in pack.chunk_templates}
        for c in pack.chunk_templates:
            for tag, tgt in (c.get("exits") or {}).items():
                nxt = tgt.get("next_chunk_id") if isinstance(tgt, dict) else tgt
                if nxt and nxt not in ids:
                    fails.append(
                        f"chunk {c.get('id')!r} exit {tag} -> unknown chunk {nxt!r}")
    return fails


def seal_pack(sess: ForgeSession, *, force: bool = False) -> SealResult:
    """Seal a pack-forge session: validate the curated draft → write
    ``<state_root>/packs/<name>.json`` → SEALED. Failures return the
    session to REVIEW (no file written)."""
    from ara.forge.phases.synthesize import load_pack_draft
    from ara.forge.alphabets import pack as pack_mod
    draft = load_pack_draft(sess.state_root, sess.forge_id)
    if draft is None:
        return SealResult(ok=False, failures=["no pack_draft.json — run synthesize first"])
    try:
        pack = draft.to_pack()
    except Exception as e:  # noqa: BLE001
        return SealResult(ok=False, failures=[f"pack assembly failed: {e}"])
    fails = _validate_pack(pack, force=force)
    if fails:
        sess.transition(ForgePhase.REVIEW, reason="pack seal failed",
                        meta={"failures": fails})
        return SealResult(ok=False, failures=fails)
    out_path = pack_mod.runtime_packs_dir(sess.state_root) / f"{pack.name}.json"
    pack_mod.write_pack(out_path, pack)
    try:
        pack_mod.load_pack(out_path)  # round-trip validation
    except Exception as e:  # noqa: BLE001
        out_path.unlink(missing_ok=True)
        sess.transition(ForgePhase.REVIEW, reason="pack seal round-trip failed")
        return SealResult(ok=False, failures=[f"sealed pack does not reload: {e}"])
    sess.transition(ForgePhase.SEALED, reason="pack seal complete",
                    meta={"pack_name": pack.name})
    try:
        from ara.forge.cli import _unequip_forge_pair
        _unequip_forge_pair(sess)
    except Exception:  # noqa: BLE001
        pass
    return SealResult(ok=True, weapon_id=pack.name)


def seal(sess: ForgeSession, *, weapon_id: str | None = None,
          force: bool = False) -> SealResult:
    """Run seal validators and (if all pass) write the weapon directory.

    ``force=True`` downgrades the soft-blocking validators to warnings:

    * ``manifest_coverage`` — pending elements no longer block. The
      operator's choice to ship a partially-extracted weapon is recorded
      as a warning.
    * empty pack-declared alphabets — listed as warnings, not failures,
      so a sealed weapon can ship with placeholder alphabets the
      operator promises to fill in via reforge.

    Hard correctness validators (citation_integrity, weapon_target_id)
    still block under ``force=True``: an empty target id or unsourced
    component is a real defect, not a coverage gap.
    """
    if sess.phase != ForgePhase.SEAL:
        raise ValueError(
            f"seal requires SEAL phase (current: {sess.phase.value})"
        )
    if sess.config.kind == ForgeKind.PACK.value:
        return seal_pack(sess, force=force)
    draft = load_draft(sess.state_root, sess.forge_id)
    if draft is None:
        return SealResult(ok=False, failures=["no weapon_draft.json — run synthesize first"])

    failures: list[str] = []
    warnings: list[str] = []
    _SOFT_VALIDATORS = {"validate_manifest_coverage"}
    for fn in all_validators():
        try:
            rep = fn(sess, draft)
        except Exception as e:
            failures.append(f"{fn.__name__}: validator crashed: {e}")
            continue
        if force and fn.__name__ in _SOFT_VALIDATORS and rep.failures:
            warnings.extend(
                f"[forced past] {msg}" for msg in rep.failures
            )
        else:
            failures.extend(rep.failures)
        warnings.extend(rep.warnings)

    if failures:
        # Move forge back to REVIEW; audit the failure.
        for f in failures:
            raudit.append(
                fstate.forge_audit_path(sess.state_root, sess.forge_id),
                {"reason": f}, kind="seal.failure",
            )
        sess.transition(ForgePhase.REVIEW, reason="seal failed",
                        meta={"failures": failures, "warnings": warnings})
        return SealResult(ok=False, failures=failures, warnings=warnings)

    # All validators pass — write the weapon directory.
    wid = weapon_id or sess.weapon_target_id or uuid4().hex[:10]
    weapon_dir = fstate.weapon_dir(sess.state_root, wid)
    fstate.ensure_weapon_dirs(sess.state_root, wid)
    # Drop the draft as canonical weapon.json
    weapon_json = weapon_dir / "weapon.json"
    weapon_json.write_text(json.dumps(draft.to_jsonable(), sort_keys=True, indent=2))
    # ARA policy hot-swap — write the draft's bulwark/WALLS/rhema text
    # to <weapon>/policy/*.md so ``ara weapon equip`` can swap them
    # into state_root. Filenames are conventional (walls.md / bulwark.md
    # / rhema.md) so the equip handler can copy by known names.
    if draft.policy:
        policy_dir = weapon_dir / "policy"
        policy_dir.mkdir(parents=True, exist_ok=True)
        for kind, text in draft.policy.items():
            if not isinstance(text, str) or not text.strip():
                continue
            target_name = {
                "walls": "WALLS.md",
                "bulwark": "bulwark.md",
                "rhema": "rhema.md",
            }.get(kind, f"{kind}.md")
            (policy_dir / target_name).write_text(text)

    # 2.3 chunks — write each draft chunk into <weapon>/chunks/<id>.json
    # so ChunkRuntime + auto-firer can load them at engagement start.
    if draft.chunks:
        from ara.forge.chunks.chunkdef import ChunkDef, write_chunkdef
        chunks_dir = weapon_dir / "chunks"
        for chunk_jsonable in draft.chunks:
            try:
                chunk = ChunkDef.from_jsonable(chunk_jsonable)
            except (KeyError, TypeError, ValueError):
                continue
            write_chunkdef(chunks_dir / f"{chunk.id}.json", chunk)

    # 1.4 alphabets — re-project from extractions and write to disk.
    # Synthesize already validated + computed conflicts; we re-run the
    # cheap projection here so the on-disk alphabet files are produced
    # from the canonical extractions log, not the draft summary.
    pack_name = sess.config.alphabet_pack
    alphabet_failures = _seal_validate_and_write_alphabets(
        sess, draft, pack_name, weapon_dir,
    )
    if alphabet_failures:
        if force:
            warnings.extend(
                f"[forced past] {msg}" for msg in alphabet_failures
            )
        else:
            for f in alphabet_failures:
                raudit.append(
                    fstate.forge_audit_path(sess.state_root, sess.forge_id),
                    {"reason": f}, kind="seal.failure",
                )
            sess.transition(ForgePhase.REVIEW, reason="seal failed",
                            meta={"failures": alphabet_failures, "warnings": warnings})
            return SealResult(ok=False, failures=alphabet_failures, warnings=warnings)

    # 4.1 channel catalog — generated post-validators, written into the weapon dir
    seal_id = f"seal_{int(time.time() * 1000):x}_{wid[:6]}"
    catalog = generate_catalog(weapon_id=wid, seal_id=seal_id)
    isolated = detect_isolated(catalog)
    if isolated:
        # v1: warn (full hard-fail per 4.1 confirm Q3 needs more accurate
        # cross-channel call detection than naive string grep gives).
        warnings.append(f"isolated channels (no static edges): {isolated}")
    write_catalog_json(weapon_dir / "channel_catalog.json", catalog)
    write_catalog_md(weapon_dir / "channel_catalog.md", catalog)

    # 3.1 search index build — FTS5 over weapon components.
    fts = FTSIndex(weapon_dir / "search" / "fts.db")
    fts.add_many(index_weapon_components(draft.to_jsonable()))
    fts.close()

    # 3.1.1 calibration queries — operators can author them in the forge
    # dir before seal; we copy them into the weapon so
    # ``ara weapon calibrate`` works against the sealed weapon without
    # manual file shuffling.
    forge_cal = fstate.forge_dir(sess.state_root, sess.forge_id) / "calibration_queries.json"
    if forge_cal.is_file():
        shutil.copyfile(forge_cal, weapon_dir / "calibration_queries.json")

    weapon_hash = _hash_weapon_dir(weapon_dir)
    seal_doc = {
        "weapon_id": wid,
        "weapon_hash": weapon_hash,
        "weapon_target_id": sess.weapon_target_id,
        "weapon_predecessor_id": sess.weapon_predecessor_id,
        "forge_id": sess.forge_id,
        "operator_id": sess.operator_id,
        "sealed_at": time.time(),
        "validators_passed": [fn.__name__ for fn in all_validators()],
        "warnings": warnings,
    }
    seal_json = weapon_dir / "seal.json"
    tmp = seal_json.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(seal_doc, sort_keys=True, indent=2))
    os.replace(tmp, seal_json)

    seal_event = raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {"weapon_id": wid, "weapon_hash": weapon_hash, "warnings": warnings},
        kind="seal.complete",
    )
    sess.transition(ForgePhase.SEALED, reason="seal complete",
                    meta={"weapon_id": wid, "weapon_hash": weapon_hash})

    # Optionally archive forge dir into the weapon directory
    forge_audit_src = fstate.forge_audit_path(sess.state_root, sess.forge_id)
    if forge_audit_src.exists():
        shutil.copyfile(forge_audit_src, weapon_dir / "forge_audit.jsonl")

    return SealResult(
        ok=True, failures=[], warnings=warnings,
        weapon_id=wid, weapon_hash=weapon_hash,
        seal_audit_hash=seal_event.self_hash,
    )


def request_seal(sess: ForgeSession) -> None:
    """REVIEW → SEAL transition."""
    if sess.phase != ForgePhase.REVIEW:
        raise ValueError(
            f"request_seal requires REVIEW (current: {sess.phase.value})"
        )
    sess.transition(ForgePhase.SEAL, reason="operator requested seal")


def _seal_validate_and_write_alphabets(
    sess: ForgeSession,
    draft: WeaponDraft,
    pack_name: str,
    weapon_dir,
) -> list[str]:
    """Project alphabets from extractions, write them to the weapon dir,
    fail seal on declared-but-empty alphabets (Q6-A).

    Returns list of failure messages; empty = ok.
    """
    if not pack_name:
        return []
    try:
        pack = resolve_pack(pack_name, sess.state_root)
    except FileNotFoundError:
        return [f"alphabet pack not found: {pack_name!r}"]
    extractions = read_extractions(sess.state_root, sess.forge_id)
    conc_bodies = [
        e.body for e in extractions
        if e.channel == "extract.conc" and e.body.get("alphabet_name")
    ]
    try:
        derived = project_alphabets(conc_bodies, pack)
    except ValueError as e:
        return [f"alphabet projection failed: {e}"]
    if derived.has_conflicts():
        names = sorted({c.alphabet_name for c in derived.conflicts})
        return [f"alphabet conflicts in: {names}; resolve and re-run synthesize"]
    if sess.config.allow_starter_alphabets:
        merge_starter(derived)
    try:
        validate_foreign_keys(derived)
    except ValueError as e:
        return [f"alphabet foreign-key check failed: {e}"]
    empty = derived.empty_alphabets()
    if empty:
        return [
            f"alphabet {n!r} is empty (declared by pack {pack_name!r}); "
            f"add covering mats, drop the pack, or set "
            f"config.allow_starter_alphabets=True"
            for n in empty
        ]
    write_derived(weapon_dir, derived)
    return []
