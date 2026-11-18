"""Tool classification registry — pause-and-ask classifier.

Per spec ``armor-trim-v0.1-amendment.md`` §11. No baseline list ships
with ARA; tool classifications are built up organically by the agent
on first use, with operator review.

OS-agnostic by design — the registry keys on ``(tool, flag_pattern)``,
not absolute path. Same nmap on Linux / macOS / Windows shares its
classification.

This module owns:

- Registry storage (global at ``<state>/tool_registry.json``;
  weapon-local at ``<weapon>/tool_classifications.json``).
- Classification-gate primitives: ``key_for(...)``, ``lookup(...)``,
  ``record_classification(...)``, ``apply_operator_action(...)``.
- The pause-and-ask **prompt builder** + **response parser** — the
  actual injection into the agent's conversation is the supervisor's
  job (Step 5); this module only provides pure helpers so the
  supervisor stays a thin shim.

Status lifecycle::

    pending_operator_first_use   ← 1/3 confidence; held until operator OKs
    pending_operator_review      ← 2/3 or 3/3; auto-allowed, TTL'd review
    confirmed                    ← operator approved
    operator_overridden          ← operator edited or denied
    merged_by_ttl                ← TTL expired without operator action

Conflict resolution between the global + weapon-local copies is the
operator's job via the Tools UI (Step 6); this module surfaces the
conflict, never auto-resolves.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY_FILENAME = "tool_registry.json"

STATUS_PENDING_FIRST_USE = "pending_operator_first_use"
STATUS_PENDING_REVIEW = "pending_operator_review"
STATUS_CONFIRMED = "confirmed"
STATUS_OPERATOR_OVERRIDDEN = "operator_overridden"
STATUS_MERGED_BY_TTL = "merged_by_ttl"

VALID_STATUSES = frozenset({
    STATUS_PENDING_FIRST_USE,
    STATUS_PENDING_REVIEW,
    STATUS_CONFIRMED,
    STATUS_OPERATOR_OVERRIDDEN,
    STATUS_MERGED_BY_TTL,
})

# Default TTL for `pending_operator_review` entries when the engagement
# has no `test_window.end`. 24h matches the spec default.
DEFAULT_REVIEW_TTL_SECONDS = 24 * 3600

# Tool-name validator. Conservative — POSIX names + dots / hyphens /
# underscores. Quoted/escaped tool paths get unwrapped to the basename
# before lookup so these stay clean.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


class InvalidToolName(ValueError):
    pass


class InvalidStatus(ValueError):
    pass


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    """Multi-flag classification per spec §11.1."""
    active: bool = False
    destructive: bool = False
    intrusive: bool = False
    scanner: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Classification":
        return cls(
            active=bool(d.get("active", False)),
            destructive=bool(d.get("destructive", False)),
            intrusive=bool(d.get("intrusive", False)),
            scanner=bool(d.get("scanner", False)),
        )


@dataclass
class OperatorOverride:
    classification: Classification
    reason: str
    ts: str

    def to_dict(self) -> dict:
        return {
            "classification": self.classification.to_dict(),
            "reason": self.reason,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OperatorOverride":
        return cls(
            classification=Classification.from_dict(d.get("classification") or {}),
            reason=d.get("reason") or "",
            ts=d.get("ts") or "",
        )


@dataclass
class ToolEntry:
    key: str                                  # "<tool> | <flag_pattern>"
    classification: Classification
    confidence: int                           # 1 | 2 | 3
    reasoning: str
    classified_by: str                        # "agent" | "operator"
    classified_at: str                        # ISO-8601 UTC
    status: str
    ttl_expires_at: str | None = None
    first_seen_engagement: str | None = None
    first_seen_weapon: str | None = None
    first_seen_crumb: str | None = None
    operator_override: OperatorOverride | None = None
    reclassify_on_next_call: bool = False
    use_count: int = 0
    engagements_used: list[str] = field(default_factory=list)

    @property
    def tool(self) -> str:
        return self.key.split(" | ", 1)[0]

    @property
    def flag_pattern(self) -> str:
        parts = self.key.split(" | ", 1)
        return parts[1] if len(parts) == 2 else ""

    @property
    def effective_classification(self) -> Classification:
        """If the operator overrode, that's the truth at runtime."""
        if self.operator_override is not None:
            return self.operator_override.classification
        return self.classification

    def to_dict(self) -> dict:
        d = asdict(self)
        d["operator_override"] = (
            self.operator_override.to_dict()
            if self.operator_override is not None else None
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ToolEntry":
        ov = d.get("operator_override")
        return cls(
            key=d["key"],
            classification=Classification.from_dict(d.get("classification") or {}),
            confidence=int(d.get("confidence") or 1),
            reasoning=d.get("reasoning") or "",
            classified_by=d.get("classified_by") or "agent",
            classified_at=d.get("classified_at") or "",
            status=_validated_status(d.get("status") or STATUS_PENDING_REVIEW),
            ttl_expires_at=d.get("ttl_expires_at"),
            first_seen_engagement=d.get("first_seen_engagement"),
            first_seen_weapon=d.get("first_seen_weapon"),
            first_seen_crumb=d.get("first_seen_crumb"),
            operator_override=OperatorOverride.from_dict(ov) if ov else None,
            reclassify_on_next_call=bool(d.get("reclassify_on_next_call", False)),
            use_count=int(d.get("use_count") or 0),
            engagements_used=list(d.get("engagements_used") or []),
        )


def _validated_status(s: str) -> str:
    if s not in VALID_STATUSES:
        raise InvalidStatus(f"unknown tool-registry status: {s!r}")
    return s


# ---------------------------------------------------------------------------
# Key shape
# ---------------------------------------------------------------------------

# Flag-pattern derivation strategy: take the *significant* short/long
# flags from the args, drop positional values, drop targets. Conservative
# — too-coarse keys merge things that should be separate (nmap -sV vs
# nmap -sP); too-fine keys explode the registry. Strategy: keep flag
# tokens (-sV, --script, etc.) *and* attach a glob `*` at the end so
# multiple targets share a key.
#
# `--key=value` and `-kvalue` shapes preserve the key but glob the
# value — ``sqlmap --risk=3`` and ``sqlmap --risk=1`` need separate
# keys because they classify differently (risk=3 enables destructive
# payloads), but ``--risk=3`` and ``--risk=2`` shouldn't fragment the
# registry needlessly. Compromise: keep the level digit when the value
# is short + numeric (the case where it changes classification);
# glob otherwise.
#
# Examples:
#   ["nmap", "-sV", "-p", "80,443", "portal.acme.com"] → "nmap | -sV -p*"
#   ["sqlmap", "-u", "https://x", "--batch", "--risk=3"]
#                                              → "sqlmap | -u --batch --risk=3*"
#   ["sqlmap", "-u", "https://x", "--tamper=between"]
#                                              → "sqlmap | -u --tamper=*"
#   ["nuclei", "-t", "tpls/cves/", "-target", "x"] → "nuclei | -t -target*"

_FLAG_BARE_RE = re.compile(r"^-+[A-Za-z0-9][A-Za-z0-9-]*$")
# `--flag=value` or `-fvalue` — the value bit may matter for classification.
_FLAG_KV_RE = re.compile(r"^(-+[A-Za-z0-9][A-Za-z0-9-]*)=(.+)$")
# Short numeric values (1-2 digits) get preserved literally; everything
# else is globbed to keep the registry from exploding on each new target.
_SHORT_NUMERIC = re.compile(r"^\d{1,2}$")


def _normalise_flag(token: str) -> str | None:
    if _FLAG_BARE_RE.match(token):
        return token
    m = _FLAG_KV_RE.match(token)
    if m:
        flag, value = m.group(1), m.group(2)
        if _SHORT_NUMERIC.match(value):
            return f"{flag}={value}"
        return f"{flag}=*"
    return None


def derive_flag_pattern(args: list[str]) -> str:
    """Reduce an arg list to a glob-shaped flag pattern. Pure function."""
    flags: list[str] = []
    for a in args:
        norm = _normalise_flag(a)
        if norm:
            flags.append(norm)
    if not flags:
        return "*"
    return " ".join(flags) + "*"


# Back-compat alias used by tests + other callsites.
_FLAG_RE = _FLAG_BARE_RE


def _validate_tool_name(tool: str) -> str:
    if not isinstance(tool, str) or not _TOOL_NAME_RE.match(tool):
        raise InvalidToolName(
            f"tool name must match {_TOOL_NAME_RE.pattern!r}, got {tool!r}"
        )
    return tool


def key_for(tool: str, args: list[str]) -> str:
    """Produce the registry key for a `(tool, args)` pair."""
    # Normalise tool to its basename — operators may pass `/usr/bin/nmap`,
    # `/opt/homebrew/bin/nmap`, or `nmap.exe` and they all share a key.
    base = Path(tool).name
    if base.endswith(".exe"):
        base = base[:-4]
    _validate_tool_name(base)
    return f"{base} | {derive_flag_pattern(args)}"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def registry_path(state_root: Path) -> Path:
    return Path(state_root) / REGISTRY_FILENAME


def weapon_classifications_path(weapon_dir: Path) -> Path:
    return Path(weapon_dir) / "tool_classifications.json"


# ---------------------------------------------------------------------------
# Persistence (registry + lock)
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def _load_registry_dict(p: Path) -> dict[str, dict]:
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict) and "entries" in data:
        return {row["key"]: row for row in data["entries"]}
    return {}


def _save_registry_dict(p: Path, by_key: dict[str, dict]) -> None:
    rows = sorted(by_key.values(), key=lambda r: r.get("key") or "")
    payload = {
        "schema_version": "0.1",
        "updated_at": _now_iso(),
        "entries": rows,
    }
    _atomic_write_text(p, json.dumps(payload, indent=2, sort_keys=True))


def list_entries(state_root: Path) -> list[ToolEntry]:
    with _LOCK:
        by_key = _load_registry_dict(registry_path(state_root))
        return [ToolEntry.from_dict(d) for d in by_key.values()]


def lookup(state_root: Path, key: str) -> ToolEntry | None:
    with _LOCK:
        by_key = _load_registry_dict(registry_path(state_root))
        d = by_key.get(key)
        return ToolEntry.from_dict(d) if d else None


def save_entry(state_root: Path, entry: ToolEntry) -> None:
    with _LOCK:
        p = registry_path(state_root)
        by_key = _load_registry_dict(p)
        by_key[entry.key] = entry.to_dict()
        _save_registry_dict(p, by_key)


def delete_entry(state_root: Path, key: str) -> bool:
    with _LOCK:
        p = registry_path(state_root)
        by_key = _load_registry_dict(p)
        if key not in by_key:
            return False
        del by_key[key]
        _save_registry_dict(p, by_key)
        return True


# ---------------------------------------------------------------------------
# Confidence-tiered classification recording
# ---------------------------------------------------------------------------

def _initial_status_for(confidence: int, paranoid: bool) -> str:
    """Per spec §11.4 + §11.5."""
    if paranoid:
        return STATUS_PENDING_FIRST_USE
    if confidence >= 2:
        return STATUS_PENDING_REVIEW
    return STATUS_PENDING_FIRST_USE


def _ttl_expires_at(
    *, test_window_end: str | None, now: float | None = None,
) -> str:
    """Earliest of (now + 24h) and test_window_end. Returns ISO-8601 UTC."""
    n = datetime.fromtimestamp(
        now if now is not None else datetime.now(timezone.utc).timestamp(),
        tz=timezone.utc,
    )
    default_expiry = n.timestamp() + DEFAULT_REVIEW_TTL_SECONDS
    if test_window_end:
        try:
            wend = datetime.strptime(
                test_window_end, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc)
            if wend.timestamp() < default_expiry:
                return wend.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return datetime.fromtimestamp(
        default_expiry, tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_classification(
    state_root: Path, *,
    key: str,
    classification: Classification,
    confidence: int,
    reasoning: str,
    classified_by: str = "agent",
    paranoid: bool = False,
    test_window_end: str | None = None,
    engagement_id: str | None = None,
    weapon: str | None = None,
    crumb: str | None = None,
    now: float | None = None,
) -> ToolEntry:
    """Write a fresh classification or update one flagged for
    reclassification. Returns the persisted entry.

    Per spec §11.4: 3/3 or 2/3 → ``pending_operator_review`` with TTL;
    1/3 → ``pending_operator_first_use`` (block until operator OKs).
    Paranoid mode (§11.5) forces ``pending_operator_first_use``
    regardless of confidence.
    """
    if confidence not in (1, 2, 3):
        raise ValueError(f"confidence must be 1, 2, or 3; got {confidence!r}")

    ts = _now_iso() if now is None else datetime.fromtimestamp(
        now, tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    status = _initial_status_for(confidence, paranoid)
    ttl: str | None = None
    if status == STATUS_PENDING_REVIEW:
        ttl = _ttl_expires_at(test_window_end=test_window_end, now=now)

    with _LOCK:
        existing = lookup(state_root, key)
        if existing is None:
            entry = ToolEntry(
                key=key,
                classification=classification,
                confidence=confidence,
                reasoning=reasoning,
                classified_by=classified_by,
                classified_at=ts,
                status=status,
                ttl_expires_at=ttl,
                first_seen_engagement=engagement_id,
                first_seen_weapon=weapon,
                first_seen_crumb=crumb,
            )
        else:
            # Reclassification path — preserve first-seen + use_count.
            entry = existing
            entry.classification = classification
            entry.confidence = confidence
            entry.reasoning = reasoning
            entry.classified_by = classified_by
            entry.classified_at = ts
            entry.status = status
            entry.ttl_expires_at = ttl
            entry.reclassify_on_next_call = False
            entry.operator_override = None    # operator must re-decide
        save_entry(state_root, entry)
        return entry


def record_use(
    state_root: Path, *, key: str, engagement_id: str,
) -> ToolEntry | None:
    """Bump ``use_count`` + record this engagement_id. No-op if entry
    doesn't exist (caller should classify first)."""
    with _LOCK:
        entry = lookup(state_root, key)
        if entry is None:
            return None
        entry.use_count += 1
        if engagement_id and engagement_id not in entry.engagements_used:
            entry.engagements_used.append(engagement_id)
        save_entry(state_root, entry)
        return entry


# ---------------------------------------------------------------------------
# Operator actions (UI + CLI)
# ---------------------------------------------------------------------------

def operator_approve(state_root: Path, key: str) -> ToolEntry | None:
    with _LOCK:
        entry = lookup(state_root, key)
        if entry is None:
            return None
        entry.status = STATUS_CONFIRMED
        entry.ttl_expires_at = None
        save_entry(state_root, entry)
        return entry


def operator_override(
    state_root: Path, key: str, *,
    classification: Classification, reason: str,
) -> ToolEntry | None:
    with _LOCK:
        entry = lookup(state_root, key)
        if entry is None:
            return None
        entry.operator_override = OperatorOverride(
            classification=classification,
            reason=reason,
            ts=_now_iso(),
        )
        entry.status = STATUS_OPERATOR_OVERRIDDEN
        entry.ttl_expires_at = None
        save_entry(state_root, entry)
        return entry


def operator_request_reclassify(
    state_root: Path, key: str,
) -> ToolEntry | None:
    """Flag an entry to re-run classification on next call."""
    with _LOCK:
        entry = lookup(state_root, key)
        if entry is None:
            return None
        entry.reclassify_on_next_call = True
        save_entry(state_root, entry)
        return entry


def merge_expired_reviews(
    state_root: Path, *, now: float | None = None,
) -> list[ToolEntry]:
    """Sweep ``pending_operator_review`` entries whose TTL has passed
    and merge them as ``merged_by_ttl``. Auditably distinct from
    ``confirmed`` so the operator can see at a glance what slipped
    past review.
    """
    n = datetime.fromtimestamp(
        now if now is not None else datetime.now(timezone.utc).timestamp(),
        tz=timezone.utc,
    )
    merged: list[ToolEntry] = []
    with _LOCK:
        p = registry_path(state_root)
        by_key = _load_registry_dict(p)
        changed = False
        for k, raw in list(by_key.items()):
            entry = ToolEntry.from_dict(raw)
            if entry.status != STATUS_PENDING_REVIEW:
                continue
            if not entry.ttl_expires_at:
                continue
            try:
                expires = datetime.strptime(
                    entry.ttl_expires_at, "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if expires <= n:
                entry.status = STATUS_MERGED_BY_TTL
                entry.ttl_expires_at = None
                by_key[k] = entry.to_dict()
                merged.append(entry)
                changed = True
        if changed:
            _save_registry_dict(p, by_key)
    return merged


# ---------------------------------------------------------------------------
# Conflict detection (global vs weapon-local)
# ---------------------------------------------------------------------------

def load_weapon_classifications(weapon_dir: Path) -> dict[str, ToolEntry]:
    p = weapon_classifications_path(weapon_dir)
    out: dict[str, ToolEntry] = {}
    if not p.is_file():
        return out
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    rows = data.get("entries") if isinstance(data, dict) else data
    for row in rows or []:
        try:
            entry = ToolEntry.from_dict(row)
        except (KeyError, InvalidStatus):
            continue
        out[entry.key] = entry
    return out


def find_conflicts(
    state_root: Path, weapon_dir: Path,
) -> list[tuple[ToolEntry, ToolEntry]]:
    """Return (global, weapon) entry pairs whose classifications differ.

    Global wins at runtime per spec §11.2; this just surfaces the
    disagreement for the Tools UI's reconcile button.
    """
    weapon_by_key = load_weapon_classifications(weapon_dir)
    if not weapon_by_key:
        return []
    out: list[tuple[ToolEntry, ToolEntry]] = []
    for entry in list_entries(state_root):
        wpn = weapon_by_key.get(entry.key)
        if wpn is None:
            continue
        if entry.effective_classification != wpn.classification:
            out.append((entry, wpn))
    return out


# ---------------------------------------------------------------------------
# Pause-and-ask: prompt builder + response parser
# ---------------------------------------------------------------------------

def build_classification_prompt(
    *, tool: str, args: list[str],
    engagement_env: str = "nonprod",
    posture: str = "blackbox",
) -> str:
    """Pure helper. The supervisor injects this into the agent's
    conversation; this module never talks to the LLM directly.

    Sanitisation discipline (§11.8): only ``(tool, flag_pattern,
    engagement_metadata)`` is sent — never free-form target data,
    payloads, or tool output. We still echo the *flags* the agent
    intends to use because classification of `nmap -sV` differs from
    `nmap -sP`; the flags themselves are not target data.
    """
    base = Path(tool).name
    if base.endswith(".exe"):
        base = base[:-4]
    # The classifier needs to see `--key=value` shapes verbatim — the
    # value may flip classification (e.g. sqlmap --risk=3 is destructive).
    flag_tokens = [a for a in args if _normalise_flag(a)]
    flag_str = " ".join(flag_tokens) if flag_tokens else "(no flags)"
    return (
        f"[ARA] Before executing `{base} {flag_str}`, classify "
        f"the tool+flags. Engagement env: {engagement_env}. "
        f"Posture: {posture}.\n"
        "Return ONLY this JSON, nothing else:\n"
        "{\n"
        '  "active":      bool,    // sends crafted/probe requests\n'
        '  "destructive": bool,    // state-changing on target\n'
        '  "intrusive":   bool,    // crafted requests visible in target logs\n'
        '  "scanner":     bool,    // automated multi-target/multi-vector\n'
        '  "confidence":  1 | 2 | 3,\n'
        '  "reasoning":   "<one sentence>"\n'
        "}\n"
    )


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def parse_classification_response(text: str) -> tuple[Classification, int, str]:
    """Extract the first valid JSON classification object from the
    agent's reply. Tolerates fenced code blocks + leading/trailing
    prose. Raises ``ValueError`` if nothing parseable is present.
    """
    if not text or not isinstance(text, str):
        raise ValueError("empty classification response")

    # Strip common code-fence wrappers.
    candidates: list[str] = []
    stripped = text.strip()
    for fence in ("```json", "```"):
        if stripped.startswith(fence):
            inner = stripped[len(fence):]
            if inner.endswith("```"):
                inner = inner[:-3]
            candidates.append(inner.strip())
            break
    candidates.append(stripped)
    for m in _JSON_BLOCK_RE.finditer(text):
        candidates.append(m.group(0))

    last_err: Exception | None = None
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if not isinstance(obj, dict):
            continue
        if "active" not in obj or "confidence" not in obj:
            continue
        try:
            confidence = int(obj["confidence"])
        except (TypeError, ValueError):
            continue
        if confidence not in (1, 2, 3):
            continue
        cls = Classification(
            active=bool(obj.get("active", False)),
            destructive=bool(obj.get("destructive", False)),
            intrusive=bool(obj.get("intrusive", False)),
            scanner=bool(obj.get("scanner", False)),
        )
        reasoning = str(obj.get("reasoning") or "").strip()
        return cls, confidence, reasoning
    raise ValueError(
        f"could not parse classification JSON from response: {last_err}"
    )


# ---------------------------------------------------------------------------
# Policy verdict (composes registry + trim flags)
# ---------------------------------------------------------------------------

@dataclass
class PolicyContext:
    active_testing: bool
    env: str                               # "prod" | "nonprod" | "mixed"
    posture: str                           # "blackbox" | "graybox" | "whitebox"


@dataclass
class PolicyVerdict:
    allowed: bool
    reason: str
    requires_token: str | None = None      # one-time token to consume on call


def evaluate_policy(
    entry: ToolEntry, ctx: PolicyContext,
) -> PolicyVerdict:
    """Pure function. Apply the §11.7 composition rules:

    - active_testing=False + classified active=True → deny.
    - env=prod + classified destructive=True → require PROD-DESTRUCTIVE-APPROVAL.
    - else allow.
    """
    cls = entry.effective_classification
    if cls.active and not ctx.active_testing:
        return PolicyVerdict(
            allowed=False,
            reason=(
                f"tool `{entry.tool}` classified active=true but "
                "engagement active_testing=false"
            ),
        )
    if cls.destructive and ctx.env == "prod":
        return PolicyVerdict(
            allowed=True,
            reason=(
                f"tool `{entry.tool}` classified destructive=true on prod "
                "— requires PROD-DESTRUCTIVE-APPROVAL"
            ),
            requires_token="PROD-DESTRUCTIVE-APPROVAL",
        )
    return PolicyVerdict(
        allowed=True, reason="classification within engagement contract",
    )


__all__ = [
    "Classification", "OperatorOverride", "ToolEntry",
    "PolicyContext", "PolicyVerdict",
    "InvalidToolName", "InvalidStatus",
    "REGISTRY_FILENAME",
    "STATUS_PENDING_FIRST_USE", "STATUS_PENDING_REVIEW",
    "STATUS_CONFIRMED", "STATUS_OPERATOR_OVERRIDDEN", "STATUS_MERGED_BY_TTL",
    "build_classification_prompt", "parse_classification_response",
    "delete_entry", "derive_flag_pattern", "evaluate_policy",
    "find_conflicts", "key_for", "list_entries",
    "load_weapon_classifications", "lookup",
    "merge_expired_reviews", "operator_approve", "operator_override",
    "operator_request_reclassify",
    "record_classification", "record_use",
    "registry_path", "save_entry", "weapon_classifications_path",
]
