"""Typed channel for chunk-tag emission.

The agent (or operator UI) calls ``chunks.emit_tag`` instead of
embedding ``<tag>`` markers in prose; this is unambiguous and
audit-logged like any other channel call.
"""
from __future__ import annotations

from dataclasses import dataclass

from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection
from ara.runtime.chunks.runtime import ChunkRuntime


@dataclass
class ChunksEmitTagBody:
    tag: str


class ChunksEmitTagChannel(Channel):
    name = "chunks.emit_tag"
    schema = ChunksEmitTagBody
    sink_subsystem = "chunk_history.jsonl"

    def __init__(self, *, runtime: ChunkRuntime) -> None:
        self.runtime = runtime

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        result = self.runtime.emit_tag(body["tag"], caller=ctx.caller)
        if not result.ok:
            raise ChannelRejection(
                f"{result.error_kind}: {result.error}"
            )
        return {
            "from_chunk": result.from_chunk,
            "to_chunk": result.to_chunk,
            "new_activation_id": result.new_activation_id,
            "tag": result.tag,
        }


registry.register_class(ChunksEmitTagChannel)


def install_chunks_channels(*, runtime: ChunkRuntime) -> dict[str, Channel]:
    """Construct + register the chunks.emit_tag channel against a runtime.

    Idempotent: if already registered for a prior runtime, replaces
    the binding so the new runtime takes effect."""
    if registry.has("chunks.emit_tag"):
        # Re-bind: drop the prior instance, register fresh.
        registry.unregister_for_tests("chunks.emit_tag")
    ch = ChunksEmitTagChannel(runtime=runtime)
    registry.register(ch)
    return {"chunks.emit_tag": ch}
