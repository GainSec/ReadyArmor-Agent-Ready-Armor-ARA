"""Approval-token primitive (BRA-derived).

A token encodes an operator-granted authorization for a class of
operation. ``require_token()`` checks at policy boundaries; ``set_token``
/ ``unset_token`` toggle them; one-time tokens auto-consume on first
matching ``require_token`` call.

Persistence: ``<state>/tokens/<TOKEN-NAME>`` (mode 0o600). Each file
carries a small JSON record:

    {
      "set_at":     "2026-04-27T14:32:01Z",
      "set_by":     "operator@acme.com",
      "reason":     "engagement-acme-portal step 4 elected YES",
      "expires_at": null | "2026-05-13T23:59:59Z",
      "one_time":   false,
      "target":     null | "billing.acme.com"
    }

Audit chain: every set / consume / unset writes one event to
``<state>/tokens.audit.jsonl`` via the same ``SyncAuditWriter``
primitive used by the runtime audit chain. Tokens predate per-session
context, so they get their own audit log at the state root rather
than under a session dir.

Registry sources (per spec §17.1):

* Built-in (``BUILTIN_TOKENS`` here).
* Operator config: ``<state>/tokens.json`` keyed by token name with
  ``{description, one_time, source}`` values.
* Trim-declared: ``meta.json:declared_tokens: [...]`` per trim,
  loaded by ``apply_trim``.
* Weapon-declared: ``<weapon>/seal.json:declared_tokens: [...]``,
  loaded by ``equip_policy``.
* Programmatic: ``register_token(name, ...)``.

The registry is mutable and ARA does NOT enforce membership at the
``set_token`` / ``has_token`` boundary — name validation is
shape-only (``UPPERCASE-WITH-HYPHENS-AND-COLONS-AND-DOTS``), and the
registry exists for documentation + UI listings.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from ara.runtime import audit as raudit


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenSpec:
    """Metadata for a registered token. Returned by :func:`known_tokens`."""
    name: str
    description: str = ""
    one_time: bool = False
    source: str = "builtin"          # builtin | operator | trim:<id> | weapon:<id> | programmatic


# Built-in core. Trims, weapons, operator config can extend at runtime.
BUILTIN_TOKENS: tuple[str, ...] = (
    # Engagement gates
    "ENGAGEMENT-AUTHORIZED",          # ethics confirmation from intake
    "TRIM-CONFIRMED",                 # specific trim is locked + applied
    "SCOPE-GRANT",                    # one-time per-target add (auto-consumed)

    # Capability gates (operator-elected during intake)
    "ACTIVE-TESTING-APPROVAL",        # network-based testing allowed
    "OFFENSIVE-TESTING-APPROVAL",     # exploitation allowed
    "DESTRUCTIVE-APPROVAL",           # DEPRECATED — use ENV-PROD/NONPROD per v0.1
    "SYSTEM-INSTALL-APPROVAL",        # apt install etc.

    # ARA framework gates
    "BULWARK-EDIT-APPROVAL",          # edits to bulwark.md outside the wizard
    "ARMOR-EDIT-APPROVAL",            # edits to armor identity files
    "WEAPON-IMPORT-APPROVAL",         # importing a third-party weapon

    # Privacy
    "ANON-DISABLE",                   # explicitly turn off anonymization

    # ── v0.1 amendment (engagement contract expansion) ──────────────
    # Environment class (replaces the leaky DESTRUCTIVE-APPROVAL gate)
    "ENV-PROD",                       # standing — engagement runs against prod
    "ENV-NONPROD",                    # standing — engagement runs against nonprod
    "PROD-DESTRUCTIVE-APPROVAL",      # one-time, per-action — gates state-changing ops on prod
    "ACTIVE-PROD-APPROVAL",           # standing — required for active+prod combo

    # Posture (knowledge level)
    "POSTURE-GRAYBOX",                # standing — partial mats provided
    "POSTURE-WHITEBOX",               # standing — full source/artifacts provided

    # Time-of-day modal outcomes
    "WINDOW-EXTEND",                  # one-time, TTL'd — operator extends past TOD close
    "WINDOW-PIVOT-READONLY",          # one-time, TTL'd — pivot to artifact review only

    # Stop-condition resume
    "RESUME-AFTER-STOP",              # one-time — operator ack + resume after stop trip

    # Knowledge-gap recon override
    "RUN-RECON-DESPITE-KNOWLEDGE",    # one-time, per-crumb — re-run recon despite mats coverage
)

# Built-in defaults: which tokens are documented as one-time at registration
# time (vs. operator/programmatic registration which can override).
_BUILTIN_ONE_TIME: frozenset[str] = frozenset({
    "SCOPE-GRANT",                    # consumed on first matching request
    # v0.1 one-time tokens
    "PROD-DESTRUCTIVE-APPROVAL",      # consumed per state-changing prod op
    "WINDOW-EXTEND",                  # consumed on TOD-extend grant
    "WINDOW-PIVOT-READONLY",          # consumed on pivot grant
    "RESUME-AFTER-STOP",              # consumed on operator resume
    "RUN-RECON-DESPITE-KNOWLEDGE",    # consumed per recon override
})

_REGISTRY: dict[str, TokenSpec] = {}
_REGISTRY_LOCK = threading.RLock()
_OPERATOR_CONFIG_LOADED: dict[Path, float] = {}   # state_root → mtime


def _populate_builtins() -> None:
    """Idempotent built-in registration. Called on first registry access."""
    with _REGISTRY_LOCK:
        for name in BUILTIN_TOKENS:
            if name not in _REGISTRY:
                _REGISTRY[name] = TokenSpec(
                    name=name,
                    description="",
                    one_time=name in _BUILTIN_ONE_TIME,
                    source="builtin",
                )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# UPPERCASE-WITH-HYPHENS-AND-COLONS-AND-DOTS. The colon allows targeted
# tokens like ``SCOPE-GRANT:billing.acme.com`` and ``TRIM-CONFIRMED:eng-acme-portal-2026-04``.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_-]*(?::[A-Za-z0-9_.-]+)?$")


class InvalidTokenName(ValueError):
    """Raised when a token name violates the shape grammar."""


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise InvalidTokenName(
            f"invalid token name {name!r}: expected UPPERCASE-WITH-HYPHENS"
            f" optionally followed by ':<target>'"
        )
    return name


def _split_qualified(name: str) -> tuple[str, str | None]:
    """Split a qualified token name into (base, target).

    ``ACTIVE-TESTING-APPROVAL`` → ``("ACTIVE-TESTING-APPROVAL", None)``
    ``SCOPE-GRANT:billing.acme.com`` → ``("SCOPE-GRANT", "billing.acme.com")``
    """
    if ":" in name:
        base, target = name.split(":", 1)
        return base, target
    return name, None


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _tokens_dir(state_root: Path) -> Path:
    return Path(state_root) / "tokens"


def _token_path(state_root: Path, name: str) -> Path:
    # File name uses the full qualified name, with `:` replaced by `__`
    # so we don't choke on filesystems that disallow `:` (Windows).
    safe = name.replace(":", "__")
    return _tokens_dir(state_root) / safe


def _audit_path(state_root: Path) -> Path:
    return Path(state_root) / "tokens.audit.jsonl"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _maybe_load_operator_config(state_root: Path) -> None:
    """Read ``<state>/tokens.json`` (if present) and register any tokens
    declared there. Re-reads on mtime change."""
    cfg = Path(state_root) / "tokens.json"
    if not cfg.is_file():
        return
    try:
        mtime = cfg.stat().st_mtime
    except OSError:
        return
    if _OPERATOR_CONFIG_LOADED.get(cfg) == mtime:
        return
    try:
        data = json.loads(cfg.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    with _REGISTRY_LOCK:
        for name, body in data.items():
            try:
                _validate_name(name)
            except InvalidTokenName:
                continue
            entry = body if isinstance(body, dict) else {}
            _REGISTRY[name] = TokenSpec(
                name=name,
                description=str(entry.get("description") or ""),
                one_time=bool(entry.get("one_time", False)),
                source="operator",
            )
        _OPERATOR_CONFIG_LOADED[cfg] = mtime


# ---------------------------------------------------------------------------
# Public registry API
# ---------------------------------------------------------------------------

def register_token(
    name: str,
    *,
    description: str = "",
    one_time: bool = False,
    source: str = "programmatic",
) -> TokenSpec:
    """Register (or update) a token in the registry.

    Idempotent for the same name: the most recent registration wins.
    Validation only enforces the name's shape grammar — the registry
    is a documentation surface, not an allowlist.
    """
    _populate_builtins()
    name = _validate_name(name)
    with _REGISTRY_LOCK:
        spec = TokenSpec(
            name=name, description=description,
            one_time=one_time, source=source,
        )
        _REGISTRY[name] = spec
        return spec


def known_tokens(state_root: Path | None = None) -> dict[str, TokenSpec]:
    """Return the full registry as a mapping. Loads operator config
    if ``state_root`` is given."""
    _populate_builtins()
    if state_root is not None:
        _maybe_load_operator_config(Path(state_root))
    with _REGISTRY_LOCK:
        return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# Persistence + audit
# ---------------------------------------------------------------------------

@dataclass
class TokenRecord:
    """On-disk shape of a token file. Round-trips via ``to_dict`` /
    ``from_dict``."""
    name: str
    set_at: str
    set_by: str = ""
    reason: str = ""
    expires_at: str | None = None
    one_time: bool = False
    target: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "TokenRecord":
        return cls(
            name=name,
            set_at=str(d.get("set_at") or ""),
            set_by=str(d.get("set_by") or ""),
            reason=str(d.get("reason") or ""),
            expires_at=d.get("expires_at"),
            one_time=bool(d.get("one_time", False)),
            target=d.get("target"),
        )

    def is_expired(self, *, now: float | None = None) -> bool:
        if not self.expires_at:
            return False
        try:
            ts = datetime.strptime(
                self.expires_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return False
        return (now or time.time()) > ts


def _audit(state_root: Path, kind: str, body: dict) -> None:
    """Write one event to the tokens audit chain."""
    try:
        raudit.append(_audit_path(state_root), body, kind=kind)
    except Exception:  # noqa: BLE001
        # Audit is best-effort — never fail a token op because of disk error.
        pass


# ---------------------------------------------------------------------------
# Public token API
# ---------------------------------------------------------------------------

def set_token(
    name: str,
    *,
    reason: str = "",
    set_by: str = "",
    expires_at: str | None = None,
    one_time: bool | None = None,
    state_root: Path | None = None,
) -> Path:
    """Set a token. Returns the path written.

    ``one_time=None`` (default) inherits from the registry entry; pass
    ``True`` / ``False`` to override per-call.
    """
    _populate_builtins()
    name = _validate_name(name)
    if state_root is None:
        state_root = Path.cwd()
    state_root = Path(state_root)

    base, target = _split_qualified(name)
    spec = known_tokens(state_root).get(base) or TokenSpec(name=base)
    if one_time is None:
        one_time = spec.one_time

    rec = TokenRecord(
        name=name,
        set_at=_utc_now(),
        set_by=set_by,
        reason=reason,
        expires_at=expires_at,
        one_time=one_time,
        target=target,
    )
    p = _token_path(state_root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(rec.to_dict(), sort_keys=True, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    _audit(state_root, "token_set", {
        "token": name, "reason": reason,
        "set_by": set_by, "expires_at": expires_at,
        "one_time": one_time, "target": target,
    })
    return p


def unset_token(
    name: str, *,
    reason: str = "",
    state_root: Path | None = None,
) -> bool:
    """Remove a token. Returns True if a token file existed."""
    name = _validate_name(name)
    if state_root is None:
        state_root = Path.cwd()
    p = _token_path(Path(state_root), name)
    if not p.exists():
        return False
    try:
        p.unlink()
    except OSError:
        return False
    _audit(Path(state_root), "token_unset", {
        "token": name, "reason": reason,
    })
    return True


def has_token(
    name: str, *,
    state_root: Path | None = None,
    now: float | None = None,
) -> bool:
    """Check token presence without consuming. Returns False on
    expired tokens (and silently unsets them)."""
    name = _validate_name(name)
    if state_root is None:
        state_root = Path.cwd()
    state_root = Path(state_root)
    p = _token_path(state_root, name)
    if not p.exists():
        return False
    try:
        rec = TokenRecord.from_dict(name, json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError):
        return False
    if rec.is_expired(now=now):
        # Auto-cleanup expired tokens.
        try:
            p.unlink()
        except OSError:
            pass
        _audit(state_root, "token_expired", {"token": name})
        return False
    return True


def require_token(
    name: str, *,
    state_root: Path | None = None,
    now: float | None = None,
) -> bool:
    """Check token presence; consume if one-time. Returns True iff the
    token was present (and if one-time, has now been consumed)."""
    name = _validate_name(name)
    if state_root is None:
        state_root = Path.cwd()
    state_root = Path(state_root)
    if not has_token(name, state_root=state_root, now=now):
        _audit(state_root, "token_required", {
            "token": name, "consumed": False, "present": False,
        })
        return False
    p = _token_path(state_root, name)
    try:
        rec = TokenRecord.from_dict(name, json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError):
        return False
    consumed = False
    if rec.one_time:
        try:
            p.unlink()
            consumed = True
        except OSError:
            pass
    _audit(state_root, "token_required", {
        "token": name, "consumed": consumed, "present": True,
    })
    return True


def list_tokens(state_root: Path | None = None) -> dict[str, bool]:
    """Return a mapping of every registered token (built-in + operator
    + programmatic) to its current set/unset state under ``state_root``.

    Targeted variants of registered tokens that exist on disk are
    included with their full qualified names.
    """
    _populate_builtins()
    if state_root is None:
        state_root = Path.cwd()
    state_root = Path(state_root)
    _maybe_load_operator_config(state_root)

    out: dict[str, bool] = {name: False for name in _REGISTRY}
    tdir = _tokens_dir(state_root)
    if tdir.is_dir():
        for f in tdir.iterdir():
            if not f.is_file():
                continue
            qname = f.name.replace("__", ":")
            try:
                _validate_name(qname)
            except InvalidTokenName:
                continue
            out[qname] = True
    return out


def issue_one_time_token(
    base: str, *,
    target: str | None = None,
    reason: str = "",
    set_by: str = "",
    state_root: Path | None = None,
) -> Path:
    """Convenience: set a one-time token. ``target`` extends the name
    via the qualified-name colon convention."""
    name = f"{base}:{target}" if target else base
    return set_token(
        name, reason=reason, set_by=set_by,
        one_time=True, state_root=state_root,
    )


# ---------------------------------------------------------------------------
# Public utility
# ---------------------------------------------------------------------------

def token_path(name: str, state_root: Path | None = None) -> Path:
    """Resolve the on-disk path for a token. Useful for tests + UIs."""
    name = _validate_name(name)
    if state_root is None:
        state_root = Path.cwd()
    return _token_path(Path(state_root), name)


def _reset_for_tests() -> None:
    """Clear the in-memory registry + operator-config cache. Tests only."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _OPERATOR_CONFIG_LOADED.clear()


__all__ = [
    "BUILTIN_TOKENS",
    "TokenSpec",
    "TokenRecord",
    "InvalidTokenName",
    "register_token",
    "known_tokens",
    "set_token",
    "unset_token",
    "has_token",
    "require_token",
    "list_tokens",
    "issue_one_time_token",
    "token_path",
]
