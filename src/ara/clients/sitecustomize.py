"""ARA runtime auto-patch for Python workers.

Python's ``site`` module imports any ``sitecustomize`` it finds on
``sys.path`` at interpreter startup, before user code runs. ARA's
launcher prepends a directory containing this file to ``PYTHONPATH``,
so any Python worker — including OpenClaw — picks it up automatically
with no source modification.

What it does: when the worker imports the OpenAI SDK (or LiteLLM),
we wrap the ``chat.completions.create`` and streaming entry points so
every call routes through ``ara.clients.transcript.intercept`` first.
The wrapper records the request, asks the supervisor for a decision,
and either passes the call through, rewrites the payload, or
synthesizes a deny response without ever touching the network.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys


_TARGET_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_OUTPUT_CAPTURE_LIMIT = 64 * 1024


def _protocol_enabled() -> bool:
    # Only attach when the supervisor has advertised the channel; this
    # also keeps un-supervised invocations (tests, dev scripts) clean.
    return os.environ.get("ARA_TRANSCRIPT_PROTOCOL") == "v1"


def _sdk_intercept_enabled() -> bool:
    if not _protocol_enabled():
        return False
    # Escape hatch: agent runtimes that don't need (or interact badly
    # with) the openai-SDK call wrap can opt out. Hermes' streaming
    # chat completions hang here because intercept() ships a large
    # request payload over the unix socket and the supervisor's reply
    # latency stacks 10s timeouts × 3 SDK retries × 2 inner attempts —
    # the agent surfaces this as APIConnectionError before any HTTP is
    # tried. The MITM proxy + libc shim still record the turn at the
    # network/syscall layer; only the SDK-level transcript is dropped.
    if os.environ.get("ARA_PYINTERCEPT_DISABLE", "").strip() in ("1", "true", "yes"):
        return False
    return True


def _subprocess_capture_enabled() -> bool:
    return _protocol_enabled()


def _task_run_active() -> bool:
    return _protocol_enabled() and bool(os.environ.get("ARA_TASK_RUN_ID", "").strip())


def _split_subprocess_command(cmd) -> tuple[str, list[str]]:
    if isinstance(cmd, (list, tuple)):
        parts = [str(x) for x in cmd]
    else:
        try:
            parts = shlex.split(str(cmd))
        except ValueError:
            parts = [str(cmd)]
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _query_subprocess_decision(command: str, args: list[str]) -> dict:
    from ara.clients import transcript
    return transcript.query(
        op="exec.spawn",
        command=command,
        args=args,
        cwd=os.getcwd(),
    )


def _enforce_subprocess_decision(cmd, *, query=None) -> None:
    if not _task_run_active():
        return
    command, args = _split_subprocess_command(cmd)
    if not command:
        return
    query_fn = query or _query_subprocess_decision
    try:
        decision = query_fn(command, args)
    except Exception as exc:
        raise PermissionError(
            f"ARA_SUBPROCESS_QUERY_FAILED: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(decision, dict):
        raise PermissionError("ARA_SUBPROCESS_QUERY_FAILED: invalid decision")
    if decision.get("verdict") != "allow":
        reason = decision.get("reason") or "ARA_SUBPROCESS_DENIED"
        raise PermissionError(f"ARA_DENY: {reason} ({command})")


def _try_import_intercept():
    try:
        from ara.clients.transcript import intercept
        return intercept
    except Exception:
        return None


def _to_text(value) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, bytes):
        raw = value[:_OUTPUT_CAPTURE_LIMIT]
        return raw.decode("utf-8", errors="replace"), len(value) > len(raw)
    text = str(value)
    if len(text.encode("utf-8", errors="replace")) <= _OUTPUT_CAPTURE_LIMIT:
        return text, False
    raw = text.encode("utf-8", errors="replace")[:_OUTPUT_CAPTURE_LIMIT]
    return raw.decode("utf-8", errors="replace"), True


def _argv_list(args) -> list[str]:
    if isinstance(args, (list, tuple)):
        return [str(a) for a in args]
    return [str(args)]


def _default_event_sender(event: dict) -> None:
    try:
        from ara.clients import transcript as _transcript
        sock = _transcript._connect()  # type: ignore[attr-defined]
        if sock is None:
            return
        event = dict(event)
        event["seq"] = _transcript._next_seq()  # type: ignore[attr-defined]
        with _transcript._SOCK_LOCK:  # type: ignore[attr-defined]
            sock.sendall((json.dumps(event) + "\n").encode("utf-8"))
    except Exception:
        pass


def _emit_subprocess_output_event(
    args,
    stdout,
    stderr,
    *,
    returncode=None,
    sender=None,
) -> None:
    argv = _argv_list(args)
    haystack = " ".join(argv)
    match = _TARGET_URL_RE.search(haystack)
    if match is None:
        return
    stdout_text, stdout_truncated = _to_text(stdout)
    stderr_text, stderr_truncated = _to_text(stderr)
    if not stdout_text and not stderr_text:
        return
    command = argv[0] if argv else ""
    if not command:
        return
    event = {
        "type": "event",
        "kind": "spawn_output",
        "source": "python-subprocess",
        "pid": os.getpid(),
        "command": command,
        "args": argv,
        "url": match.group(0).rstrip("),.;"),
        "exit_code": returncode,
        "signal": None,
        "stdout_text": stdout_text,
        "stderr_text": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    (sender or _default_event_sender)(event)


def _patch_subprocess_module() -> None:
    try:
        import subprocess
    except Exception:
        return
    if getattr(subprocess, "_ara_wrapped", False):
        return

    orig_run = subprocess.run
    orig_check_output = subprocess.check_output
    orig_popen = subprocess.Popen
    orig_communicate = orig_popen.communicate

    class PatchedPopen(orig_popen):
        def __init__(self, *popenargs, **kwargs):
            cmd = popenargs[0] if popenargs else kwargs.get("args")
            _enforce_subprocess_decision(cmd)
            super().__init__(*popenargs, **kwargs)

    def patched_run(*popenargs, **kwargs):
        result = orig_run(*popenargs, **kwargs)
        cmd = popenargs[0] if popenargs else kwargs.get("args")
        try:
            _emit_subprocess_output_event(
                getattr(result, "args", cmd),
                getattr(result, "stdout", None),
                getattr(result, "stderr", None),
                returncode=getattr(result, "returncode", None),
            )
        except Exception:
            pass
        return result

    def patched_check_output(*popenargs, **kwargs):
        cmd = popenargs[0] if popenargs else kwargs.get("args")
        try:
            output = orig_check_output(*popenargs, **kwargs)
        except subprocess.CalledProcessError as exc:
            try:
                _emit_subprocess_output_event(
                    getattr(exc, "cmd", cmd),
                    getattr(exc, "output", None),
                    getattr(exc, "stderr", None),
                    returncode=getattr(exc, "returncode", None),
                )
            except Exception:
                pass
            raise
        try:
            _emit_subprocess_output_event(cmd, output, None, returncode=0)
        except Exception:
            pass
        return output

    def patched_communicate(self, *args, **kwargs):
        out = orig_communicate(self, *args, **kwargs)
        try:
            stdout = out[0] if isinstance(out, tuple) and len(out) > 0 else None
            stderr = out[1] if isinstance(out, tuple) and len(out) > 1 else None
            _emit_subprocess_output_event(
                getattr(self, "args", []),
                stdout,
                stderr,
                returncode=getattr(self, "returncode", None),
            )
        except Exception:
            pass
        return out

    patched_run._ara_wrapped = True
    patched_check_output._ara_wrapped = True
    patched_communicate._ara_wrapped = True
    PatchedPopen._ara_wrapped = True
    subprocess.run = patched_run
    subprocess.check_output = patched_check_output
    PatchedPopen.communicate = patched_communicate
    subprocess.Popen = PatchedPopen
    subprocess._ara_wrapped = True


def _safe_dump(obj):
    """Coerce arbitrary kwargs into a JSON-safe shape for the transcript.
    Anything we can't serialize falls back to its repr.

    Implementation note: the previous version called ``json.dumps(obj)``
    once per recursion level to test serialisability, then walked the
    container types when that raised. For deeply-nested kwargs (hermes
    chat completions carry ~18 MCP tool schemas with multi-level nesting)
    this re-serialised the whole subtree at every level — quadratic in
    depth × size, which stalled the SDK wrapper for tens of seconds and
    surfaced as ``APIConnectionError`` because the openai-python retry
    timer fired before the wrapper returned. The fix is to ask
    ``json.dumps`` to stringify anything it can't natively handle in a
    single pass via ``default=`` — so we never recurse and never re-try
    the whole tree."""
    import json
    try:
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump(mode="json")
        elif hasattr(obj, "dict"):
            obj = obj.dict()
    except Exception:
        pass
    try:
        # default=repr keeps the call O(N) and is happy with pydantic
        # models, httpx clients, asyncio futures — anything that has a
        # __repr__ (i.e. everything in Python).
        return json.loads(json.dumps(obj, default=repr))
    except Exception:
        # Last-ditch: don't let transcript serialisation block the
        # actual LLM call. The libc shim + MITM still record the turn
        # at the network/syscall layer.
        return {"_ara_dump_failed": repr(type(obj).__name__)}


def _looks_like_stream(resp) -> bool:
    if isinstance(resp, (str, bytes, bytearray, dict, list, tuple)):
        return False
    return hasattr(resp, "__iter__") and not hasattr(resp, "choices")


class _TranscriptStream:
    """Pass-through wrapper that records OpenAI streaming chunks.

    ``chat.completions.create(stream=True)`` returns an iterator. Logging it
    immediately only captures ``<openai.Stream object ...>``. This wrapper
    preserves iteration semantics and emits the chunk list after consumption.
    """

    def __init__(self, stream, intercept, model):
        self._stream = stream
        self._intercept = intercept
        self._model = model
        self._chunks = []
        self._done = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._stream)
        except StopIteration:
            self._flush("exhausted")
            raise
        self._chunks.append(_safe_dump(chunk))
        return chunk

    def __enter__(self):
        if hasattr(self._stream, "__enter__"):
            entered = self._stream.__enter__()
            if entered is not self._stream:
                self._stream = entered
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if hasattr(self._stream, "__exit__"):
                return self._stream.__exit__(exc_type, exc, tb)
        finally:
            self._flush("closed")
        return False

    def close(self):
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                return close()
        finally:
            self._flush("closed")

    def _flush(self, reason):
        if self._done:
            return
        self._done = True
        try:
            self._intercept(
                "response",
                {"stream": True, "complete": reason == "exhausted",
                 "reason": reason, "chunks": self._chunks},
                model=self._model,
            )
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _TranscriptAsyncStream:
    def __init__(self, stream, intercept, model):
        self._stream = stream
        self._intercept = intercept
        self._model = model
        self._chunks = []
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._stream.__anext__()
        except StopAsyncIteration:
            self._flush("exhausted")
            raise
        self._chunks.append(_safe_dump(chunk))
        return chunk

    async def __aenter__(self):
        if hasattr(self._stream, "__aenter__"):
            entered = await self._stream.__aenter__()
            if entered is not self._stream:
                self._stream = entered
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if hasattr(self._stream, "__aexit__"):
                return await self._stream.__aexit__(exc_type, exc, tb)
        finally:
            self._flush("closed")
        return False

    async def close(self):
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    return await result
                return result
        finally:
            self._flush("closed")

    def _flush(self, reason):
        if self._done:
            return
        self._done = True
        try:
            self._intercept(
                "response",
                {"stream": True, "complete": reason == "exhausted",
                 "reason": reason, "chunks": self._chunks},
                model=self._model,
            )
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _patch_openai_module(openai_mod, intercept):
    """Wrap chat.completions.create on every Client/AsyncClient instance.
    The OpenAI SDK builds these per-client, so we patch the resource
    classes directly."""
    try:
        from openai.resources.chat.completions import Completions, AsyncCompletions
    except Exception:
        return

    if getattr(Completions.create, "_ara_wrapped", False):
        return

    orig_create = Completions.create

    def patched_create(self, *args, **kwargs):
        model = kwargs.get("model")
        d = intercept("request", _safe_dump(kwargs), model=model) or {}
        action = d.get("action", "allow")
        if action == "deny":
            return _synth_response(d.get("injected_response"))
        if action == "rewrite" and isinstance(d.get("new_payload"), dict):
            # Only adopt fields that survive a JSON round-trip cleanly —
            # tools, response_format, http_client, etc. typically carry
            # pydantic models or live httpx objects in the original
            # kwargs that the supervisor's repr-based serialisation
            # can't reproduce. Replacing kwargs wholesale with the
            # round-tripped payload broke streaming chat completions
            # for SDK-wrapped agents (hermes): the SDK still got
            # ``messages`` and ``model`` back but lost the live tool
            # schemas / transport, then surfaced as APIConnectionError.
            # Merge only ``messages`` and ``system`` (the two fields
            # the forge-brief injector touches) into the original
            # kwargs.
            new_payload = d["new_payload"]
            for k in ("messages", "system"):
                if k in new_payload:
                    kwargs[k] = new_payload[k]
            model = kwargs.get("model")
        resp = orig_create(self, *args, **kwargs)
        if kwargs.get("stream") is True or _looks_like_stream(resp):
            return _TranscriptStream(resp, intercept, model)
        try:
            intercept("response", _safe_dump(resp), model=model)
        except Exception:
            pass
        return resp

    patched_create._ara_wrapped = True
    Completions.create = patched_create

    if AsyncCompletions and not getattr(AsyncCompletions.create, "_ara_wrapped", False):
        orig_acreate = AsyncCompletions.create

        async def patched_acreate(self, *args, **kwargs):
            model = kwargs.get("model")
            d = intercept("request", _safe_dump(kwargs), model=model) or {}
            action = d.get("action", "allow")
            if action == "deny":
                return _synth_response(d.get("injected_response"))
            if action == "rewrite" and isinstance(d.get("new_payload"), dict):
                # See sync patched_create for why we merge only
                # ``messages`` and ``system`` instead of replacing
                # kwargs wholesale.
                new_payload = d["new_payload"]
                for k in ("messages", "system"):
                    if k in new_payload:
                        kwargs[k] = new_payload[k]
                model = kwargs.get("model")
            resp = await orig_acreate(self, *args, **kwargs)
            if kwargs.get("stream") is True or hasattr(resp, "__aiter__"):
                return _TranscriptAsyncStream(resp, intercept, model)
            try:
                intercept("response", _safe_dump(resp), model=model)
            except Exception:
                pass
            return resp

        patched_acreate._ara_wrapped = True
        AsyncCompletions.create = patched_acreate


class _StubResponse:
    """Minimal duck-type for an OpenAI ChatCompletion when ARA denies."""
    def __init__(self, payload):
        self._payload = payload or {}
        self.choices = [type("C", (), {
            "message": type("M", (), {
                "role": "assistant",
                "content": str(payload or "[ARA denied: no message provided]"),
                "tool_calls": None,
            })(),
            "finish_reason": "ara_denied",
            "index": 0,
        })()]
        self.usage = type("U", (), {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        })()


def _synth_response(injected):
    return _StubResponse(injected)


def _install_finder():
    """Register an import audit hook that fires the patch the moment
    ``openai`` (or ``litellm``) is imported by the worker."""
    intercept = _try_import_intercept()
    if intercept is None:
        return

    def _after_import(event, args):
        if event != "import":
            return
        # ``args`` is (module, filename, sys.path, sys.meta_path, sys.path_hooks).
        # The module hasn't been added to sys.modules yet during this
        # event; it lands by the time our caller continues.
        try:
            name = args[0]
        except Exception:
            return
        if name in ("openai", "openai.resources.chat.completions"):
            mod = sys.modules.get("openai")
            if mod is not None:
                try:
                    _patch_openai_module(mod, intercept)
                except Exception:
                    pass

    try:
        sys.addaudithook(_after_import)
    except Exception:
        pass

    # If ``openai`` was already imported (rare for sitecustomize but
    # possible if some agent stack pre-loaded it), patch it now.
    if "openai" in sys.modules:
        try:
            _patch_openai_module(sys.modules["openai"], intercept)
        except Exception:
            pass


if _sdk_intercept_enabled():
    _install_finder()
if _subprocess_capture_enabled():
    _patch_subprocess_module()
