"""v0.2 pause-and-ask shared state.

A weapon-driven tool call hits the trim-gate; if the (tool, flag) is
unclassified, ``trim_gate.evaluate_call`` returns
``requires_classification=True``. The supervisor seeds a
:class:`PendingClassification` here; the next outbound LLM turn (seen by
either the supervisor's ``prompt_intercept`` IPC path or the MITM proxy
when the agent runtime doesn't emit those events) gets a system message
prepended asking the agent to classify the tool. The subsequent
response is parsed for the JSON reply, the registry is written, and the
pending entry is cleared.

The state is keyed by ``state_root`` so the supervisor and MITM proxy
(running in the same Python process per launch) share a single queue
without passing references around.

Weapon-context gate: injection only fires when there is both an active
armor and an applied trim under that armor. Forge phases run without an
applied trim, so they fall through cleanly even if a leftover pending
entry exists from a previous weapon run.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock


@dataclass
class PendingClassification:
    tool: str
    flag_pattern: str
    classification_key: str
    classification_prompt: str
    engagement_env: str = "nonprod"
    posture: str = "blackbox"
    asked: bool = False
    queued_at: float = 0.0


_lock = RLock()
_state: dict[Path, dict[str, PendingClassification]] = {}


def _store(state_root: Path) -> dict[str, PendingClassification]:
    sr = Path(state_root).resolve()
    with _lock:
        d = _state.get(sr)
        if d is None:
            d = {}
            _state[sr] = d
        return d


def queue(state_root: Path, p: PendingClassification) -> None:
    _store(state_root)[p.classification_key] = p


def get_all(state_root: Path) -> list[PendingClassification]:
    with _lock:
        return list(_store(state_root).values())


def pop(state_root: Path, key: str) -> None:
    _store(state_root).pop(key, None)


def has_asked_pending(state_root: Path) -> bool:
    """True if at least one pending entry has been injected (asked=True)
    and is awaiting a response. Used by the MITM proxy to decide whether
    to force the buffered-response path on the current turn.
    """
    return any(p.asked for p in get_all(state_root))


def reset_for_tests(state_root: Path | None = None) -> None:
    with _lock:
        if state_root is None:
            _state.clear()
        else:
            _state.pop(Path(state_root).resolve(), None)


def is_weapon_context(state_root: Path) -> bool:
    """True iff an armor is active with an applied trim. Weapon
    execution applies a trim; forge phases and bare operator sessions
    don't, so this distinguishes them without needing a separate flag.
    """
    try:
        from . import armors as armors_mod
        from . import trims as trims_mod
    except Exception:
        return False
    try:
        armor = armors_mod.active_armor_name(state_root)
        if not armor:
            return False
        return trims_mod.active_trim_id(state_root, armor) is not None
    except Exception:
        return False


def maybe_inject_classification_request(
    state_root: Path, payload: dict,
) -> dict | None:
    """Mutate ``payload`` to prepend a system-message asking for the
    JSON classification of the oldest pending tool. Returns the new
    payload (a shallow copy) or None if no injection happened.

    Only one classification is asked per turn; extra pending entries
    wait. Skips entirely outside weapon context.
    """
    if not is_weapon_context(state_root):
        return None
    pendings = get_all(state_root)
    if not pendings:
        return None
    pending = next((p for p in pendings if not p.asked), None)
    if pending is None:
        return None
    new_payload = inject_system_message(payload, pending.classification_prompt)
    if new_payload is None:
        return None
    pending.asked = True
    return new_payload


def maybe_consume_classification_response(
    state_root: Path, payload: dict,
) -> bool:
    """Look at an LLM response payload for a classification JSON reply.
    On success, write the registry and clear the pending entry; return
    True. Parse failures leave pending as-is and return False so the
    next turn re-asks.
    """
    if not is_weapon_context(state_root):
        return False
    asked = [p for p in get_all(state_root) if p.asked]
    if not asked:
        return False
    text = extract_response_text(payload)
    if not text:
        return False
    try:
        from . import tool_registry as _tr
    except Exception:
        return False
    try:
        classification, confidence, reasoning = (
            _tr.parse_classification_response(text)
        )
    except ValueError:
        return False
    pending = min(asked, key=lambda p: p.queued_at)
    try:
        from . import armors as armors_mod
        from . import trims as trims_mod
        armor_name = armors_mod.active_armor_name(state_root)
        test_window_end = None
        engagement_id = None
        if armor_name:
            tid = trims_mod.active_trim_id(state_root, armor_name)
            if tid:
                meta = trims_mod.load_trim(state_root, armor_name, tid)
                if meta is not None:
                    test_window_end = meta.test_window_end
                    engagement_id = meta.engagement_id
        _tr.record_classification(
            state_root,
            key=pending.classification_key,
            classification=classification,
            confidence=confidence,
            reasoning=reasoning,
            test_window_end=test_window_end,
            engagement_id=engagement_id,
        )
    except Exception:
        return False
    pop(state_root, pending.classification_key)
    return True


def maybe_inject_into_provider_request(
    state_root: Path, provider: str, req_json: dict,
) -> dict | None:
    """Provider-aware variant used by the MITM proxy. Handles both
    Anthropic's top-level ``system`` field and OpenAI's role=system
    message in ``messages``. Returns the mutated dict or None.
    """
    if not is_weapon_context(state_root):
        return None
    pendings = get_all(state_root)
    pending = next((p for p in pendings if not p.asked), None)
    if pending is None:
        return None
    new_json = _inject_provider_specific(
        provider, req_json, pending.classification_prompt,
    )
    if new_json is None:
        return None
    pending.asked = True
    return new_json


def _inject_provider_specific(
    provider: str, req_json: dict, prompt: str,
) -> dict | None:
    if not isinstance(req_json, dict):
        return None
    if provider == "anthropic":
        new = dict(req_json)
        existing = req_json.get("system")
        if isinstance(existing, str):
            new["system"] = (
                prompt + "\n\n" + existing if existing else prompt
            )
        elif isinstance(existing, list):
            new["system"] = [
                {"type": "text", "text": prompt}, *existing,
            ]
        else:
            new["system"] = prompt
        return new
    # Default to OpenAI/litellm shape.
    return inject_system_message(req_json, prompt)


def inject_system_message(payload: dict, message: str) -> dict | None:
    """Add ``message`` as system-prompt context to ``payload['messages']``.

    If the conversation already opens with a system message, **merge** into it
    (our text first) rather than adding a second one — many providers (vLLM /
    Qwen3, etc.) reject a request with two leading system messages or a system
    message that isn't first ("System message must be at the beginning").
    Otherwise prepend a fresh leading system message. Returns a shallow copy, or
    None when the payload shape is unrecognized.
    """
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return None
    new_payload = dict(payload)
    if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
        head = dict(msgs[0])
        existing = head.get("content")
        if isinstance(existing, str):
            head["content"] = f"{message}\n\n{existing}" if existing else message
        elif isinstance(existing, list):
            head["content"] = [{"type": "text", "text": message}, *existing]
        else:
            head["content"] = message
        new_payload["messages"] = [head, *msgs[1:]]
    else:
        new_payload["messages"] = [{"role": "system", "content": message}, *msgs]
    return new_payload


def extract_response_text(payload: dict) -> str:
    """Pull text content from common LLM response shapes:
    ``{content: "..."}``, ``{content: [{text: "..."}]}`` (Anthropic),
    ``{choices: [{message: {content: "..."}}]}`` (OpenAI).
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message") or {}
            if isinstance(msg, dict):
                c = msg.get("content")
                if isinstance(c, str):
                    return c
    return ""
