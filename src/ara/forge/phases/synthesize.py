"""SYNTHESIZE phase: compose weapon_draft.json from extraction events.

Each extraction's channel determines which weapon component it
contributes to:

- ``extract.proc``  → crumbs (procedure with requires/action/emits)
- ``extract.conc``  → shapes / output schemas (severity rubric, fix lib, etc.)
- ``extract.fact_shape`` → fact-shape registrations (1.1)
- ``extract.store`` → resource libraries (payload sets, fixes)
- ``extract.line``  → line-deny rules
- ``fact.assert``   → seed facts in the weapon (mat-derived ground truth)

Citation chain enforced: every weapon component carries the
``manifest_element_id`` of its source extraction.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ara import resources as resource_mod
from ara.forge import state as fstate
from ara.forge.alphabets.pack import AlphabetPack, load_builtin_pack, resolve_pack
from ara.forge.alphabets.projector import (
    DerivedAlphabets,
    merge_starter,
    project as project_alphabets,
    validate_foreign_keys,
)
from ara.forge.phases.exhaust import read_extractions
from ara.forge.session import ForgePhase, ForgeSession
from ara.runtime import audit as raudit


@dataclass
class WeaponDraft:
    weapon_target_id: str
    forge_id: str
    generated_at: float
    crumbs: list[dict] = field(default_factory=list)
    shapes: list[dict] = field(default_factory=list)
    fact_shapes: list[dict] = field(default_factory=list)
    stores: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)
    seed_facts: list[dict] = field(default_factory=list)
    # Policy-rule extractions (mat-derived). Stored structured here so
    # the dashboard can audit per-rule, then rendered into ``policy``
    # text strings below for the equip-time hot-swap.
    rhema_rules: list[dict] = field(default_factory=list)
    bulwark_rules: list[dict] = field(default_factory=list)
    walls_paths: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    style: dict | None = None
    alphabets: dict | None = None  # serialized DerivedAlphabets summary
    alphabet_pack_name: str | None = None
    policy: dict[str, str] = field(default_factory=dict)  # bulwark/walls/rhema text
    metadata: dict = field(default_factory=dict)

    def to_jsonable(self) -> dict:
        return {
            "weapon_target_id": self.weapon_target_id,
            "forge_id": self.forge_id,
            "generated_at": self.generated_at,
            "crumbs": self.crumbs,
            "shapes": self.shapes,
            "fact_shapes": self.fact_shapes,
            "stores": self.stores,
            "resources": self.resources,
            "lines": self.lines,
            "seed_facts": self.seed_facts,
            "rhema_rules": self.rhema_rules,
            "bulwark_rules": self.bulwark_rules,
            "walls_paths": self.walls_paths,
            "chunks": self.chunks,
            "style": self.style,
            "alphabets": self.alphabets,
            "alphabet_pack_name": self.alphabet_pack_name,
            "policy": dict(self.policy),
            "metadata": self.metadata,
        }


_CHANNEL_TO_BUCKET = {
    "extract.proc": "crumbs",
    "extract.conc": "shapes",
    "extract.fact_shape": "fact_shapes",
    "extract.store": "stores",
    "extract.resource": "resources",
    "extract.line": "lines",
    "fact.assert": "seed_facts",
    # Policy-extraction channels: their bucket lists feed the
    # ``_render_policy_text`` rendering below, which composes
    # ``draft.policy[{rhema,bulwark,walls}]`` strings written to
    # ``<weapon>/policy/*.md`` at seal time.
    "extract.rhema_rule": "rhema_rules",
    "extract.bulwark_rule": "bulwark_rules",
    "extract.walls_path": "walls_paths",
}


def _render_rhema_text(rules: list[dict]) -> str:
    """Compose a ``rhema.md`` text block from agent-extracted rule
    bodies. Each rule must carry ``action``, ``layer``, ``pattern``;
    ``rationale`` and ``evidence_quote`` are echoed as comments.
    """
    if not rules:
        return ""
    lines = [
        "# RHEMA — synthesized from operator mats",
        "#",
        "# Prompt-mediation rules extracted by the forge agent from the",
        "# deposited materials. Each rule carries a rationale and a",
        "# manifest_element_id citation back to its source.",
        "",
    ]
    for r in rules:
        body = r if not isinstance(r.get("body"), dict) else r["body"]
        action = (body.get("action") or "").strip()
        layer = (body.get("layer") or "").strip()
        pattern = (body.get("pattern") or "").strip()
        rationale = (body.get("rationale") or "").strip()
        cite = (r.get("citation") or {}).get("manifest_element_id", "")
        if not (action and layer and pattern):
            continue
        if rationale:
            lines.append(f"# {rationale}")
        if cite:
            lines.append(f"# cite: {cite}")
        lines.append(f"{action:<8} {layer:<6} {pattern}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_bulwark_text(rules: list[dict]) -> str:
    """Compose a ``bulwark.md`` text block. Rules go under
    ``## armor: weapon`` so they layer on top of standing armor rules
    when the weapon is equipped (matches existing equip semantics)."""
    if not rules:
        return ""
    lines = [
        "# BULWARK — synthesized from operator mats",
        "#",
        "# Layered policy rules extracted by the forge agent. Apply when",
        "# this weapon is equipped on top of the active armor.",
        "",
        "## armor: weapon",
        "",
    ]
    for r in rules:
        body = r if not isinstance(r.get("body"), dict) else r["body"]
        action = (body.get("action") or "").strip()
        layer = (body.get("layer") or "").strip()
        match = (body.get("match") or "").strip()
        rationale = (body.get("rationale") or "").strip()
        cite = (r.get("citation") or {}).get("manifest_element_id", "")
        if not (action and layer and match):
            continue
        if rationale:
            lines.append(f"# {rationale}")
        if cite:
            lines.append(f"# cite: {cite}")
        lines.append(f"{action:<8} {layer:<5} {match}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_walls_text(paths: list[dict]) -> str:
    """Compose a ``WALLS.md`` text block from extracted path/command
    entries. Groups by kind (deny_path / deny_command / approval_path)."""
    if not paths:
        return ""
    by_kind: dict[str, list[dict]] = {}
    for p in paths:
        body = p if not isinstance(p.get("body"), dict) else p["body"]
        kind = (body.get("kind") or "").strip()
        if not kind:
            continue
        by_kind.setdefault(kind, []).append(body | {
            "_cite": (p.get("citation") or {}).get("manifest_element_id", "")
        })
    if not by_kind:
        return ""
    lines = [
        "# WALLS — synthesized from operator mats",
        "#",
        "# Resource-centric deny rules extracted by the forge agent.",
        "# Each block lists path globs / command names with the source",
        "# manifest_element_id as a leading comment.",
        "",
    ]
    for kind in ("deny_path", "deny_command", "approval_path"):
        block = by_kind.get(kind) or []
        if not block:
            continue
        lines.append(f"## {kind}")
        for b in block:
            value = (b.get("value") or "").strip()
            rationale = (b.get("rationale") or "").strip()
            cite = b.get("_cite") or ""
            if not value:
                continue
            if rationale:
                lines.append(f"# {rationale}")
            if cite:
                lines.append(f"# cite: {cite}")
            lines.append(value)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class AlphabetConflictError(Exception):
    """Raised when CONC extractions disagree on the same alphabet entry.

    Operator must resolve via ``<weapon>/alphabets/_conflicts.json``
    before re-running synthesize (Q3-B in the design doc).
    """


def synthesize(sess: ForgeSession) -> WeaponDraft:
    if sess.phase not in (ForgePhase.SYNTHESIZE, ForgePhase.REVIEW):
        raise ValueError(
            f"synthesize requires SYNTHESIZE or REVIEW (current: {sess.phase.value})"
        )
    extractions = read_extractions(sess.state_root, sess.forge_id)
    draft = WeaponDraft(
        weapon_target_id=sess.weapon_target_id,
        forge_id=sess.forge_id,
        generated_at=time.time(),
    )
    conc_for_alphabets: list[dict] = []
    for evt in extractions:
        bucket = _CHANNEL_TO_BUCKET.get(evt.channel)
        if bucket is None:
            continue
        component = dict(evt.body)
        component.setdefault("citation", evt.citation or {
            "manifest_element_id": evt.manifest_element_id,
        })
        component.setdefault("extraction_id", evt.extraction_id)
        if evt.channel == "extract.resource":
            component = resource_mod.normalize_resource_intent(
                component,
                citation=component.get("citation"),
                extraction_id=component.get("extraction_id"),
            )
        getattr(draft, bucket).append(component)
        # CONC bodies that target an alphabet (carry alphabet_name) are
        # also fed to the alphabet projector. Existing shape behavior
        # (storing in draft.shapes) is unchanged for backwards
        # compatibility with non-alphabet CONC extractions.
        if evt.channel == "extract.conc" and evt.body.get("alphabet_name"):
            conc_for_alphabets.append(evt.body)
    draft.metadata["extraction_count"] = len(extractions)

    # ---------- alphabet projection ----------
    pack_name = sess.config.alphabet_pack
    pack: AlphabetPack | None = None
    if pack_name:
        try:
            pack = resolve_pack(pack_name, sess.state_root)
        except FileNotFoundError:
            pack = None
    if pack is not None:
        derived = project_alphabets(conc_for_alphabets, pack)
        if derived.has_conflicts():
            raise AlphabetConflictError(
                f"alphabet projection produced {len(derived.conflicts)} "
                f"conflict(s); resolve via <weapon>/alphabets/_conflicts.json "
                f"before re-running synthesize"
            )
        if sess.config.allow_starter_alphabets:
            merge_starter(derived)
        validate_foreign_keys(derived)
        draft.alphabet_pack_name = pack.name
        draft.alphabets = _summarize_derived(derived)
        # Pack-supplied chunk templates: copy into the draft only when
        # the operator hasn't already authored chunks. Operator-authored
        # chunks always win — the pack ships defaults, not policy.
        if not draft.chunks and pack.chunk_templates:
            draft.chunks = [dict(c) for c in pack.chunk_templates]
        # Pack-shipped policy stack (bulwark/walls/rhema). Copied into
        # the draft so seal writes them to <weapon>/policy/*.md and
        # ``ara weapon equip`` can hot-swap them into state_root.
        if not draft.policy and pack.policy_defaults:
            draft.policy = dict(pack.policy_defaults)

    # ---------- agent-extracted policy ----------
    # Render the rhema/bulwark/walls extractions into ``draft.policy``
    # text. Agent output WINS over pack defaults: if the operator's
    # mats produced rule entries, those are the operator's intent.
    # Pack defaults survive only when the agent emitted nothing for a
    # given layer (gives operators a sensible baseline rather than an
    # empty policy file).
    rhema_text = _render_rhema_text(draft.rhema_rules)
    if rhema_text:
        draft.policy["rhema"] = rhema_text
    bulwark_text = _render_bulwark_text(draft.bulwark_rules)
    if bulwark_text:
        draft.policy["bulwark"] = bulwark_text
    resource_bulwark_text = resource_mod.render_resource_bulwark_text(
        draft.resources
    )
    if resource_bulwark_text:
        existing = draft.policy.get("bulwark") or ""
        draft.policy["bulwark"] = (
            existing.rstrip() + "\n\n" + resource_bulwark_text
            if existing.strip() else resource_bulwark_text
        )
    walls_text = _render_walls_text(draft.walls_paths)
    if walls_text:
        draft.policy["walls"] = walls_text

    out = fstate.forge_weapon_draft_path(sess.state_root, sess.forge_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(draft.to_jsonable(), sort_keys=True, indent=2))
    os.replace(tmp, out)
    raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {
            "crumbs": len(draft.crumbs),
            "shapes": len(draft.shapes),
            "fact_shapes": len(draft.fact_shapes),
            "stores": len(draft.stores),
            "resources": len(draft.resources),
            "lines": len(draft.lines),
            "seed_facts": len(draft.seed_facts),
            "rhema_rules": len(draft.rhema_rules),
            "bulwark_rules": len(draft.bulwark_rules),
            "walls_paths": len(draft.walls_paths),
        },
        kind="synthesize.complete",
    )
    return draft


def _summarize_derived(derived: DerivedAlphabets) -> dict:
    """Compact summary of a DerivedAlphabets bundle for the draft.

    The full structured forms are written to disk by the seal phase via
    ``projector.write_derived``; the draft only carries counts + ids
    so the JSON stays readable in CLI status output.
    """
    out: dict[str, dict] = {}
    for name, alpha in derived.categorical.items():
        out[name] = {
            "shape": "categorical",
            "entry_count": len(alpha.entries),
            "ids": sorted(alpha.entries),
        }
    for name, m in derived.matrix.items():
        out[name] = {
            "shape": "matrix",
            "axes": list(m.axes),
            "cell_count": len(m.cells),
            "buckets": dict(m.buckets),
        }
    for name, ts in derived.template_set.items():
        out[name] = {
            "shape": "template_set",
            "template_count": len(ts.templates),
            "ids": sorted(ts.templates),
        }
    return out


# ---------- pack-forge (kind=pack) ----------

def pack_draft_path(state_root: Path, forge_id: str) -> Path:
    return fstate.forge_dir(state_root, forge_id) / "pack_draft.json"


def _hermes_default_policy_baseline() -> dict:
    """The hermes-default armor bulwark as the pack policy baseline."""
    from ara.seed import seed_root
    p = seed_root() / "armors" / "hermes-default" / "bulwark-section.md"
    try:
        return {"bulwark": p.read_text()}
    except OSError:
        return {}


def synthesize_pack(sess: ForgeSession):
    """Assemble a PackDraft from the pen agent's proposals (kind=pack) and
    persist ``pack_draft.json``. Returns the draft. Seeds chunk-graph +
    policy baselines when the agent proposed none."""
    from ara.forge.pack_draft import build_pack_draft
    extractions = read_extractions(sess.state_root, sess.forge_id)
    events = [{"channel": e.channel, "body": e.body,
               "manifest_element_id": e.manifest_element_id} for e in extractions]
    # Seed the operator-selected investigation workflow (general/firmware/
    # mobile/...) as the chunk baseline. Falls back to the offsec loop.
    try:
        from ara.forge import chunk_workflows
        chunk_baseline = chunk_workflows.load_workflow(
            getattr(sess.config, "chunk_workflow", chunk_workflows.DEFAULT),
            sess.state_root)
        if not chunk_baseline:
            chunk_baseline = load_builtin_pack("offsec").chunk_templates
    except Exception:  # noqa: BLE001
        chunk_baseline = []
    draft = build_pack_draft(
        events, name=sess.weapon_target_id,
        chunk_baseline=chunk_baseline,
        policy_baseline=_hermes_default_policy_baseline(),
    )
    path = pack_draft_path(sess.state_root, sess.forge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft.to_jsonable(), sort_keys=True, indent=2))
    return draft


def load_pack_draft(state_root: Path, forge_id: str):
    from ara.forge.pack_draft import PackDraft
    path = pack_draft_path(state_root, forge_id)
    if not path.exists():
        return None
    return PackDraft.from_jsonable(json.loads(path.read_text()))


def save_pack_draft(state_root: Path, forge_id: str, draft) -> None:
    path = pack_draft_path(state_root, forge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft.to_jsonable(), sort_keys=True, indent=2))


def load_draft(state_root: Path, forge_id: str) -> WeaponDraft | None:
    p = fstate.forge_weapon_draft_path(state_root, forge_id)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return WeaponDraft(
        weapon_target_id=d["weapon_target_id"],
        forge_id=d["forge_id"],
        generated_at=d["generated_at"],
        crumbs=d.get("crumbs") or [],
        shapes=d.get("shapes") or [],
        fact_shapes=d.get("fact_shapes") or [],
        stores=d.get("stores") or [],
        resources=d.get("resources") or [],
        lines=d.get("lines") or [],
        seed_facts=d.get("seed_facts") or [],
        rhema_rules=d.get("rhema_rules") or [],
        bulwark_rules=d.get("bulwark_rules") or [],
        walls_paths=d.get("walls_paths") or [],
        chunks=d.get("chunks") or [],
        style=d.get("style"),
        alphabets=d.get("alphabets"),
        alphabet_pack_name=d.get("alphabet_pack_name"),
        policy=dict(d.get("policy") or {}),
        metadata=d.get("metadata") or {},
    )


def save_draft(state_root: Path, forge_id: str, draft: WeaponDraft) -> None:
    out = fstate.forge_weapon_draft_path(state_root, forge_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(draft.to_jsonable(), sort_keys=True, indent=2))
    os.replace(tmp, out)
