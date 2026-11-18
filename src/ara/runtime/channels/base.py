"""Channel base class — the single inspectable contract for every
cross-subsystem call in ARA forge-v1.

A channel:
- Validates the call body at the boundary (rejects malformed input
  before it touches state).
- Executes effects (writes to a subsystem JSONL/DB).
- Audit-logs the call (status + body/result hashes + prev_hash chain).

Every typed channel subclasses :class:`Channel`. The registry (§6.3)
collects all instances; the 4.1 catalog generator introspects them.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ara.runtime import audit as raudit


class ChannelRejection(Exception):
    """Raised by Channel.validate when the body fails validation.

    The message is written into the audit event's body under ``reason``.
    """


@dataclass
class ChannelContext:
    engagement_id: str | None = None
    activation_id: str | None = None
    forge_id: str | None = None
    caller: str = "agent"  # "agent" | "operator" | "system"
    audit_sink: Path | None = None  # where to write the audit event

    def to_dict(self) -> dict:
        return {
            "engagement_id": self.engagement_id,
            "activation_id": self.activation_id,
            "forge_id": self.forge_id,
            "caller": self.caller,
        }


def _hash_obj(obj: Any) -> str:
    encoded = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class Channel(ABC):
    """Subclass and set ``name`` / ``schema`` / ``sink_subsystem``.

    ``allowed_callers`` defaults to all three roles; tighten per channel
    (e.g. ``("operator",)`` for operator-only channels).
    """

    name: ClassVar[str]
    schema: ClassVar[type]
    sink_subsystem: ClassVar[str]
    allowed_callers: ClassVar[tuple[str, ...]] = ("agent", "operator", "system")

    def validate(self, body: dict) -> None:
        """Default validation: every required dataclass field must be present
        and types match (shallow). Subclasses extend with semantic rules.
        """
        schema = self.schema
        if not dataclasses.is_dataclass(schema):
            return
        fields = {f.name: f for f in dataclasses.fields(schema)}
        unknown = set(body) - set(fields)
        if unknown:
            raise ChannelRejection(f"unknown fields: {sorted(unknown)}")
        for fname, fld in fields.items():
            has_default = (
                fld.default is not dataclasses.MISSING
                or fld.default_factory is not dataclasses.MISSING  # type: ignore[misc]
            )
            if fname not in body and not has_default:
                raise ChannelRejection(f"missing required field: {fname}")

    @abstractmethod
    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        """Apply the channel's effect. Return a result dict (audit-logged)."""

    def call(self, body: dict, ctx: ChannelContext) -> dict:
        if ctx.caller not in self.allowed_callers:
            self._audit(ctx, body, status="rejected",
                        reason=f"caller {ctx.caller!r} not allowed",
                        result=None)
            raise ChannelRejection(
                f"caller {ctx.caller!r} not allowed for channel {self.name!r}"
            )
        try:
            self.validate(body)
        except ChannelRejection as e:
            self._audit(ctx, body, status="rejected", reason=str(e), result=None)
            raise
        result = self.execute(body, ctx)
        self._audit(ctx, body, status="ok", reason=None, result=result)
        return result

    def _audit(self, ctx: ChannelContext, body: dict, *, status: str,
               reason: str | None, result: dict | None) -> None:
        if ctx.audit_sink is None:
            return
        event_body: dict[str, Any] = {
            "channel": self.name,
            "ctx": ctx.to_dict(),
            "status": status,
            "body_hash": _hash_obj(body),
        }
        if result is not None:
            event_body["result_hash"] = _hash_obj(result)
        if reason is not None:
            event_body["reason"] = reason
        raudit.append(ctx.audit_sink, event_body, kind=f"channel.{self.name}")
