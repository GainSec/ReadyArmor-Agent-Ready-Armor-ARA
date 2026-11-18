"""ForgeSession lifecycle and persistence.

Phase machine (orchestrator skeleton §3): DEPOSIT → MANIFEST → REVIEW
→ EXHAUST → REVIEW → SYNTHESIZE → REVIEW → SEAL → SEALED. Reentrant
REVIEW state. ABORTED is terminal.

Each transition appends a hash-chained event to the forge audit log.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from uuid import uuid4

from ara.forge import state as fstate
from ara.runtime import audit as raudit


class ForgePhase(str, Enum):
    DEPOSIT = "deposit"
    MANIFEST = "manifest"
    EXHAUST = "exhaust"
    SYNTHESIZE = "synthesize"
    REVIEW = "review"
    SEAL = "seal"
    SEALED = "sealed"
    ABORTED = "aborted"
    REFORGE_PROPOSAL = "reforge_proposal"


class ForgeKind(str, Enum):
    """What a forge session produces. ``weapon`` (default) is the classic
    weapon-forge; ``pack`` forges a new alphabet pack from mats. Stored on
    ``ForgeConfig.kind`` as its string value; branch with
    ``config.kind == ForgeKind.PACK.value``."""
    WEAPON = "weapon"
    PACK = "pack"


_VALID_TRANSITIONS: dict[ForgePhase, set[ForgePhase]] = {
    ForgePhase.DEPOSIT: {ForgePhase.MANIFEST, ForgePhase.ABORTED},
    ForgePhase.MANIFEST: {ForgePhase.REVIEW, ForgePhase.ABORTED},
    ForgePhase.REVIEW: {
        ForgePhase.EXHAUST,
        ForgePhase.SYNTHESIZE,
        ForgePhase.SEAL,
        ForgePhase.MANIFEST,  # operator can request manifest re-run
        ForgePhase.REFORGE_PROPOSAL,
        ForgePhase.ABORTED,
    },
    ForgePhase.EXHAUST: {ForgePhase.REVIEW, ForgePhase.ABORTED},
    ForgePhase.SYNTHESIZE: {ForgePhase.REVIEW, ForgePhase.ABORTED},
    ForgePhase.SEAL: {ForgePhase.SEALED, ForgePhase.REVIEW, ForgePhase.ABORTED},
    ForgePhase.SEALED: set(),
    ForgePhase.ABORTED: set(),
    ForgePhase.REFORGE_PROPOSAL: {ForgePhase.REVIEW, ForgePhase.ABORTED},
}


class InvalidTransition(Exception):
    pass


@dataclass
class ForgeConfig:
    embedder_provider: str = "null"  # "null" | "local-onnx" (D3)
    embedder_model_id: str | None = None
    coverage_threshold: float = 1.0
    calibration_min_precision_at_5: float = 0.6
    auto_advance: bool = False
    alphabet_pack: str = "offsec"             # name of built-in pack
    allow_starter_alphabets: bool = False     # opt-in starter entries (Q4-B)
    kind: str = "weapon"                       # ForgeKind value; pack-forge branches on == ForgeKind.PACK.value
    forge_policy: bool = True                  # pack-forge: propose bulwark/rhema policy from mats (operator-reviewed); False = hermes-default only
    chunk_workflow: str = "general"            # pack-forge: named investigation-loop baseline (chunk_workflows); operator-selected at create


@dataclass
class ForgeSession:
    forge_id: str
    state_root: Path
    weapon_target_id: str
    weapon_predecessor_id: str | None
    phase: ForgePhase
    created_at: float
    last_advanced_at: float
    operator_id: str
    config: ForgeConfig = field(default_factory=ForgeConfig)
    # State captured at session-create time so the equipped armor /
    # weapon can be restored on seal/abort (per the blacksmith-hammer
    # spec §7.3). ``None`` means "nothing was equipped — restore to
    # nothing." If the operator was already running blacksmith, both
    # are stored as ``None`` so unequip doesn't loop forever.
    prior_armor: str | None = None
    prior_weapon: str | None = None
    # v0.1: forge ↔ engagement linkage. When set, deposited mats are
    # hard-linked into <state>/engagements/<eid>/mats/source/forge-<fid>/
    # so the trim picks them up via populated_classes() without the
    # operator uploading twice.
    engagement_id: str | None = None

    @classmethod
    def new(
        cls,
        state_root: Path,
        *,
        weapon_target_id: str,
        operator_id: str,
        weapon_predecessor_id: str | None = None,
        config: ForgeConfig | None = None,
        forge_id: str | None = None,
        engagement_id: str | None = None,
    ) -> "ForgeSession":
        fid = forge_id or uuid4().hex[:10]
        now = time.time()
        # Capture pre-forge equipped state so seal/abort can restore.
        # Local imports keep the forge subsystem importable in test
        # contexts that don't load armors/weapons.
        from ara import armors as _armors
        from ara import weapons as _weapons
        prior_armor = _armors.active_armor_name(Path(state_root))
        prior_weapon = _weapons.equipped_weapon_id(Path(state_root))
        # Don't capture our own seeded equip so re-running forge new
        # against an already-blacksmith-equipped state doesn't lock
        # the operator into blacksmith forever on unequip.
        if prior_armor == "blacksmith":
            prior_armor = None
        if prior_weapon == "blacksmith-hammer":
            prior_weapon = None
        sess = cls(
            forge_id=fid,
            state_root=Path(state_root),
            weapon_target_id=weapon_target_id,
            weapon_predecessor_id=weapon_predecessor_id,
            phase=ForgePhase.DEPOSIT,
            created_at=now,
            last_advanced_at=now,
            operator_id=operator_id,
            config=config or ForgeConfig(),
            prior_armor=prior_armor,
            prior_weapon=prior_weapon,
            engagement_id=engagement_id,
        )
        fstate.ensure_forge_dirs(state_root, fid)
        sess._persist()
        # If an engagement was named, ensure its dir exists so the
        # downstream deposit can hard-link mats in. Best-effort —
        # the import is local so test contexts that don't load
        # engagement_mats stay green.
        if engagement_id:
            try:
                from ara import engagement_mats as _em
                _em.ensure_engagement(Path(state_root), engagement_id)
            except Exception:
                pass
        sess._audit("session.created", {
            "weapon_target_id": weapon_target_id,
            "weapon_predecessor_id": weapon_predecessor_id,
            "operator_id": operator_id,
            "prior_armor": prior_armor,
            "prior_weapon": prior_weapon,
            "engagement_id": engagement_id,
            "config": asdict(sess.config),
        })
        return sess

    @classmethod
    def load(cls, state_root: Path, forge_id: str) -> "ForgeSession":
        path = fstate.forge_session_path(state_root, forge_id)
        data = json.loads(path.read_text())
        return cls(
            forge_id=data["forge_id"],
            state_root=Path(state_root),
            weapon_target_id=data["weapon_target_id"],
            weapon_predecessor_id=data.get("weapon_predecessor_id"),
            phase=ForgePhase(data["phase"]),
            created_at=data["created_at"],
            last_advanced_at=data["last_advanced_at"],
            operator_id=data["operator_id"],
            config=ForgeConfig(**data.get("config", {})),
            prior_armor=data.get("prior_armor"),
            prior_weapon=data.get("prior_weapon"),
            engagement_id=data.get("engagement_id"),
        )

    def transition(self, target: ForgePhase, *, reason: str = "",
                   meta: dict | None = None) -> None:
        allowed = _VALID_TRANSITIONS.get(self.phase, set())
        if target not in allowed:
            raise InvalidTransition(
                f"cannot transition {self.phase.value} → {target.value}"
            )
        prior = self.phase
        self.phase = target
        self.last_advanced_at = time.time()
        self._persist()
        self._audit("phase.transition", {
            "from": prior.value,
            "to": target.value,
            "reason": reason,
            "meta": meta or {},
        })

    def abort(self, reason: str = "") -> None:
        if self.phase in (ForgePhase.SEALED, ForgePhase.ABORTED):
            return
        self.transition(ForgePhase.ABORTED, reason=reason)

    def save(self) -> None:
        """Persist the session to ``session.json`` + ``phase.txt``.

        Use after mutating ``self.config`` or other dataclass fields
        (e.g. flipping ``allow_starter_alphabets`` or
        ``coverage_threshold``). Phase transitions go through
        :meth:`transition` and persist automatically.
        """
        self._persist()

    def to_dict(self) -> dict:
        return {
            "forge_id": self.forge_id,
            "weapon_target_id": self.weapon_target_id,
            "weapon_predecessor_id": self.weapon_predecessor_id,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "last_advanced_at": self.last_advanced_at,
            "operator_id": self.operator_id,
            "config": asdict(self.config),
            "prior_armor": self.prior_armor,
            "prior_weapon": self.prior_weapon,
            "engagement_id": self.engagement_id,
        }

    # --- internals ---

    def _persist(self) -> None:
        path = fstate.forge_session_path(self.state_root, self.forge_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), sort_keys=True, indent=2))
        os.replace(tmp, path)
        phase_path = fstate.forge_phase_path(self.state_root, self.forge_id)
        phase_path.write_text(self.phase.value)

    def _audit(self, kind: str, body: dict) -> None:
        sink = fstate.forge_audit_path(self.state_root, self.forge_id)
        raudit.append(sink, body, kind=kind)
