"""ARA debug event stream.

Diagnostics must never change runtime behavior. This module writes a
redacted, append-only JSONL stream when a state root or explicit log path is
available, then silently drops diagnostics if logging itself fails.
"""
from __future__ import annotations

import inspect
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_LOG_NAME = "ara-debug.jsonl"
_SENSITIVE_MARKS = (
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "secret",
    "password",
    "passwd",
    "cookie",
    "credential",
    "private_key",
    "client_secret",
)


def debug_log_path(
    state_root: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    explicit = str(env.get("ARA_DEBUG_LOG") or "").strip()
    if explicit:
        return Path(explicit)
    if state_root is not None:
        return Path(state_root) / "logs" / DEFAULT_LOG_NAME
    env_state = str(env.get("ARA_STATE_ROOT") or "").strip()
    if env_state and _truthy(env.get("ARA_DEBUG")):
        return Path(env_state) / "logs" / DEFAULT_LOG_NAME
    return None


def emit(
    component: str,
    event: str,
    *,
    state_root: Path | str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    level: str = "debug",
    environ: Mapping[str, str] | None = None,
    **fields: Any,
) -> None:
    """Append one debug event.

    Logging is enabled by an explicit ``state_root``/``ARA_DEBUG_LOG``. For
    process-wide debug with only ``ARA_STATE_ROOT``, set ``ARA_DEBUG=1``.
    """
    try:
        env = os.environ if environ is None else environ
        path = debug_log_path(state_root, environ=env)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "iso": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "component": str(component),
            "event": str(event),
            "level": str(level or "debug"),
            "caller": _caller(),
            "session_id": session_id or env.get("ARA_SESSION_ID"),
            "task_id": task_id or env.get("ARA_TASK_RUN_ID"),
            "fields": _sanitize(fields),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    except Exception:
        return


def emit_exception(
    component: str,
    event: str,
    exc: BaseException,
    *,
    state_root: Path | str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    environ: Mapping[str, str] | None = None,
    **fields: Any,
) -> None:
    fields = dict(fields)
    fields.update({
        "exception_type": type(exc).__name__,
        "exception": str(exc),
    })
    emit(
        component,
        event,
        state_root=state_root,
        session_id=session_id,
        task_id=task_id,
        level="error",
        environ=environ,
        **fields,
    )


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _caller() -> str:
    try:
        frame = inspect.currentframe()
        f = frame.f_back if frame else None
        while f is not None:
            module = f.f_globals.get("__name__", "")
            if module != __name__:
                code = f.f_code
                return f"{os.path.basename(code.co_filename)}:{f.f_lineno} {code.co_name}"
            f = f.f_back
    except Exception:
        return ""
    finally:
        try:
            del frame
        except Exception:
            pass
    return ""


def _mask(value: Any) -> str:
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            key_l = key_s.lower()
            if any(mark in key_l for mark in _SENSITIVE_MARKS):
                out[key_s] = _mask(item)
            else:
                out[key_s] = _sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = [
    "DEFAULT_LOG_NAME",
    "debug_log_path",
    "emit",
    "emit_exception",
]
