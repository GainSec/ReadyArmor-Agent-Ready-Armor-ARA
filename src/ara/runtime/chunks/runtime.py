"""ChunkRuntime — chunk-swap supervisor.

Loaded at engagement start with the sealed weapon's chunk graph.
Mounts the entry chunk; processes exit-tag emissions; validates
caller restrictions; mints a new activation_id on each swap (via
:class:`ActivationManager`); audit-logs every transition; exposes
``is_tool_allowed`` so the agent loop can gate channel calls by the
mounted chunk's ``tool_surface``.

Operator override tags (``<pause>`` / ``<resume>`` / ``<kill>`` /
``<jump:ID>`` / ``<rewind:N>``) bypass the per-chunk exit table —
they come from a trusted operator UI, not the agent.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ara.forge.chunks.chunkdef import ChunkDef, read_chunks_dir
from ara.runtime.activation import ActivationManager


# ---------- result type ----------

@dataclass(frozen=True)
class SwapResult:
    ok: bool
    new_activation_id: str | None = None
    from_chunk: str | None = None
    to_chunk: str | None = None
    tag: str | None = None
    caller: str | None = None
    error: str | None = None
    error_kind: str | None = None  # "invalid_tag" | "caller_mismatch" | "unknown_chunk" | etc.

    @classmethod
    def success(
        cls, *, from_chunk: str, to_chunk: str, tag: str, caller: str,
        new_activation_id: str,
    ) -> "SwapResult":
        return cls(
            ok=True,
            new_activation_id=new_activation_id,
            from_chunk=from_chunk, to_chunk=to_chunk,
            tag=tag, caller=caller,
        )

    @classmethod
    def invalid_tag(cls, tag: str, *, from_chunk: str, valid_tags: list[str]) -> "SwapResult":
        return cls(
            ok=False, from_chunk=from_chunk, tag=tag,
            error_kind="invalid_tag",
            error=f"tag {tag!r} not in chunk {from_chunk!r} exits {sorted(valid_tags)}",
        )

    @classmethod
    def caller_mismatch(
        cls, *, tag: str, expected: str, got: str, from_chunk: str,
    ) -> "SwapResult":
        return cls(
            ok=False, from_chunk=from_chunk, tag=tag, caller=got,
            error_kind="caller_mismatch",
            error=f"tag {tag!r} requires caller={expected!r}, got {got!r}",
        )

    @classmethod
    def unknown_chunk(cls, chunk_id: str) -> "SwapResult":
        return cls(
            ok=False, error_kind="unknown_chunk",
            error=f"chunk {chunk_id!r} not in graph",
        )


# ---------- weapon graph wrapper ----------

@dataclass
class WeaponChunkGraph:
    """In-memory map of a sealed weapon's chunk graph."""
    chunks: dict[str, ChunkDef] = field(default_factory=dict)

    @classmethod
    def load(cls, weapon_dir: Path) -> "WeaponChunkGraph":
        items = read_chunks_dir(Path(weapon_dir))
        return cls(chunks={c.id: c for c in items})

    def chunk(self, chunk_id: str) -> ChunkDef | None:
        return self.chunks.get(chunk_id)

    def entry_chunk(self) -> ChunkDef | None:
        for c in self.chunks.values():
            if c.is_entry:
                return c
        return None

    def has(self, chunk_id: str) -> bool:
        return chunk_id in self.chunks


# ---------- runtime ----------

_OVERRIDE_PREFIX_JUMP = "<jump:"
_OVERRIDE_PREFIX_REWIND = "<rewind:"
_PLAIN_OVERRIDES = {"<kill>", "<pause>", "<resume>"}


class ChunkRuntime:
    def __init__(
        self,
        *,
        graph: WeaponChunkGraph,
        activation_manager: ActivationManager,
        crumb_tracker=None,  # optional: runtime.anomaly.crumb_tracker.CrumbTracker
        history_path: Path | None = None,
    ) -> None:
        self.graph = graph
        self.activation = activation_manager
        self.crumb_tracker = crumb_tracker
        self.history_path = history_path
        self._current_chunk_id: str | None = None
        self._chunk_started_at: float | None = None
        self._paused = False
        self._killed = False

    # ---------- queries ----------

    def current_chunk_id(self) -> str | None:
        return self._current_chunk_id

    def current_chunk(self) -> ChunkDef | None:
        if self._current_chunk_id is None:
            return None
        return self.graph.chunk(self._current_chunk_id)

    def mount_existing_chunk(self, chunk_id: str) -> SwapResult:
        """Hydrate the active chunk pointer from persisted history.

        This does not append a history row or mint a new activation. It is for
        short-lived helper processes that need to re-enter the active chunk
        before using the normal audited chunk channel.
        """
        chunk = self.graph.chunk(chunk_id)
        if chunk is None:
            return SwapResult.unknown_chunk(chunk_id)
        self._mount(chunk)
        return SwapResult(
            ok=True,
            from_chunk=chunk_id,
            to_chunk=chunk_id,
            tag="<hydrate>",
            caller="system",
            new_activation_id=self.activation.peek(),
        )

    def is_tool_allowed(self, channel_name: str) -> bool:
        """True iff the channel is in the active chunk's tool_surface.

        When no chunk is mounted (pre-initialize), every channel is
        allowed — the supervisor hasn't started gating yet.
        """
        chunk = self.current_chunk()
        if chunk is None:
            return True
        return channel_name in chunk.tool_surface

    def is_paused(self) -> bool:
        return self._paused

    def is_killed(self) -> bool:
        return self._killed

    # ---------- lifecycle ----------

    def initialize(self) -> SwapResult:
        if self._current_chunk_id is not None:
            return SwapResult(
                ok=False, error_kind="already_initialized",
                error="ChunkRuntime already initialized",
            )
        entry = self.graph.entry_chunk()
        if entry is None:
            return SwapResult(
                ok=False, error_kind="no_entry",
                error="weapon has no entry chunk (none with is_entry=True)",
            )
        new_aid = self.activation.mint(reason="initial", chunk_id=entry.id)
        self._mount(entry)
        self._append_history({
            "ts": time.time(),
            "kind": "initialize",
            "to_chunk": entry.id,
            "activation_id": new_aid,
        })
        return SwapResult.success(
            from_chunk="", to_chunk=entry.id, tag="<init>",
            caller="system", new_activation_id=new_aid,
        )

    def emit_tag(self, tag: str, *, caller: str) -> SwapResult:
        """Process an agent/operator-emitted exit tag.

        Operator override tags (``<pause>``, ``<resume>``, ``<kill>``,
        ``<jump:ID>``, ``<rewind:N>``) bypass the per-chunk exit table
        but require ``caller="operator"`` (or ``"system"`` for kill).
        """
        if self._killed:
            return SwapResult(ok=False, error_kind="killed",
                              error="session killed; no further transitions")
        # Override tags
        if tag in _PLAIN_OVERRIDES or tag.startswith(_OVERRIDE_PREFIX_JUMP) \
                or tag.startswith(_OVERRIDE_PREFIX_REWIND):
            return self._handle_override(tag, caller=caller)

        chunk = self.current_chunk()
        if chunk is None:
            return SwapResult(
                ok=False, error_kind="not_initialized",
                error="ChunkRuntime not initialized; call initialize() first",
            )
        if tag not in chunk.exits:
            return SwapResult.invalid_tag(
                tag, from_chunk=chunk.id,
                valid_tags=list(chunk.exits.keys()),
            )
        target = chunk.exits[tag]
        if target.requires_caller and target.requires_caller != caller:
            return SwapResult.caller_mismatch(
                tag=tag, expected=target.requires_caller,
                got=caller, from_chunk=chunk.id,
            )
        if not self.graph.has(target.next_chunk_id):
            return SwapResult.unknown_chunk(target.next_chunk_id)
        tgt_chunk = self.graph.chunk(target.next_chunk_id)
        # Unattended-run guard: an autonomous task run must never trap itself in
        # a human-only terminal hold (e.g. operator_review) — a chunk that is
        # terminal AND exposes no tools, so the agent can neither act nor emit a
        # forward tag, and no operator will release it. Entering it freezes the
        # run for the rest of the window. Block the transition (agent callers
        # only; operators/system may still enter it) so the agent picks a
        # productive forward exit; the durable-loop time-forcer also advances
        # enum→probe as a backstop. Keyed on ARA_TASK_RUN_ID (set only for
        # unattended task runs); interactive sessions are unaffected.
        if (caller == "agent"
                and os.environ.get("ARA_TASK_RUN_ID")
                and getattr(tgt_chunk, "is_terminal", False)
                and not getattr(tgt_chunk, "tool_surface", None)):
            return SwapResult(
                ok=False, error_kind="operator_hold_unavailable",
                error=(
                    f"tag {tag!r} enters operator-hold chunk "
                    f"{target.next_chunk_id!r}, which is unavailable on an "
                    f"unattended run — no operator is present to release it, "
                    f"and it exposes no tools, so entering it would halt the "
                    f"engagement. Choose a forward exit that continues the "
                    f"work (e.g. advance toward probe)."),
                tag=tag, from_chunk=chunk.id, caller=caller,
            )
        return self._swap(chunk, tgt_chunk, tag=tag, caller=caller)

    # ---------- override handling ----------

    def _handle_override(self, tag: str, *, caller: str) -> SwapResult:
        if caller not in ("operator", "system"):
            return SwapResult(
                ok=False, error_kind="caller_mismatch",
                error=f"override tag {tag!r} requires operator/system, got {caller!r}",
                tag=tag, caller=caller,
            )
        if tag == "<kill>":
            self._killed = True
            self._append_history({
                "ts": time.time(), "kind": "kill", "caller": caller,
                "activation_id": self.activation.peek(),
            })
            return SwapResult(
                ok=True, tag=tag, caller=caller,
                from_chunk=self._current_chunk_id, to_chunk=None,
                error_kind="kill_acknowledged",
            )
        if tag == "<pause>":
            self._paused = True
            self._append_history({
                "ts": time.time(), "kind": "pause", "caller": caller,
                "activation_id": self.activation.peek(),
            })
            return SwapResult(
                ok=True, tag=tag, caller=caller,
                from_chunk=self._current_chunk_id, to_chunk=self._current_chunk_id,
            )
        if tag == "<resume>":
            self._paused = False
            self._append_history({
                "ts": time.time(), "kind": "resume", "caller": caller,
                "activation_id": self.activation.peek(),
            })
            return SwapResult(
                ok=True, tag=tag, caller=caller,
                from_chunk=self._current_chunk_id, to_chunk=self._current_chunk_id,
            )
        if tag.startswith(_OVERRIDE_PREFIX_JUMP):
            target_id = tag[len(_OVERRIDE_PREFIX_JUMP):-1]  # strip "<jump:" + ">"
            if not self.graph.has(target_id):
                return SwapResult.unknown_chunk(target_id)
            current = self.current_chunk()
            target_chunk = self.graph.chunk(target_id)
            new_aid = self.activation.mint(
                reason="chunk_swap_jump", chunk_id=target_id,
            )
            self._mount(target_chunk)
            self._append_history({
                "ts": time.time(), "kind": "jump", "caller": caller,
                "from_chunk": current.id if current else None,
                "to_chunk": target_id,
                "activation_id": new_aid,
            })
            return SwapResult.success(
                from_chunk=current.id if current else "",
                to_chunk=target_id, tag=tag, caller=caller,
                new_activation_id=new_aid,
            )
        if tag.startswith(_OVERRIDE_PREFIX_REWIND):
            try:
                n = int(tag[len(_OVERRIDE_PREFIX_REWIND):-1])
            except ValueError:
                return SwapResult(
                    ok=False, error_kind="invalid_override",
                    error=f"malformed rewind tag: {tag!r}",
                )
            return self._rewind(n, caller=caller)
        return SwapResult(
            ok=False, error_kind="invalid_override",
            error=f"unknown override tag: {tag!r}",
        )

    def _rewind(self, n: int, *, caller: str) -> SwapResult:
        """Roll the active chunk pointer back N swaps. Append-only state
        is preserved; audit shows both forward and backward transitions.
        """
        history = self._read_history()
        # Walk back n swap-or-jump events
        swap_count = 0
        target_idx: int | None = None
        for i in range(len(history) - 1, -1, -1):
            kind = history[i].get("kind")
            if kind in ("initialize", "swap", "jump"):
                if swap_count == n:
                    target_idx = i
                    break
                swap_count += 1
        if target_idx is None:
            return SwapResult(
                ok=False, error_kind="rewind_out_of_range",
                error=f"can't rewind {n} swaps; only {swap_count} available",
            )
        target_chunk_id = history[target_idx].get("to_chunk")
        if not target_chunk_id or not self.graph.has(target_chunk_id):
            return SwapResult.unknown_chunk(str(target_chunk_id))
        target_chunk = self.graph.chunk(target_chunk_id)
        new_aid = self.activation.mint(
            reason="chunk_rewind", chunk_id=target_chunk_id,
        )
        from_chunk_id = self._current_chunk_id
        self._mount(target_chunk)
        self._append_history({
            "ts": time.time(), "kind": "rewind", "caller": caller,
            "from_chunk": from_chunk_id, "to_chunk": target_chunk_id,
            "rewound_swaps": n, "activation_id": new_aid,
        })
        return SwapResult.success(
            from_chunk=from_chunk_id or "",
            to_chunk=target_chunk_id, tag=f"<rewind:{n}>",
            caller=caller, new_activation_id=new_aid,
        )

    # ---------- internals ----------

    def _swap(
        self, from_chunk: ChunkDef, to_chunk: ChunkDef,
        *, tag: str, caller: str,
    ) -> SwapResult:
        new_aid = self.activation.mint(reason="chunk_swap", chunk_id=to_chunk.id)
        self._mount(to_chunk)
        self._append_history({
            "ts": time.time(), "kind": "swap",
            "from_chunk": from_chunk.id, "to_chunk": to_chunk.id,
            "tag": tag, "caller": caller, "activation_id": new_aid,
        })
        return SwapResult.success(
            from_chunk=from_chunk.id, to_chunk=to_chunk.id,
            tag=tag, caller=caller, new_activation_id=new_aid,
        )

    def _mount(self, chunk: ChunkDef) -> None:
        self._current_chunk_id = chunk.id
        self._chunk_started_at = time.time()
        # CrumbTracker integration: clear active crumb on chunk swap so
        # subsequent tool calls under the new chunk don't carry the old
        # crumb's identity. The agent (or supervisor) calls
        # set_active(crumb_id) before each procedure.
        if self.crumb_tracker is not None:
            self.crumb_tracker.set_active(None)

    def _read_history(self) -> list[dict]:
        if not self.history_path or not self.history_path.exists():
            return []
        out: list[dict] = []
        with self.history_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

    def _append_history(self, entry: dict) -> None:
        if self.history_path is None:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True, default=str) + "\n"
        fd = os.open(
            self.history_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
        )
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)


# ---------- inline tag parser ----------

_INLINE_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9_:.\-]*)>")


def parse_inline_tags(text: str) -> list[str]:
    """Extract all ``<tag>``-style tokens from agent text.

    Returns the full ``<tag>`` form (with brackets) so callers can
    pass results directly to ``ChunkRuntime.emit_tag``. Order
    preserved; duplicates kept.
    """
    return [f"<{m.group(1)}>" for m in _INLINE_TAG_RE.finditer(text)]
