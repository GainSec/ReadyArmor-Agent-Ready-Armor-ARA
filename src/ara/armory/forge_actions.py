from __future__ import annotations

import tempfile
from pathlib import Path

from ..forge.alphabets.auto_extractor import run_auto_extract
from ..forge.cli import _equip_blacksmith, _unequip_blacksmith
from ..forge.phases import deposit, exhaust, manifest, seal, synthesize
from ..forge.session import ForgeConfig, ForgeKind, ForgePhase, ForgeSession


def _boolish(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _float_body(body: dict[str, object], key: str, default: float) -> float:
    raw = _clean_text(body.get(key))
    if not raw:
        return default
    return float(raw)


def _int_body(body: dict[str, object], key: str, default: int) -> int:
    raw = _clean_text(body.get(key))
    if not raw:
        return default
    return int(raw)


def _load(state_root: Path, forge_id: str) -> ForgeSession:
    return ForgeSession.load(Path(state_root), forge_id)


def _result(fn) -> tuple[bool, str]:
    try:
        return True, fn()
    except Exception as exc:  # noqa: BLE001 - displayed as operator flash
        return False, str(exc)


def handle_forge_create(
    state_root: Path,
    body: dict[str, object],
) -> tuple[bool, str, str | None]:
    target = _clean_text(body.get("weapon_target_id"))
    if not target:
        return False, "weapon target required", None
    predecessor = _clean_text(body.get("weapon_predecessor_id")) or None
    engagement = _clean_text(body.get("engagement_id")) or None
    operator = _clean_text(body.get("operator_id")) or "armory"
    pack = _clean_text(body.get("alphabet_pack")) or "offsec"
    threshold_raw = _clean_text(body.get("coverage_threshold"))
    try:
        threshold = float(threshold_raw) if threshold_raw else 1.0
    except ValueError:
        return False, "coverage threshold must be a number", None
    if threshold < 0 or threshold > 1:
        return False, "coverage threshold must be between 0 and 1", None
    kind = _clean_text(body.get("kind")) or "weapon"
    if kind not in ("weapon", "pack"):
        return False, "kind must be 'weapon' or 'pack'", None
    from ara.forge import chunk_workflows
    workflow = _clean_text(body.get("chunk_workflow")) or chunk_workflows.DEFAULT
    if workflow not in chunk_workflows.workflow_names(state_root):
        workflow = chunk_workflows.DEFAULT
    config = ForgeConfig(
        coverage_threshold=threshold,
        alphabet_pack=pack,
        allow_starter_alphabets=_boolish(body.get("allow_starter_alphabets")),
        kind=kind,
        forge_policy=not _boolish(body.get("skip_policy")),
        chunk_workflow=workflow,
    )
    try:
        sess = ForgeSession.new(
            Path(state_root),
            weapon_target_id=target,
            weapon_predecessor_id=predecessor,
            operator_id=operator,
            config=config,
            engagement_id=engagement,
        )
        _equip_blacksmith(sess)
    except Exception as exc:  # noqa: BLE001 - displayed as operator flash
        return False, str(exc), None
    return True, f"Created forge {sess.forge_id} for {target}.", sess.forge_id


def handle_forge_pack_toggle(
    state_root: Path, forge_id: str, group: str, index: int, keep: bool,
) -> tuple[bool, str]:
    """Flip keep/drop on a pack-draft item (kind=pack review panel)."""
    from ara.forge.phases import synthesize
    draft = synthesize.load_pack_draft(Path(state_root), forge_id)
    if draft is None:
        return False, "no pack draft — run synthesize first"
    try:
        draft.toggle(group, int(index), bool(keep))
    except (IndexError, KeyError, ValueError):
        return False, "invalid draft item"
    synthesize.save_pack_draft(Path(state_root), forge_id, draft)
    return True, f"{group}[{index}] {'kept' if keep else 'dropped'}"


_PACK_GROUPS = ("alphabets", "entries", "bindings", "chunks", "policy", "detection")


def handle_forge_pack_curate(
    state_root: Path, forge_id: str, body: dict | None,
) -> tuple[bool, str]:
    """Batch curate the pack draft from a submitted form (kind=pack).

    Every item's keep-state is set from whether its ``keep_<group>_<index>``
    checkbox was submitted (absent = dropped). A per-group ``add_<group>``
    JSON field appends a custom item. If the Seal button was pressed
    (``seal`` present in body), chains into the seal handler."""
    import json as _json
    from ara.forge.phases import synthesize as _syn
    body = body or {}
    draft = _syn.load_pack_draft(Path(state_root), forge_id)
    if draft is None:
        return False, "no pack draft — run synthesize first"
    for group in _PACK_GROUPS:
        items = draft.items_for(group)
        for i in range(len(items)):
            items[i]["keep"] = f"keep_{group}_{i}" in body
        raw = _clean_text(body.get(f"add_{group}"))
        if raw:
            try:
                draft.add_item(group, _json.loads(raw))
            except (ValueError, TypeError):
                return False, f"add_{group}: invalid JSON"
    _syn.save_pack_draft(Path(state_root), forge_id, draft)
    if body.get("seal"):
        return handle_forge_seal(state_root, forge_id, {})
    return True, "Curation saved."


def handle_forge_pack_promote(state_root: Path, name: str) -> tuple[bool, str]:
    """Copy a runtime pack into the built-in repo dir (dev-time promote)."""
    import shutil
    from ..forge.alphabets import pack as pack_mod
    src = pack_mod.runtime_packs_dir(Path(state_root)) / f"{name}.json"
    if not src.exists():
        return False, f"no runtime pack {name!r} to promote"
    dst = pack_mod.builtin_pack_path(name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return True, f"Promoted {name} to built-in packs."


def handle_forge_upload_mat(
    state_root: Path,
    forge_id: str,
    body: dict[str, object],
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    filename = Path(_clean_text(body.get("file_filename"))).name
    blob = body.get("file")
    if not filename or not isinstance(blob, (bytes, bytearray)):
        return False, "file is required"
    if not blob:
        return False, "file is empty"

    def _upload() -> str:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(bytes(blob))
            tmp_path = Path(tmp.name)
        try:
            rec = deposit.deposit_mat(sess, tmp_path, rename_to=filename)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return f"Uploaded {rec.mat_filename} ({rec.size_bytes} bytes)."

    return _result(_upload)


def handle_forge_remove_mat(
    state_root: Path,
    forge_id: str,
    body: dict[str, object],
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    name = Path(_clean_text(body.get("mat_filename"))).name
    if not name:
        return False, "mat filename required"
    return _result(lambda: (deposit.remove_mat(sess, name) or f"Removed {name}."))


def handle_forge_finalize_deposit(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    return _result(lambda: (deposit.finalize_deposit(sess) or "Deposit finalized."))


def handle_forge_run_manifest(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)

    def _run() -> str:
        elements = manifest.run_manifest(sess)
        return f"Manifest generated with {len(elements)} elements."

    return _result(_run)


def handle_forge_approve_manifest(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    return _result(lambda: (manifest.approve_manifest(sess) or "Manifest approved."))


def handle_forge_drop_element(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    element_id = _clean_text((body or {}).get("element_id"))
    if not element_id:
        return False, "element_id required"
    return _result(lambda: (manifest.drop_element(sess, element_id) or f"Element {element_id} dropped."))


def handle_forge_undrop_element(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    element_id = _clean_text((body or {}).get("element_id"))
    if not element_id:
        return False, "element_id required"
    return _result(lambda: (manifest.undrop_element(sess, element_id) or f"Element {element_id} reinstated."))


def handle_forge_auto_extract(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    if sess.phase == ForgePhase.REVIEW:
        ok, msg = handle_forge_approve_manifest(state_root, forge_id)
        if not ok:
            return ok, msg
        sess = _load(state_root, forge_id)

    def _run() -> str:
        report = run_auto_extract(
            sess,
            skip_existing=not _boolish((body or {}).get("no_skip_existing")),
        )
        return (
            f"Auto-extract recorded {report.extractions_recorded} bodies "
            f"from {report.elements_scanned} report elements "
            f"({report.skipped_existing} skipped)."
        )

    return _result(_run)


def handle_forge_run_exhaust(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    data = body or {}
    # Stateless per-element extraction is the default: one direct model call
    # per pending element (constant context, grounded by construction, no
    # agentic-loop timeout). Pass engine="agentic" for the legacy contained-
    # hermes loop.
    engine = str(data.get("engine") or "stateless").strip().lower()

    def _run() -> str:
        from ..forge import loop_driver
        from ..forge.loop_driver import run_exhaust, run_exhaust_stateless
        # Pack forges produce a vocabulary via the CONTAINED agent (codex+pen)
        # calling the propose.* channels — the only generation path. There is
        # no stateless fallback: if the agent can't run, this surfaces the
        # error (via _result) rather than producing an uncontained pack.
        sess = _load(state_root, forge_id)
        if sess.config.kind == ForgeKind.PACK.value:
            res = loop_driver.run_pack_exhaust(
                Path(state_root),
                forge_id,
                batch_size=max(1, _int_body(data, "batch_size", 8)),
                max_iterations=max(1, _int_body(data, "max_iterations", 20)),
                target_coverage=_float_body(data, "target_coverage", 0.9),
                iteration_timeout_s=max(
                    5.0, _float_body(data, "iteration_timeout_s", 600.0)),
            )
            return (
                f"Pack exhaust ran {len(res.iterations)} iteration(s); "
                f"coverage {res.final_coverage:.0%}; "
                f"stopped: {res.stopped_reason}."
            )
        if engine == "agentic":
            result = run_exhaust(
                Path(state_root),
                forge_id,
                target_coverage=_float_body(data, "target_coverage", 0.5),
                batch_size=max(1, _int_body(data, "batch_size", 5)),
                max_iterations=max(1, _int_body(data, "max_iterations", 1)),
                min_extractions_per_iter=max(
                    0, _int_body(data, "min_extractions_per_iter", 1),
                ),
                iteration_timeout_s=max(
                    5.0, _float_body(data, "iteration_timeout_s", 120.0),
                ),
                no_progress_bail=max(1, _int_body(data, "no_progress_bail", 1)),
            )
            return (
                f"Hermes exhaust ran {len(result.iterations)} iteration(s); "
                f"coverage {result.final_coverage:.0%}; "
                f"stopped: {result.stopped_reason}."
            )
        sres = run_exhaust_stateless(
            Path(state_root),
            forge_id,
            max_elements=max(1, _int_body(data, "max_elements", 40)),
            timeout_s=max(5.0, _float_body(data, "timeout_s", 90.0)),
        )
        return (
            f"Stateless exhaust examined {sres.elements_examined} element(s); "
            f"+{sres.extractions_added} extraction(s); "
            f"coverage {sres.final_coverage:.0%}."
        )

    return _result(_run)


def handle_forge_pack_fallback(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    """Phase 2 (pack): run the stateless heuristic pass into a SEPARATE
    candidate pool, then have the contained agent review it (keep/skip) —
    only run AFTER the agent's own forge. Kept candidates become real
    extractions; the rest are dropped."""
    data = body or {}
    sess = _load(state_root, forge_id)
    if sess.config.kind != ForgeKind.PACK.value:
        return False, "stateless fallback is pack-forge only"

    def _run() -> str:
        from ..forge import loop_driver
        loop_driver.run_pack_exhaust_stateless(
            Path(state_root), forge_id, sink="candidates",
            max_elements=max(1, _int_body(data, "max_elements", 60)),
            timeout_s=max(5.0, _float_body(data, "timeout_s", 90.0)),
        )
        res = loop_driver.run_pack_review(
            Path(state_root), forge_id,
            iteration_timeout_s=max(5.0, _float_body(data, "review_timeout_s", 600.0)),
        )
        return (
            f"Stateless fallback: {res['candidates']} candidate(s) reviewed by "
            f"the agent, {res['kept']} kept."
        )

    return _result(_run)


def handle_forge_advance_review(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    force = _boolish((body or {}).get("force"))

    def _run() -> str:
        cov = exhaust.advance_to_review(sess, operator_force=force)
        pct = f"{cov.fraction:.0%}"
        return f"Exhaust advanced to review ({pct} coverage)."

    return _result(_run)


def handle_forge_reconcile(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    """RECONCILE step (kind=pack): run the agent-driven reduce-step
    reconciliation over the synthesized draft — resolve contradictory matrix
    cells, merge duplicate alphabets, split incompatible sources. Requires a
    synthesized pack_draft.json (run synthesize first)."""
    from ..forge import loop_driver

    def _run() -> str:
        res = loop_driver.run_reconcile(
            Path(state_root), forge_id,
            iteration_timeout_s=max(
                5.0, _float_body(body or {}, "iteration_timeout_s", 300.0)),
        )
        return (
            f"Reconcile: handled {res['groups']} conflict-group(s), applied "
            f"{res['decisions']} decision(s), dropped {res['unresolved_dropped']} "
            f"unresolved."
        )

    return _result(_run)


def handle_forge_workflow_derive(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    """Derive an investigation chunk workflow from the forge's mats (contained
    agent, WORKFLOW mode) and save it to the registry. Body: {name, domain?}.
    The derived workflow is then selectable in future pack forges."""
    from ..forge import loop_driver
    data = body or {}
    name = _clean_text(data.get("name")) or _clean_text(data.get("domain"))
    if not name:
        return False, "workflow name (or domain) required"
    domain = _clean_text(data.get("domain")) or name

    def _run() -> str:
        res = loop_driver.run_workflow_derive(
            Path(state_root), forge_id, name=name, domain=domain,
            description=_clean_text(data.get("description")) or None)
        if res.get("saved"):
            return (f"Derived workflow {name!r}: {res['nodes']} phases, "
                    f"saved to the registry.")
        return (f"Workflow derivation did not save — {res.get('nodes', 0)} "
                f"phases, failures: {'; '.join(res.get('failures') or [])}")

    return _result(_run)


def handle_forge_synthesize(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    force = _boolish((body or {}).get("force"))

    def _run() -> str:
        if sess.phase == ForgePhase.EXHAUST:
            exhaust.advance_to_review(sess, operator_force=force)
        if sess.phase == ForgePhase.REVIEW:
            sess.transition(ForgePhase.SYNTHESIZE, reason="armory synthesize")
        if sess.config.kind == ForgeKind.PACK.value:
            pdraft = synthesize.synthesize_pack(sess)
            if sess.phase == ForgePhase.SYNTHESIZE:
                sess.transition(ForgePhase.REVIEW, reason="armory synth done")
            counts = ", ".join(
                f"{len(pdraft.items_for(g))} {g}"
                for g in ("alphabets", "entries", "bindings", "chunks",
                          "policy", "detection"))
            # RECONCILE is part of the standard pack flow (the reduce step):
            # the agent resolves cross-mat conflicts before the draft is
            # curated/sealed. Skips instantly (zero agent turns) when there
            # are no conflicts. Operators can opt out with skip_reconcile.
            recon_msg = ""
            if not _boolish((body or {}).get("skip_reconcile")):
                from ..forge import loop_driver
                res = loop_driver.run_reconcile(Path(state_root), forge_id)
                if res.get("groups"):
                    recon_msg = (
                        f" Reconciled {res['groups']} conflict-group(s): "
                        f"{res['decisions']} decision(s), "
                        f"{res['unresolved_dropped']} dropped.")
            return f"Pack draft synthesized: {counts}.{recon_msg}"
        draft = synthesize.synthesize(sess)
        if sess.phase == ForgePhase.SYNTHESIZE:
            sess.transition(ForgePhase.REVIEW, reason="armory synth done")
        return (
            "Draft synthesized: "
            f"{len(draft.crumbs)} crumbs, {len(draft.shapes)} shapes, "
            f"{len(draft.alphabets)} alphabets."
        )

    return _result(_run)


def handle_forge_seal(
    state_root: Path,
    forge_id: str,
    body: dict[str, object],
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    weapon_id = _clean_text(body.get("weapon_id")) or None
    force = _boolish(body.get("force"))
    if force:
        return False, (
            "Armory force seal is disabled. Complete manifest coverage "
            "before sealing, or use the CLI break-glass path explicitly."
        )

    def _run() -> str:
        if sess.phase == ForgePhase.REVIEW:
            seal.request_seal(sess)
        result = seal.seal(sess, weapon_id=weapon_id, force=force)
        if result.ok:
            temper_msg = _create_temper_from_sealed_weapon(
                Path(state_root), sess, result.weapon_id,
            )
            _unequip_blacksmith(sess)
            suffix = f" {temper_msg}" if temper_msg else ""
            return f"Sealed {result.weapon_id}.{suffix}"
        failures = "; ".join(result.failures) or "seal failed"
        return f"Seal blocked: {failures}"

    ok, msg = _result(_run)
    if msg.startswith("Seal blocked:"):
        return False, msg
    return ok, msg


def _create_temper_from_sealed_weapon(
    state_root: Path,
    sess: ForgeSession,
    weapon_id: str | None,
) -> str:
    target_armor = sess.prior_armor
    if not target_armor or not weapon_id:
        return ""
    try:
        from ara import temper as temper_mod
        proposal = temper_mod.create_proposal_from_weapon(
            state_root,
            target_armor,
            source_weapon=weapon_id,
            source_forge=sess.forge_id,
            operator_notes=(
                "Review forge-generated methodology, scope lines, and "
                "task-fit material before promoting into armor or trim."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as operator flash text
        return f"Temper proposal skipped: {exc}"
    return f"Created Temper proposal {proposal.proposal_id} for {target_armor}."


def handle_forge_abort(
    state_root: Path,
    forge_id: str,
    body: dict[str, object] | None = None,
) -> tuple[bool, str]:
    sess = _load(state_root, forge_id)
    reason = _clean_text((body or {}).get("reason")) or "aborted via armory"

    def _run() -> str:
        sess.abort(reason=reason)
        _unequip_blacksmith(sess)
        return "Forge aborted and prior loadout restored."

    return _result(_run)
