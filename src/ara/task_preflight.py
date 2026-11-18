"""Task launch preflight shared by Armory actions and views."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import armors as armors_mod
from . import temper as temper_mod
from . import trims as trims_mod
from . import weapons as weapons_mod


@dataclass
class TaskPreflight:
    ok: bool
    message: str
    armor: str | None = None
    weapon_id: str | None = None
    trim_id: str | None = None
    temper_id: str | None = None
    window_end: str | None = None


def check_active_trim_for_task(state_root: Path) -> TaskPreflight:
    armor = armors_mod.active_armor_name(state_root)
    if not armor:
        return TaskPreflight(False, "active armor required for Run Task")
    weapon_id = weapons_mod.equipped_weapon_id(state_root)
    if not weapon_id or not weapons_mod.weapon_exists(state_root, weapon_id):
        return TaskPreflight(
            False,
            "sealed equipped weapon required for Run Task",
            armor=armor,
            weapon_id=weapon_id,
        )
    trim_id = trims_mod.active_trim_id(state_root, armor)
    if not trim_id:
        return TaskPreflight(
            False,
            "active trim required for Run Task",
            armor=armor,
            weapon_id=weapon_id,
        )
    try:
        meta = trims_mod.load_trim(state_root, armor, trim_id)
    except Exception as exc:
        return TaskPreflight(
            False,
            f"active trim {trim_id!r} could not be loaded: {exc}",
            armor=armor,
            weapon_id=weapon_id,
            trim_id=trim_id,
        )
    # Converge policy before launch: ``trims-active`` can be set without
    # apply_trim ever running, leaving a stale/foreign trim block merged in
    # bulwark.md while a different trim is "active" — which silently drops
    # this engagement's net scope (the direct-socket egress leak). Ensure the
    # active trim's block is actually present; re-apply if not. apply_trim now
    # strips all other armor trim blocks, so this also evicts an orphan.
    try:
        bw_path = Path(state_root) / "bulwark.md"
        bw_text = bw_path.read_text() if bw_path.is_file() else ""
        marker = f"# ARA-ARMOR-TRIM-BEGIN {armor} {trim_id}"
        if marker not in bw_text:
            trims_mod.apply_trim(state_root, armor, trim_id)
    except Exception:  # noqa: BLE001
        pass
    if not getattr(meta, "active_testing", False):
        return TaskPreflight(
            False,
            f"active trim {trim_id!r} does not authorize active testing",
            armor=armor,
            weapon_id=weapon_id,
            trim_id=trim_id,
        )
    end = getattr(meta, "test_window_end", None) or getattr(meta, "expires_at", None)
    if end:
        try:
            end_dt = datetime.strptime(str(end), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            return TaskPreflight(
                False,
                f"active trim {trim_id!r} has invalid test window end: {end}",
                armor=armor,
                weapon_id=weapon_id,
                trim_id=trim_id,
                window_end=str(end),
            )
        if end_dt < datetime.now(timezone.utc):
            return TaskPreflight(
                False,
                f"active trim {trim_id!r} trim expired at {end}",
                armor=armor,
                weapon_id=weapon_id,
                trim_id=trim_id,
                window_end=str(end),
            )
    temper_id = temper_mod.active_temper_id(state_root, armor)
    if not temper_id:
        # New model: a temper is a hot-swappable equipment slot (tempers.py),
        # not an armor-scoped proposal. An equipped slot temper satisfies the
        # requirement — its policy is already merged into the live bulwark.md.
        try:
            from ara import tempers as _tempers
            equipped = _tempers.active_tempers(state_root)
        except Exception:  # noqa: BLE001
            equipped = []
        if equipped:
            return TaskPreflight(
                True,
                f"active trim {trim_id!r} preflight passed",
                armor=armor,
                weapon_id=weapon_id,
                trim_id=trim_id,
                temper_id=equipped[0],
                window_end=str(end) if end else None,
            )
        return TaskPreflight(
            False,
            "active temper required for Run Task",
            armor=armor,
            weapon_id=weapon_id,
            trim_id=trim_id,
            window_end=str(end) if end else None,
        )
    try:
        proposal = temper_mod.load_proposal(state_root, armor, temper_id)
    except Exception as exc:
        return TaskPreflight(
            False,
            f"active temper {temper_id!r} could not be loaded: {exc}",
            armor=armor,
            weapon_id=weapon_id,
            trim_id=trim_id,
            temper_id=temper_id,
            window_end=str(end) if end else None,
        )
    if proposal is None or proposal.status not in {"sealed", "applied"}:
        return TaskPreflight(
            False,
            f"active temper {temper_id!r} is not sealed or applied",
            armor=armor,
            weapon_id=weapon_id,
            trim_id=trim_id,
            temper_id=temper_id,
            window_end=str(end) if end else None,
        )
    materialized = temper_mod.active_temper_materialized_dir(
        state_root, armor, temper_id,
    )
    if not materialized.exists():
        return TaskPreflight(
            False,
            f"active temper {temper_id!r} is not materialized",
            armor=armor,
            weapon_id=weapon_id,
            trim_id=trim_id,
            temper_id=temper_id,
            window_end=str(end) if end else None,
        )
    return TaskPreflight(
        True,
        f"active trim {trim_id!r} preflight passed",
        armor=armor,
        weapon_id=weapon_id,
        trim_id=trim_id,
        temper_id=temper_id,
        window_end=str(end) if end else None,
    )
