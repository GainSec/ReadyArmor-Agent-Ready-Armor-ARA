"""Minimal in-tree replacement for the deleted ``ara.rhema`` module.

Keeps the surface area that ``mitm_proxy`` + ``supervisor`` still depend
on, without the policy-layer storage (which Bulwark now owns):

  * ``evaluate(state_root, scope, payload)`` → always ``None``
    (no rhema rules exist post-migration — Bulwark ask/reply/oc layers
    replace them).
  * ``load(state_root)`` → empty list.
  * ``save(state_root, rules)`` → no-op.
  * ``purge_managed(state_root, key)`` → no-op.
  * ``synthesize_deny_sse(rule, provider)`` → delegates to
    ``sse_synth.synthesize_deny``.
  * ``active_absolute_rule(state_root, channel_id=None)`` → real
    implementation: reads the equipped armor's ABSOLUTE.md and
    synthesises a shadow_rewrite-style rule record.
  * ``apply_rewrite_spec(body, spec)`` → real implementation.
  * ``new_pair_id()`` → trivial uuid helper.
  * ``RhemaRule`` dataclass → minimal record (no storage semantics).

Nothing in this module should grow. If a caller needs a new rhema-
flavoured feature, add it to Bulwark instead.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class RhemaRule:
    """Ephemeral rule record used by the ABSOLUTE live projection path.
    Not persisted. Bulwark is the durable policy store."""
    id: str
    scope: str                   # "request" | "response" | "tool_call" | "any"
    pattern: str
    pattern_kind: str = "substring"  # "substring" | "regex"
    action: str = "rewrite"      # "deny" | "rewrite" | "shadow_rewrite" | "approval"
    redirect: str = ""
    rewrite_with: Optional[dict] = None
    reason: str = ""
    managed_key: str = ""
    fire_count: int = 0
    timeout_s: Optional[float] = None


# ---- no-op policy-layer functions -----------------------------------

def load(state_root: Path) -> list[RhemaRule]:  # pragma: no cover
    return []


def save(state_root: Path, rules: list[RhemaRule]) -> None:  # pragma: no cover
    pass


def purge_managed(state_root: Path, managed_key: str) -> int:  # pragma: no cover
    return 0


def evaluate(state_root: Path, scope: str, payload: Any) -> Optional[RhemaRule]:
    """No Rhema rules exist post-migration. Bulwark is the rule source
    for ask/reply/oc layers — MITM consults it via
    ``_try_enforce_bulwark_request`` / ``_try_enforce_bulwark_response``."""
    return None


def new_pair_id() -> str:
    return uuid.uuid4().hex[:12]


# ---- real helpers ----------------------------------------------------

def synthesize_deny_sse(rule: RhemaRule, provider: str) -> bytes:
    """Thin forwarder to ``sse_synth``. Kept on this shim so legacy
    callers in ``mitm_proxy`` don't need to rewrite import statements."""
    from . import sse_synth as _sse
    text = rule.redirect or rule.reason or f"ARA policy (rule {rule.id}) blocked."
    return _sse.synthesize_deny(
        _sse.DenyInfo(rule_id=rule.id, text=text),
        provider,
    )


def synthesize_deny_response(rule: RhemaRule) -> dict:
    """Tier-A transcript-channel deny payload. Returned to the worker
    via the ARA_SOCKET intercept protocol; the agent sees it as a
    synthetic assistant turn."""
    text = rule.redirect or rule.reason or f"ARA policy (rule {rule.id}) blocked."
    return {
        "role": "assistant",
        "content": text,
        "ara_synthesized": True,
        "rule_id": rule.id,
    }


def _extract_named_sections(text: str, titles: list) -> str:
    """Pull the named markdown ``#``/``##`` sections out of ``text``.

    A section is its header line plus everything up to the next header. Matched
    case-insensitively by substring of the header title. Used to retain a few
    operator-selected default-Hermes prompt sections when the armor absolute
    otherwise replaces the system prompt (D7 composable system prompt)."""
    wanted = [str(t).strip().lower() for t in (titles or []) if str(t).strip()]
    if not wanted or not text:
        return ""
    blocks: list[tuple[str, list]] = []
    header: str | None = None
    body: list = []
    for line in str(text).split("\n"):
        if line.lstrip().startswith("#"):
            if header is not None:
                blocks.append((header, body))
            header, body = line, []
        elif header is not None:
            body.append(line)
    if header is not None:
        blocks.append((header, body))
    kept: list[str] = []
    for hdr, lines in blocks:
        title = hdr.lstrip("# ").strip().lower()
        if any(w in title for w in wanted):
            kept.append((hdr + "\n" + "\n".join(lines)).rstrip())
    return "\n\n".join(k for k in kept if k.strip())


def _compose_envelope(original: object, text: str, keep_titles: list) -> str:
    """Compose the system prompt: operator-kept default sections + absolute."""
    kept = _extract_named_sections(str(original or ""), keep_titles)
    if not kept:
        return text
    return kept.rstrip() + "\n\n" + text


def _read_keep_default_sections(armor: object) -> list:
    """D7 opt-in: section titles the operator wants kept from the default
    Hermes prompt when this armor's absolute replaces the system prompt.

    Read from ``<armor_root>/keep-default-sections`` (one title per line; ``#``
    comments ignored). Absent → empty → absolute fully replaces (unchanged)."""
    try:
        root = getattr(armor, "root", None)
        if root is None:
            return []
        path = Path(root) / "keep-default-sections"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
        return out
    except Exception:  # noqa: BLE001
        return []


def apply_rewrite_spec(body: dict, spec: dict) -> dict:
    """Apply a rewrite spec to a request body. Returns a new dict."""
    if not isinstance(body, dict) or not isinstance(spec, dict):
        return body if isinstance(body, dict) else {}
    op = spec.get("op")
    if op == "apply_envelope":
        text = str(spec.get("text") or "")
        # D7: when the armor opts to keep specific default-prompt sections,
        # compose them in. Absent (the default) → behaviour is unchanged: the
        # absolute fully replaces the system prompt.
        keep = spec.get("keep_default_sections") or []
        # ``append`` mode preserves the WHOLE original prompt and appends the
        # text (additive temper doctrine with no armor to replace the prompt).
        append = bool(spec.get("append"))

        def _compose(original: object) -> str:
            if append:
                base = str(original or "").rstrip()
                return (base + "\n\n" + text) if base else text
            return _compose_envelope(original, text, keep)

        out = dict(body)
        if "instructions" in out:
            out["instructions"] = _compose(out.get("instructions"))
            return out
        messages = out.get("messages")
        if not isinstance(messages, list):
            return out
        new_messages = [dict(m) if isinstance(m, dict) else m for m in messages]
        for idx, msg in enumerate(new_messages):
            if not isinstance(msg, dict) or msg.get("role") != "system":
                continue
            msg["content"] = _compose(msg.get("content"))
            new_messages[idx] = msg
            out["messages"] = new_messages
            return out
        out["messages"] = [{"role": "system", "content": text}] + new_messages
        return out
    if op == "pipeline":
        ops = spec.get("ops") or []
        if not isinstance(ops, list):
            return dict(body)
        current_body = dict(body)
        for inner in ops:
            if isinstance(inner, dict):
                current_body = apply_rewrite_spec(current_body, inner)
        return current_body
    out = dict(body)
    field_name = spec.get("field")
    if not field_name or not op:
        return out
    if op == "remove_field":
        out.pop(field_name, None)
        return out
    if field_name not in out:
        return out
    current = out[field_name]
    if op == "replace":
        out[field_name] = spec.get("text", "")
    elif op == "regex_sub" and isinstance(current, str):
        pattern = spec.get("pattern", "")
        replacement = spec.get("replacement", "")
        try:
            flags = re.DOTALL if spec.get("dotall") else 0
            out[field_name] = re.sub(pattern, replacement, current, flags=flags)
        except re.error:
            pass
    elif op == "trim_to_chars" and isinstance(current, str):
        try:
            n = int(spec.get("chars", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            out[field_name] = current[:n]
    return out


def active_absolute_rule(
    state_root: Path, channel_id: Optional[str] = None,
) -> Optional[RhemaRule]:
    """Build an ephemeral rule that composes the live system-prompt envelope:
    the equipped armor's active ABSOLUTE.md PLUS any equipped tempers' own
    absolute overlays. Never persisted — read per-request so edits take effect
    on the very next turn.

    Composition:
      * armor present  → armor absolute (which replaces the prompt) with the
        equipped-temper doctrine appended, kept-default sections honoured.
      * no armor but tempers equipped → temper doctrine is APPENDED to the
        original prompt (additive hardening; it must not nuke the base prompt).
    """
    try:
        from . import armors as _armors
    except Exception:  # noqa: BLE001
        _armors = None

    # --- armor absolute text (may be absent) ---------------------------------
    armor_text = None
    armor_name = None
    active_name = None
    target = None
    enforcing = True
    keep_default: list = []
    source_kind = "armor_absolute"
    task_id = str(os.environ.get("ARA_TASK_RUN_ID") or "").strip()
    if _armors is not None:
        name = _armors.active_armor_name(state_root)
        armor = _armors.load_armor(state_root, name) if name else None
        if (
            armor is not None
            and _armors.armor_applies_to_channel(armor.meta, channel_id)
        ):
            target = armor.absolute_path
            task_absolute = str(os.environ.get("ARA_TASK_ABSOLUTE_PATH") or "").strip()
            if task_id:
                if task_absolute:
                    projected = Path(task_absolute)
                    if projected.is_file():
                        target = projected
                        source_kind = "task_temper_projection"
                if source_kind == "armor_absolute":
                    try:
                        from . import temper as _temper
                        projected = _temper.task_absolute_projection_path(state_root, task_id)
                        if projected.is_file():
                            target = projected
                            source_kind = "task_temper_projection"
                    except Exception:  # noqa: BLE001
                        pass
            if target and target.exists():
                try:
                    t = target.read_text().strip()
                except OSError:
                    t = ""
                if t:
                    armor_text = t
                    armor_name = name
                    enforcing = bool(armor.meta.absolute_enforce)
                    try:
                        active_name = _armors.active_absolute_name(armor)
                    except Exception:  # noqa: BLE001
                        active_name = target.stem
                    keep_default = _read_keep_default_sections(armor)

    # --- equipped-temper doctrine overlays (global, armor-independent) --------
    temper_text, temper_ids = _active_temper_absolute_overlay(state_root)

    parts = [p for p in (armor_text, temper_text) if p]
    if not parts:
        return None
    text = "\n\n".join(parts)
    # No armor to replace the prompt → append temper doctrine to the original
    # rather than replacing it.
    append = armor_text is None

    return RhemaRule(
        id=f"abs-live-{armor_name}" if armor_name else "abs-live-tempers",
        scope="request",
        pattern="codex/responses",
        pattern_kind="substring",
        action="rewrite" if enforcing else "shadow_rewrite",
        redirect="",
        rewrite_with={
            "op": "apply_envelope",
            "text": text,
            "append": append,
            "armor": armor_name,
            "absolute_name": active_name or (target.stem if target else None),
            "source_path": str(target) if target else None,
            "source_kind": source_kind,
            "task_id": task_id or None,
            "text_chars": len(text),
            "text_bytes": len(text.encode("utf-8")),
            "enforcing": enforcing,
            "keep_default_sections": keep_default,
            "tempers": temper_ids,
        },
        reason=(
            f"ABSOLUTE.md live projection (armor={armor_name}, "
            f"tempers={temper_ids or 'none'}, {len(text):,} chars, "
            f"{'ENFORCING' if enforcing else 'shadow'})"
        ),
        managed_key="abs-live",
    )


def _active_temper_absolute_overlay(state_root) -> tuple:
    """Compose equipped tempers' own ``policy/absolute.md`` overlays into one
    doctrine block. Global — applies regardless of armor. Returns
    ``(text, [temper_ids])``; ``("", [])`` when none are equipped."""
    try:
        from . import tempers as _tempers
    except Exception:  # noqa: BLE001
        return "", []
    try:
        ids = _tempers.active_tempers(state_root)
    except Exception:  # noqa: BLE001
        return "", []
    chunks: list = []
    used: list = []
    for tid in ids:
        try:
            p = _tempers.absolute_policy_path(state_root, tid)
            if p.is_file():
                body = p.read_text().strip()
                if body:
                    chunks.append(body)
                    used.append(tid)
        except OSError:
            continue
    return ("\n\n".join(chunks), used)


# ---- remove / upsert / add compat -----------------------------------
# Callers that wrote to rhema.json are no-ops now — rules live in
# bulwark.md. Returning dummy records keeps call sites happy.

def new_id() -> str:
    return uuid.uuid4().hex[:10]


def add(state_root: Path, **kw: Any) -> RhemaRule:  # pragma: no cover
    return RhemaRule(
        id=kw.get("id") or new_id(),
        scope=kw.get("scope") or "any",
        pattern=kw.get("pattern") or "",
        pattern_kind=kw.get("pattern_kind") or "substring",
        action=kw.get("action") or "deny",
        redirect=kw.get("redirect") or "",
        rewrite_with=kw.get("rewrite_with"),
        reason=kw.get("reason") or "",
    )


def remove(state_root: Path, rule_id: str) -> bool:  # pragma: no cover
    return False


def upsert_managed(
    state_root: Path, managed_key: str, **kw: Any,
) -> RhemaRule:  # pragma: no cover
    return add(state_root, **kw)


def remove_managed(state_root: Path, managed_key: str) -> int:  # pragma: no cover
    return 0


def path_for(state_root: Path) -> Path:
    return state_root / "rhema.json"
