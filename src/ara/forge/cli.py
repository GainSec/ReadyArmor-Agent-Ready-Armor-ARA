"""``ara forge <subcommand>`` handlers.

Subcommands (per orchestrator skeleton §11)::

    ara forge new <weapon_target_id> [--state-root]
    ara forge deposit <forge_id> --mat <path> [--source-url ...]
    ara forge mats   <forge_id>
    ara forge finalize-deposit <forge_id>
    ara forge manifest <forge_id>           # run manifest enumeration
    ara forge approve-manifest <forge_id>   # REVIEW → EXHAUST
    ara forge coverage <forge_id>
    ara forge synthesize <forge_id>
    ara forge seal <forge_id> [--weapon-id]
    ara forge status <forge_id>
    ara forge abort <forge_id>
"""
from __future__ import annotations

import argparse
import sys
import json
import os
from dataclasses import asdict
from pathlib import Path

from ara.forge import state as fstate
from ara.forge.phases import deposit, exhaust, manifest, seal, synthesize
from ara.forge.session import ForgePhase, ForgeSession, ForgeKind


def _default_state_root() -> Path:
    env = os.environ.get("ARA_STATE_ROOT")
    return Path(env) if env else Path(".ara")


def _operator_id() -> str:
    return os.environ.get("ARA_OPERATOR_ID") or os.environ.get("USER") or "unknown"


def install_forge_subcommand(sub: argparse._SubParsersAction) -> None:
    forge = sub.add_parser("forge", help="weapon forge: deposit → manifest → exhaust → synthesize → seal")
    forge.add_argument("--state-root", type=Path, default=None,
                       help="ARA state dir (defaults to $ARA_STATE_ROOT or ./.ara/)")
    fsub = forge.add_subparsers(dest="forge_cmd", required=True)

    p = fsub.add_parser("new", help="create a new forge session")
    p.add_argument("weapon_target_id")
    p.add_argument("--kind", choices=["weapon", "pack"], default="weapon",
                   help="weapon (default) forges a weapon; pack forges a new "
                        "alphabet pack from mats (equips codex+pen)")
    p.add_argument("--predecessor", default=None, help="prior weapon id (for reforge)")
    p.add_argument(
        "--engagement", default=None, dest="engagement_id",
        help="link this forge to an engagement_id; deposited mats are "
             "hard-linked into <state>/engagements/<eid>/mats/source/forge-<fid>/ "
             "so the trim picks them up automatically",
    )
    p.set_defaults(_handler=_cmd_new)

    p = fsub.add_parser("deposit", help="deposit a mat file")
    p.add_argument("forge_id")
    p.add_argument("--mat", type=Path, required=True)
    p.add_argument("--source-url", default=None)
    p.add_argument("--rename-to", default=None)
    p.set_defaults(_handler=_cmd_deposit)

    p = fsub.add_parser("mats", help="list deposited mats")
    p.add_argument("forge_id")
    p.set_defaults(_handler=_cmd_mats)

    p = fsub.add_parser("finalize-deposit", help="DEPOSIT → MANIFEST")
    p.add_argument("forge_id")
    p.set_defaults(_handler=_cmd_finalize_deposit)

    p = fsub.add_parser("manifest", help="run manifest enumeration")
    p.add_argument("forge_id")
    p.set_defaults(_handler=_cmd_manifest)

    p = fsub.add_parser("approve-manifest", help="REVIEW → EXHAUST")
    p.add_argument("forge_id")
    p.set_defaults(_handler=_cmd_approve_manifest)

    p = fsub.add_parser("coverage", help="show extraction coverage")
    p.add_argument("forge_id")
    p.set_defaults(_handler=_cmd_coverage)

    p = fsub.add_parser("auto-extract",
                        help="run pack-specific auto-extractor on REPORT_SECTION mats")
    p.add_argument("forge_id")
    p.add_argument("--no-skip-existing", action="store_true",
                   help="re-record alphabet entries even if already extracted")
    p.set_defaults(_handler=_cmd_auto_extract)

    p = fsub.add_parser("synthesize", help="compose weapon_draft.json from extractions")
    p.add_argument("forge_id")
    p.add_argument("--force", action="store_true",
                   help="advance EXHAUST → REVIEW even if coverage < threshold")
    p.set_defaults(_handler=_cmd_synthesize)

    p = fsub.add_parser("seal", help="run seal validators + write weapon")
    p.add_argument("forge_id")
    p.add_argument("--weapon-id", default=None)
    p.add_argument(
        "--force", action="store_true",
        help="seal even when manifest coverage is below threshold or "
             "pack-declared alphabets are empty. Hard correctness "
             "validators (target_id, citation_integrity) still block. "
             "Forced bypasses are recorded in seal warnings.",
    )
    p.set_defaults(_handler=_cmd_seal)

    p = fsub.add_parser("status", help="show forge session status")
    p.add_argument("forge_id")
    p.set_defaults(_handler=_cmd_status)

    p = fsub.add_parser(
        "run-exhaust",
        help=(
            "iteratively drive an agent (default: hermes one-shot under "
            "ara launch --contain) through the manifest until coverage "
            "threshold or max iterations. Each iteration targets a fresh "
            "batch of unprocessed manifest elements."
        ),
    )
    p.add_argument("forge_id")
    p.add_argument("--target-coverage", type=float, default=0.50,
                   help="stop when coverage fraction >= this (default: 0.50)")
    p.add_argument("--batch-size", type=int, default=15,
                   help="manifest elements per agent invocation (default: 15)")
    p.add_argument("--max-iterations", type=int, default=50,
                   help="hard cap on agent invocations (default: 50)")
    p.add_argument("--min-extractions", type=int, default=3,
                   help="minimum extractions to request per iteration "
                        "(default: 3) — keeps short responses honest")
    p.add_argument("--iteration-timeout", type=float, default=600.0,
                   help="seconds per agent invocation (default: 600)")
    p.add_argument("--no-progress-bail", type=int, default=3,
                   help="bail after N consecutive zero-progress "
                        "iterations (default: 3 — set higher when "
                        "targeting near-100%% coverage where the "
                        "long tail naturally has occasional misses)")
    p.add_argument("--engine", choices=["stateless", "agentic"],
                   default="stateless",
                   help="stateless (default): one direct model call per "
                        "pending element — constant context, grounded, no "
                        "loop timeout. agentic: legacy contained-hermes loop "
                        "(uses --batch-size/--max-iterations/etc.)")
    p.add_argument("--max-elements", type=int, default=None,
                   help="stateless engine: cap elements processed this run "
                        "(default: all pending)")
    p.set_defaults(_handler=_cmd_run_exhaust)

    p = fsub.add_parser("abort", help="abort an in-flight forge")
    p.add_argument("forge_id")
    p.add_argument("--reason", default="operator aborted")
    p.set_defaults(_handler=_cmd_abort)

    # Reforge: typed delta against a predecessor weapon
    rf = fsub.add_parser(
        "reforge",
        help="manage reforge proposals (delta vs. predecessor weapon)",
    )
    rfsub = rf.add_subparsers(dest="reforge_cmd", required=True)
    rp = rfsub.add_parser("propose",
        help="build proposal.json from current draft vs. predecessor")
    rp.add_argument("forge_id")
    rp.add_argument("--triggered-by", default="operator",
                    choices=["operator", "telemetry-recommendation"])
    rp.set_defaults(_handler=_cmd_reforge_propose)

    ra = rfsub.add_parser("approve",
        help="mark proposal approved → REVIEW (then run forge seal)")
    ra.add_argument("forge_id")
    ra.set_defaults(_handler=_cmd_reforge_approve)

    rs = rfsub.add_parser("show",
        help="print proposal summary")
    rs.add_argument("forge_id")
    rs.set_defaults(_handler=_cmd_reforge_show)
    rf.set_defaults(_handler=lambda _args: rf.print_help() or 0)

    # ---- pack-forge draft curation (kind=pack) ----
    pk = fsub.add_parser("pack", help="pack-forge: curate + promote a forged pack")
    pksub = pk.add_subparsers(dest="pack_cmd", required=True)
    pr = pksub.add_parser("review", help="print the pack draft (keep/drop per item)")
    pr.add_argument("forge_id"); pr.set_defaults(_handler=_cmd_pack_review)
    pt = pksub.add_parser("toggle", help="keep/drop a draft item")
    pt.add_argument("forge_id"); pt.add_argument("group"); pt.add_argument("index", type=int)
    pt.add_argument("state", choices=["on", "off"]); pt.set_defaults(_handler=_cmd_pack_toggle)
    pa = pksub.add_parser("add", help="add a custom item (json) to a group")
    pa.add_argument("forge_id"); pa.add_argument("group"); pa.add_argument("json")
    pa.set_defaults(_handler=_cmd_pack_add)
    pe = pksub.add_parser("edit", help="replace a draft item's data (json)")
    pe.add_argument("forge_id"); pe.add_argument("group"); pe.add_argument("index", type=int)
    pe.add_argument("json"); pe.set_defaults(_handler=_cmd_pack_edit)
    pp = pksub.add_parser("promote", help="copy a runtime pack into the built-in repo dir")
    pp.add_argument("name"); pp.set_defaults(_handler=_cmd_pack_promote)
    pl = pksub.add_parser("list", help="list packs (built-in + runtime store)")
    pl.set_defaults(_handler=_cmd_pack_list)
    pk.set_defaults(_handler=lambda _args: pk.print_help() or 0)

    forge.set_defaults(_handler=lambda _args: forge.print_help() or 0)


def run(args: argparse.Namespace) -> int:
    handler = getattr(args, "_handler", None)
    if handler is None:
        return 1
    return handler(args)


# ---------- handlers ----------

def _state(args: argparse.Namespace) -> Path:
    return Path(args.state_root) if args.state_root else _default_state_root()


def _load(args: argparse.Namespace) -> ForgeSession:
    return ForgeSession.load(_state(args), args.forge_id)


# ---------- blacksmith auto-equip lifecycle ----------
#
# Per docs/specs/blacksmith-hammer-v0.md §7:
# - `ara forge new` auto-equips armor `blacksmith` + weapon
#   `blacksmith-hammer`. Operator's prior equipped state is captured
#   on the ForgeSession so seal/abort can restore.
# - `ara forge seal` (on success) and `ara forge abort` auto-unequip
#   and restore prior.
# - The supervisor's `_watch_bulwark` hot-reloads — no openclaw
#   respawn.

# The (armor, weapon) pair auto-equipped per forge kind.
_FORGE_PAIRS = {
    "weapon": ("blacksmith", "blacksmith-hammer"),
    "pack": ("codex", "pen"),
}


def _equip_forge_pair(sess: ForgeSession) -> dict:
    """Materialize the seed if missing, equip the kind's (armor, weapon)
    pair. Idempotent. Returns a small report dict for the CLI handler.
    weapon-forge → blacksmith + blacksmith-hammer; pack-forge → codex + pen."""
    from ara import armors as armors_mod
    from ara import weapons as weapons_mod
    from ara.seed import ensure_seed
    armor, weapon = _FORGE_PAIRS.get(sess.config.kind, _FORGE_PAIRS["weapon"])
    state = sess.state_root
    seed_report = ensure_seed(state, armors=(armor,), weapons=(weapon,))
    armors_mod.equip_armor(state, armor)
    weapons_mod.set_equipped_weapon(state, weapon)
    equip_summary = weapons_mod.equip_policy(state, weapon)
    return {
        "armor": armor,
        "weapon": weapon,
        "policy_overlaid": equip_summary.get("overlaid", []),
        "policy_kept": equip_summary.get("kept", []),
        "seeded_armors": seed_report.copied_armors,
        "seeded_weapons": seed_report.copied_weapons,
        "bulwark_section_applied": armor in seed_report.bulwark_sections_applied,
    }


# Back-compat aliases (armory.forge_actions imports these names).
_equip_blacksmith = _equip_forge_pair


def _unequip_blacksmith(sess: ForgeSession) -> dict:
    """Strip the weapon overlay, then restore the operator's prior
    armor + weapon (or unequip both if there was nothing prior)."""
    from ara import armors as armors_mod
    from ara import weapons as weapons_mod
    state = sess.state_root
    # Strip the blacksmith-hammer overlay from <state>/bulwark.md and
    # the legacy WALLS.md/rhema.md files.
    weapons_mod.unequip_policy(state)
    # Restore prior weapon (None → just clear the equipped pointer).
    weapons_mod.set_equipped_weapon(state, sess.prior_weapon)
    if sess.prior_weapon:
        try:
            weapons_mod.equip_policy(state, sess.prior_weapon)
        except Exception:  # noqa: BLE001 — prior weapon may have been removed
            pass
    # Restore prior armor (None → set_active_armor(None) clears).
    if sess.prior_armor:
        armors_mod.equip_armor(state, sess.prior_armor)
    else:
        armors_mod.set_active_armor(state, None)
    return {
        "restored_armor": sess.prior_armor,
        "restored_weapon": sess.prior_weapon,
    }


# Kind-agnostic (strips overlay + restores prior); alias for symmetry.
_unequip_forge_pair = _unequip_blacksmith


def _cmd_new(args: argparse.Namespace) -> int:
    from ara.forge.session import ForgeConfig
    sess = ForgeSession.new(
        _state(args),
        weapon_target_id=args.weapon_target_id,
        weapon_predecessor_id=args.predecessor,
        operator_id=_operator_id(),
        engagement_id=getattr(args, "engagement_id", None),
        config=ForgeConfig(kind=getattr(args, "kind", "weapon")),
    )
    equip = _equip_forge_pair(sess)
    print(json.dumps({
        "forge_id": sess.forge_id,
        "phase": sess.phase.value,
        "weapon_target_id": sess.weapon_target_id,
        "state_root": str(sess.state_root),
        "prior_armor": sess.prior_armor,
        "prior_weapon": sess.prior_weapon,
        "engagement_id": sess.engagement_id,
        "equipped": equip,
    }, indent=2))
    return 0


def _cmd_deposit(args: argparse.Namespace) -> int:
    sess = _load(args)
    rec = deposit.deposit_mat(
        sess, args.mat, rename_to=args.rename_to, source_url=args.source_url,
    )
    print(json.dumps({
        "mat_filename": rec.mat_filename,
        "sha256": rec.sha256,
        "size_bytes": rec.size_bytes,
    }, indent=2))
    return 0


def _cmd_mats(args: argparse.Namespace) -> int:
    sess = _load(args)
    out = [
        {
            "mat_filename": r.mat_filename, "sha256": r.sha256,
            "size_bytes": r.size_bytes, "source_url": r.source_url,
        }
        for r in deposit.list_mats(sess)
    ]
    print(json.dumps(out, indent=2))
    return 0


def _cmd_finalize_deposit(args: argparse.Namespace) -> int:
    sess = _load(args)
    deposit.finalize_deposit(sess)
    print(json.dumps({"phase": sess.phase.value}))
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    sess = _load(args)
    # run_manifest auto-advances MANIFEST → REVIEW.
    elements = manifest.run_manifest(sess)
    print(json.dumps({
        "elements": len(elements),
        "by_kind": _bucket(elements, lambda e: e.kind.value),
        "phase": sess.phase.value,
    }, indent=2))
    return 0


def _cmd_approve_manifest(args: argparse.Namespace) -> int:
    sess = _load(args)
    manifest.approve_manifest(sess)
    print(json.dumps({"phase": sess.phase.value}))
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    sess = _load(args)
    cov = exhaust.coverage_state(sess)
    print(json.dumps({
        "fraction": cov.fraction,
        "elements_with_extractions": cov.elements_with_extractions,
        "total_elements": cov.total_elements,
        "pending_count": len(cov.pending),
    }, indent=2))
    return 0


def _cmd_run_exhaust(args: argparse.Namespace) -> int:
    from ara.forge.loop_driver import run_exhaust, run_exhaust_stateless
    sess = _load(args)
    if sess.phase != ForgePhase.EXHAUST:
        print(
            f"forge {args.forge_id} is in phase {sess.phase.value}, not "
            f"EXHAUST — advance to EXHAUST first (manifest → approve → "
            f"exhaust)",
            file=sys.stderr,
        )
        return 2
    state_root = _state(args)
    if getattr(args, "engine", "stateless") == "stateless":
        sres = run_exhaust_stateless(
            state_root, args.forge_id, max_elements=args.max_elements)
        print(json.dumps({
            "forge_id": sres.forge_id,
            "engine": "stateless",
            "elements_examined": sres.elements_examined,
            "elements_total": sres.elements_total,
            "extractions_added": sres.extractions_added,
            "final_coverage": round(sres.final_coverage, 4),
            "stopped_reason": sres.stopped_reason,
        }, indent=2))
        return 0
    result = run_exhaust(
        state_root,
        args.forge_id,
        target_coverage=args.target_coverage,
        batch_size=args.batch_size,
        max_iterations=args.max_iterations,
        min_extractions_per_iter=args.min_extractions,
        iteration_timeout_s=args.iteration_timeout,
        no_progress_bail=args.no_progress_bail,
    )
    summary = {
        "forge_id": result.forge_id,
        "stopped_reason": result.stopped_reason,
        "target_coverage": result.target_coverage,
        "final_coverage": result.final_coverage,
        "final_extractions": result.final_extractions,
        "iterations_run": len(result.iterations),
        "iterations": [
            {
                "i": it.iteration,
                "added": it.extractions_added,
                "cov_after": round(it.coverage_after, 4),
                "rc": it.rc,
                "duration_s": round(it.duration_s, 1),
                "error": it.error,
            }
            for it in result.iterations
        ],
    }
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_auto_extract(args: argparse.Namespace) -> int:
    from ara.forge.alphabets.auto_extractor import run_auto_extract
    sess = _load(args)
    if sess.phase != ForgePhase.EXHAUST:
        # Helper: auto-advance manifest→review→exhaust if operator forgot
        if sess.phase == ForgePhase.REVIEW:
            from ara.forge.phases import manifest as _manifest
            _manifest.approve_manifest(sess)
    report = run_auto_extract(
        sess,
        skip_existing=not args.no_skip_existing,
    )
    print(json.dumps({
        "pack_name": report.pack_name,
        "elements_scanned": report.elements_scanned,
        "bodies_emitted": report.bodies_emitted,
        "extractions_recorded": report.extractions_recorded,
        "skipped_existing": report.skipped_existing,
        "by_alphabet": report.by_alphabet,
        "phase": sess.phase.value,
    }, indent=2))
    return 0


def _cmd_synthesize(args: argparse.Namespace) -> int:
    sess = _load(args)
    if sess.phase == ForgePhase.EXHAUST:
        try:
            exhaust.advance_to_review(sess, operator_force=args.force)
        except exhaust.CoverageError as e:
            print(json.dumps({"error": str(e)}))
            return 2
    if sess.phase == ForgePhase.REVIEW:
        sess.transition(ForgePhase.SYNTHESIZE, reason="cli synthesize")
    if sess.config.kind == ForgeKind.PACK.value:
        pdraft = synthesize.synthesize_pack(sess)
        if sess.phase == ForgePhase.SYNTHESIZE:
            sess.transition(ForgePhase.REVIEW, reason="synth done")
        print(json.dumps({"phase": sess.phase.value, "kind": "pack",
                          "pack_name": pdraft.name,
                          "counts": {g: len(pdraft.items_for(g))
                                     for g in ("alphabets", "entries", "bindings",
                                               "chunks", "policy", "detection")}}))
        return 0
    draft = synthesize.synthesize(sess)
    if sess.phase == ForgePhase.SYNTHESIZE:
        sess.transition(ForgePhase.REVIEW, reason="synth done")
    print(json.dumps({
        "phase": sess.phase.value,
        "crumbs": len(draft.crumbs),
        "shapes": len(draft.shapes),
        "stores": len(draft.stores),
        "lines": len(draft.lines),
        "seed_facts": len(draft.seed_facts),
        "rhema_rules": len(draft.rhema_rules),
        "bulwark_rules": len(draft.bulwark_rules),
        "walls_paths": len(draft.walls_paths),
    }, indent=2))
    return 0


def _cmd_seal(args: argparse.Namespace) -> int:
    sess = _load(args)
    if sess.phase == ForgePhase.REVIEW:
        seal.request_seal(sess)
    result = seal.seal(sess, weapon_id=args.weapon_id, force=args.force)
    # Auto-unequip on a successful seal. On failure we leave blacksmith
    # equipped so the operator can fix and re-seal without losing the
    # forge-locked posture.
    unequip_summary = None
    if result.ok:
        unequip_summary = _unequip_blacksmith(sess)
    print(json.dumps({
        "ok": result.ok,
        "weapon_id": result.weapon_id,
        "weapon_hash": result.weapon_hash,
        "failures": result.failures,
        "warnings": result.warnings,
        "unequipped": unequip_summary,
    }, indent=2))
    return 0 if result.ok else 3


def _cmd_status(args: argparse.Namespace) -> int:
    sess = _load(args)
    cov = None
    if sess.phase in (ForgePhase.EXHAUST, ForgePhase.REVIEW, ForgePhase.SYNTHESIZE,
                      ForgePhase.SEAL, ForgePhase.SEALED):
        try:
            cov = exhaust.coverage_state(sess)
        except Exception:
            cov = None
    out: dict = sess.to_dict()
    if cov is not None:
        out["coverage_fraction"] = cov.fraction
    out["forge_dir"] = str(fstate.forge_dir(sess.state_root, sess.forge_id))
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_abort(args: argparse.Namespace) -> int:
    sess = _load(args)
    sess.abort(reason=args.reason)
    unequip_summary = _unequip_blacksmith(sess)
    print(json.dumps({
        "phase": sess.phase.value,
        "unequipped": unequip_summary,
    }))
    return 0


def _cmd_reforge_propose(args: argparse.Namespace) -> int:
    from ara.forge.reforge import driver as reforge_driver
    from ara.forge.reforge.proposal import summarize
    sess = _load(args)
    try:
        proposal = reforge_driver.propose(sess, triggered_by=args.triggered_by)
    except reforge_driver.ReforgeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(summarize(proposal), indent=2))
    return 0


def _cmd_reforge_approve(args: argparse.Namespace) -> int:
    from ara.forge.reforge import driver as reforge_driver
    from ara.forge.reforge.proposal import summarize
    sess = _load(args)
    try:
        proposal = reforge_driver.approve(sess, approved_by=_operator_id())
    except reforge_driver.ReforgeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(summarize(proposal), indent=2))
    return 0


def _cmd_reforge_show(args: argparse.Namespace) -> int:
    from ara.forge.reforge import driver as reforge_driver
    from ara.forge.reforge.proposal import summarize
    sess = _load(args)
    proposal = reforge_driver.get(sess)
    if proposal is None:
        print(json.dumps({"error": "no proposal recorded yet"}))
        return 2
    print(json.dumps(summarize(proposal), indent=2))
    return 0


def _cmd_pack_review(args: argparse.Namespace) -> int:
    sess = _load(args)
    draft = synthesize.load_pack_draft(sess.state_root, sess.forge_id)
    if draft is None:
        print(json.dumps({"error": "no pack draft; run `forge synthesize` first"}))
        return 2
    print("\n".join(draft.review_lines()))
    return 0


def _cmd_pack_toggle(args: argparse.Namespace) -> int:
    sess = _load(args)
    draft = synthesize.load_pack_draft(sess.state_root, sess.forge_id)
    if draft is None:
        print(json.dumps({"error": "no pack draft"}))
        return 2
    draft.toggle(args.group, args.index, args.state == "on")
    synthesize.save_pack_draft(sess.state_root, sess.forge_id, draft)
    print(json.dumps({"ok": True, "group": args.group, "index": args.index,
                      "keep": args.state == "on"}))
    return 0


def _cmd_pack_add(args: argparse.Namespace) -> int:
    sess = _load(args)
    draft = synthesize.load_pack_draft(sess.state_root, sess.forge_id)
    if draft is None:
        print(json.dumps({"error": "no pack draft"}))
        return 2
    draft.add_item(args.group, json.loads(args.json))
    synthesize.save_pack_draft(sess.state_root, sess.forge_id, draft)
    print(json.dumps({"ok": True, "added_to": args.group}))
    return 0


def _cmd_pack_edit(args: argparse.Namespace) -> int:
    sess = _load(args)
    draft = synthesize.load_pack_draft(sess.state_root, sess.forge_id)
    if draft is None:
        print(json.dumps({"error": "no pack draft"}))
        return 2
    draft.edit(args.group, args.index, json.loads(args.json))
    synthesize.save_pack_draft(sess.state_root, sess.forge_id, draft)
    print(json.dumps({"ok": True, "edited": f"{args.group}[{args.index}]"}))
    return 0


def _cmd_pack_promote(args: argparse.Namespace) -> int:
    import shutil
    from ara.forge.alphabets import pack as pack_mod
    src = pack_mod.runtime_packs_dir(_state(args)) / f"{args.name}.json"
    if not src.exists():
        print(json.dumps({"error": f"no runtime pack {args.name!r} to promote"}))
        return 2
    dst = pack_mod.builtin_pack_path(args.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    print(json.dumps({"promoted": args.name, "to": str(dst)}))
    return 0


def _cmd_pack_list(args: argparse.Namespace) -> int:
    from ara.forge.alphabets import pack as pack_mod
    st = _state(args)
    builtin = set(pack_mod.list_builtin_packs())
    rd = pack_mod.runtime_packs_dir(st)
    runtime = {p.stem for p in rd.glob("*.json")} if rd.is_dir() else set()
    out = [{"name": n, "builtin": n in builtin, "runtime": n in runtime}
           for n in sorted(builtin | runtime)]
    print(json.dumps(out, indent=2))
    return 0


def _bucket(elements: list, key) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in elements:
        k = key(e)
        out[k] = out.get(k, 0) + 1
    return out


# Silence unused-import warning for asdict; reserved for future status output
_ = asdict
