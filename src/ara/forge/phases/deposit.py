"""DEPOSIT phase: operator uploads mats into ``forge/<forge_id>/mats/``.

Each mat copied with a stable filename + sha256 metadata sidecar.
Audit logged.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ara.forge import state as fstate
from ara.forge.session import ForgePhase, ForgeSession
from ara.runtime import audit as raudit


_META_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class DepositRecord:
    mat_filename: str
    sha256: str
    size_bytes: int
    deposited_at: float
    original_path: str
    source_url: str | None = None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def deposit_mat(
    sess: ForgeSession,
    src: Path,
    *,
    rename_to: str | None = None,
    source_url: str | None = None,
) -> DepositRecord:
    if sess.phase != ForgePhase.DEPOSIT:
        raise ValueError(
            f"deposit_mat requires phase=DEPOSIT (current: {sess.phase.value})"
        )
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    mats_dir = fstate.forge_mats_dir(sess.state_root, sess.forge_id)
    mats_dir.mkdir(parents=True, exist_ok=True)
    target_name = rename_to or src.name
    target = mats_dir / target_name
    counter = 1
    while target.exists():
        target = mats_dir / f"{src.stem}__{counter}{src.suffix}"
        counter += 1
    shutil.copyfile(src, target)
    sha = _sha256_file(target)
    rec = DepositRecord(
        mat_filename=target.name,
        sha256=sha,
        size_bytes=target.stat().st_size,
        deposited_at=time.time(),
        original_path=str(src),
        source_url=source_url,
    )
    meta = mats_dir / f"{target.name}{_META_SUFFIX}"
    tmp = meta.with_suffix(".meta.json.tmp")
    tmp.write_text(json.dumps({
        "mat_filename": rec.mat_filename,
        "sha256": rec.sha256,
        "size_bytes": rec.size_bytes,
        "deposited_at": rec.deposited_at,
        "original_path": rec.original_path,
        "source_url": rec.source_url,
    }, sort_keys=True, indent=2))
    os.replace(tmp, meta)
    raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {"mat_filename": rec.mat_filename, "sha256": rec.sha256,
         "size_bytes": rec.size_bytes, "source_url": rec.source_url},
        kind="deposit.added",
    )

    # v0.1: hard-link this mat into the engagement's source/forge-<fid>/
    # so trims pick it up via populated_classes() without re-uploading.
    # Best-effort — failures don't block the deposit.
    if sess.engagement_id:
        try:
            from ara import engagement_mats as _em
            _em.add_mat_path(
                sess.state_root, sess.engagement_id, "source",
                target,
                dest_name=f"forge-{sess.forge_id}__{target.name}",
                uploaded_by=f"forge:{sess.forge_id}",
                tags=["forge"], link=True,
            )
        except Exception:
            pass

    return rec


def remove_mat(sess: ForgeSession, mat_filename: str) -> None:
    if sess.phase != ForgePhase.DEPOSIT:
        raise ValueError(
            f"remove_mat requires phase=DEPOSIT (current: {sess.phase.value})"
        )
    mats_dir = fstate.forge_mats_dir(sess.state_root, sess.forge_id)
    target = mats_dir / mat_filename
    meta = mats_dir / f"{mat_filename}{_META_SUFFIX}"
    if target.exists():
        target.unlink()
    if meta.exists():
        meta.unlink()
    raudit.append(
        fstate.forge_audit_path(sess.state_root, sess.forge_id),
        {"mat_filename": mat_filename}, kind="deposit.removed",
    )


def list_mats(sess: ForgeSession) -> list[DepositRecord]:
    mats_dir = fstate.forge_mats_dir(sess.state_root, sess.forge_id)
    if not mats_dir.exists():
        return []
    out: list[DepositRecord] = []
    for meta_path in sorted(mats_dir.glob(f"*{_META_SUFFIX}")):
        try:
            d = json.loads(meta_path.read_text())
        except Exception:
            continue
        out.append(DepositRecord(
            mat_filename=d["mat_filename"],
            sha256=d["sha256"],
            size_bytes=d["size_bytes"],
            deposited_at=d["deposited_at"],
            original_path=d.get("original_path", ""),
            source_url=d.get("source_url"),
        ))
    return out


def finalize_deposit(sess: ForgeSession) -> None:
    """Operator finalizes upload — transitions DEPOSIT → MANIFEST."""
    mats = list_mats(sess)
    if not mats:
        raise ValueError("cannot finalize deposit: no mats deposited")
    sess.transition(ForgePhase.MANIFEST,
                    reason="deposit finalized",
                    meta={"mat_count": len(mats)})
