"""Typed channels for evidence binder reads."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection


@dataclass
class EvidenceSearchBody:
    query: str
    source: str | None = None
    since: float | None = None
    k: int = 10


@dataclass
class EvidenceGetBody:
    evidence_id: str
    kb: int = 2


def _record_to_dict(rec) -> dict[str, Any]:
    return {
        "id": rec.id,
        "sha256": rec.sha256,
        "captured_at": rec.captured_at,
        "captured_during_activation_id": rec.captured_during_activation_id,
        "source": rec.source,
        "source_metadata": rec.source_metadata,
        "summary": rec.summary,
        "size_bytes": rec.size_bytes,
        "content_type": rec.content_type,
        "masked": rec.masked,
    }


def _binder_for(ctx: ChannelContext, sessions_root: Path) -> EvidenceBinder:
    if not ctx.engagement_id:
        raise ChannelRejection("ChannelContext.engagement_id required for evidence")
    return EvidenceBinder(Path(sessions_root) / ctx.engagement_id)


class EvidenceSearchChannel(Channel):
    name = "evidence.search"
    schema = EvidenceSearchBody
    sink_subsystem = "evidence/index.db"

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def validate(self, body: dict) -> None:
        super().validate(body)
        if not str(body.get("query") or "").strip():
            raise ChannelRejection("query required")
        k = int(body.get("k", 10))
        if k < 1 or k > 50:
            raise ChannelRejection("k must be between 1 and 50")

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        binder = _binder_for(ctx, self.sessions_root)
        results = binder.search(
            str(body["query"]),
            source=body.get("source"),
            since=body.get("since"),
            k=int(body.get("k", 10)),
        )
        return {
            "results": [
                {
                    "record": _record_to_dict(r.record),
                    "score": r.score,
                    "excerpt": r.excerpt,
                }
                for r in results
            ],
        }


class EvidenceGetChannel(Channel):
    name = "evidence.get"
    schema = EvidenceGetBody
    sink_subsystem = "evidence/index.db"

    def __init__(self, *, sessions_root: Path) -> None:
        self.sessions_root = Path(sessions_root)

    def validate(self, body: dict) -> None:
        super().validate(body)
        if not str(body.get("evidence_id") or "").strip():
            raise ChannelRejection("evidence_id required")
        kb = int(body.get("kb", 2))
        if kb < 1 or kb > 64:
            raise ChannelRejection("kb must be between 1 and 64")

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        binder = _binder_for(ctx, self.sessions_root)
        evidence_id = str(body["evidence_id"])
        rec = binder.get_record(evidence_id)
        if rec is None:
            raise ChannelRejection(f"evidence_id not found: {evidence_id}")
        head = binder.head(evidence_id, kb=int(body.get("kb", 2)))
        return {
            "record": _record_to_dict(rec),
            "excerpt": head.decode("utf-8", errors="replace"),
        }


registry.register_class(EvidenceSearchChannel)
registry.register_class(EvidenceGetChannel)


def install_evidence_channels(*, sessions_root: Path) -> dict[str, Channel]:
    chans: dict[str, Channel] = {}
    for cls in (EvidenceSearchChannel, EvidenceGetChannel):
        if registry.has(cls.name):
            registry.unregister_for_tests(cls.name)
        ch = cls(sessions_root=sessions_root)
        registry.register(ch)
        chans[ch.name] = ch
    return chans
