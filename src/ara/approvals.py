"""Approval lifecycle for ARA.

Pending approvals are written to ``.ara/sessions/<sid>/approvals/pending/<id>.json``;
resolutions to ``approvals/resolved/<id>.json``. Operator CLI watches the
former, writes the latter. Supervisor blocks the guard query until a
resolved file appears (or a timeout fires).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Mapping

APPROVAL_TIMEOUT_DEFAULT = 120.0  # seconds
# When timeout auto-grant is enabled, routine (non-danger) approvals fall
# through to ALLOW after this many seconds of operator silence instead of
# stalling the turn. Danger-classified ops never use this path.
AUTOGRANT_TIMEOUT_DEFAULT = 15.0  # seconds

_TRUTHY = {"1", "true", "on", "yes"}
_FALSY = {"0", "false", "off", "no"}


def settings_path(state_root: Path) -> Path:
    """Operator-tunable approval settings, readable by supervisor + MITM and
    writable by the Tools page. Runtime-toggleable without a restart."""
    return Path(state_root) / "approval_settings.json"


def read_settings(state_root: Path) -> dict:
    try:
        return json.loads(settings_path(state_root).read_text())
    except Exception:  # noqa: BLE001
        return {}


def write_settings(state_root: Path, settings: dict) -> None:
    p = settings_path(state_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings, indent=2, sort_keys=True))


def _bool_env(env: Mapping[str, str], key: str) -> Optional[bool]:
    v = str(env.get(key) or "").strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return None


def autogrant_enabled(
    state_root: Path, *, environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Whether timeout auto-grant is active. Env override wins, then the
    operator's settings file, then the default (ON — the feature exists to
    keep autonomous runs from stalling; danger ops are gated separately)."""
    env = os.environ if environ is None else environ
    ov = _bool_env(env, "ARA_APPROVAL_AUTOGRANT")
    if ov is not None:
        return ov
    s = read_settings(state_root)
    if isinstance(s.get("autogrant"), bool):
        return s["autogrant"]
    return True


def autogrant_timeout_s(
    state_root: Path, *, environ: Optional[Mapping[str, str]] = None,
) -> float:
    env = os.environ if environ is None else environ
    for src in (env.get("ARA_APPROVAL_AUTOGRANT_S"),
                read_settings(state_root).get("autogrant_timeout_s")):
        try:
            if src is not None and str(src) != "":
                v = float(src)
                if v > 0:
                    return v
        except (TypeError, ValueError):
            continue
    return AUTOGRANT_TIMEOUT_DEFAULT


@dataclass
class ApprovalRecord:
    id: str
    session_id: str
    created_at: float
    op: str                         # e.g. "fs.read", "exec.spawn"
    path: Optional[str] = None
    command: Optional[str] = None
    args: list = field(default_factory=list)
    cwd: Optional[str] = None
    pid: Optional[int] = None
    rule_id: Optional[str] = None
    rule_pattern: Optional[str] = None
    status: str = "pending"         # "pending" | "allowed" | "denied" | "timeout"
    action: Optional[str] = None    # operator's response verb
    resolved_at: Optional[float] = None
    reason: Optional[str] = None
    # When the operator chose an `allow-always`/`deny-always` action, this
    # is the TTL (seconds) before the resulting override/blackhole expires.
    # None = permanent.
    ttl_s: Optional[float] = None
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    resource_weapon_id: Optional[str] = None
    suggested_url: Optional[str] = None
    resolved_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            k: v
            for k, v in self.__dict__.items()
            if v is not None and v != []
        }


def _approvals_root(session_root: Path) -> Path:
    return session_root / "approvals"


def pending_dir(session_root: Path) -> Path:
    return _approvals_root(session_root) / "pending"


def resolved_dir(session_root: Path) -> Path:
    return _approvals_root(session_root) / "resolved"


def new_approval_id() -> str:
    return uuid.uuid4().hex[:10]


def write_pending(session_root: Path, record: ApprovalRecord) -> Path:
    d = pending_dir(session_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{record.id}.json"
    path.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True))
    return path


def read_pending(session_root: Path, approval_id: str) -> Optional[ApprovalRecord]:
    path = pending_dir(session_root) / f"{approval_id}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return ApprovalRecord(**raw)


def list_pending(session_root: Path) -> list[ApprovalRecord]:
    d = pending_dir(session_root)
    if not d.is_dir():
        return []
    out: list[ApprovalRecord] = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(ApprovalRecord(**json.loads(f.read_text())))
        except Exception:
            continue
    return out


def list_resolved(session_root: Path) -> list[ApprovalRecord]:
    d = resolved_dir(session_root)
    if not d.is_dir():
        return []
    out: list[ApprovalRecord] = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(ApprovalRecord(**json.loads(f.read_text())))
        except Exception:
            continue
    return out


def resolve(
    session_root: Path,
    approval_id: str,
    action: str,
    reason: Optional[str] = None,
    ttl_s: Optional[float] = None,
    resolved_url: Optional[str] = None,
) -> Optional[ApprovalRecord]:
    """Mark an approval as resolved. Moves pending → resolved and returns
    the updated record. Returns None if the approval isn't found.

    ``ttl_s`` is honored only for ``allow-always``/``deny-always`` actions;
    the supervisor will give the resulting override/blackhole that expiry."""
    pending = pending_dir(session_root)
    resolved = resolved_dir(session_root)
    pfile = pending / f"{approval_id}.json"
    if not pfile.exists():
        return None
    record = ApprovalRecord(**json.loads(pfile.read_text()))
    record.action = action
    record.resolved_at = time.time()
    record.reason = reason
    record.ttl_s = ttl_s
    record.resolved_url = resolved_url.strip() if resolved_url else None
    if action.startswith("allow"):
        record.status = "allowed"
    elif action.startswith("deny"):
        record.status = "denied"
    elif action == "timeout":
        record.status = "timeout"
    else:
        record.status = "denied"

    resolved.mkdir(parents=True, exist_ok=True)
    rfile = resolved / f"{approval_id}.json"
    rfile.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True))
    with contextlib.suppress(FileNotFoundError):
        pfile.unlink()
    return record


def _resolve_timeout(
    session_root: Path, approval_id: str, on_timeout: str,
) -> "ApprovalRecord":
    """Persist a timeout resolution. ``on_timeout='allow'`` auto-grants
    (routine ops only — the caller vets eligibility); otherwise deny."""
    if on_timeout == "allow":
        action, reason, status = (
            "allow-timeout", "approval_auto_granted_timeout", "allowed")
    else:
        action, reason, status = ("timeout", "approval_timeout", "timeout")
    return resolve(
        session_root, approval_id, action=action, reason=reason,
    ) or ApprovalRecord(
        id=approval_id, session_id="", created_at=time.time(),
        op="", status=status, action=action, reason=reason,
    )


def wait_for_resolution_sync(
    session_root: Path,
    approval_id: str,
    timeout: float = None,  # type: ignore[assignment]
    poll_interval: float = 0.2,
    *,
    on_timeout: str = "deny",
) -> "ApprovalRecord":
    """Synchronous analog of :func:`wait_for_resolution`. Blocks the
    caller thread until the approval resolves or times out; suitable
    for non-async contexts (e.g. the gateway WS tap thread)."""
    if timeout is None:
        timeout = APPROVAL_TIMEOUT_DEFAULT
    rpath = resolved_dir(session_root) / f"{approval_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rpath.exists():
            return ApprovalRecord(**json.loads(rpath.read_text()))
        time.sleep(poll_interval)
    return _resolve_timeout(session_root, approval_id, on_timeout)


async def wait_for_resolution(
    session_root: Path,
    approval_id: str,
    timeout: float = APPROVAL_TIMEOUT_DEFAULT,
    poll_interval: float = 0.2,
    *,
    on_timeout: str = "deny",
) -> ApprovalRecord:
    """Poll until the approval is resolved. Returns the resolved record, or a
    synthesized timeout record on timeout. ``on_timeout='allow'`` auto-grants
    the timeout (routine ops only — the caller must vet eligibility)."""
    rpath = resolved_dir(session_root) / f"{approval_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rpath.exists():
            return ApprovalRecord(**json.loads(rpath.read_text()))
        await asyncio.sleep(poll_interval)
    return _resolve_timeout(session_root, approval_id, on_timeout)
