"""Policy Decision dataclass — shared wire shape between supervisor and
guards. The old ``evaluate()`` function (walls + overrides + blackholes
layering) has been retired; Bulwark is the only policy source.

Kept as a thin module so external imports of ``from ara.policy import
Decision`` keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Decision:
    verdict: str                  # "allow" | "deny" | "approval"
    reason: Optional[str] = None
    rule_id: Optional[str] = None
    touched: Optional[str] = None
    approval_pattern: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"verdict": self.verdict}
        if self.reason:
            d["reason"] = self.reason
        if self.rule_id:
            d["rule_id"] = self.rule_id
        if self.touched:
            d["touched"] = self.touched
        if self.approval_pattern:
            d["approval_pattern"] = self.approval_pattern
        return d


def evaluate(walls, msg: dict, *, override_store=None) -> Decision:
    """Legacy entry point — now delegates to Bulwark if a ``bulwark.md``
    exists at the caller's state root, otherwise returns allow.

    Kept so ``ara explain`` keeps working without a rewrite. ``walls``
    and ``override_store`` are ignored; Bulwark covers both.
    """
    # Locate state_root — callers of this legacy function typically pass
    # walls and store constructed from a specific state root. We best-
    # effort discover via cwd + default. For correct results, callers
    # should migrate to ``bulwark.evaluate`` directly.
    from . import bulwark as bulwark_mod
    state_root = Path.cwd() / ".ara"
    bw_path = state_root / "bulwark.md"
    if not bw_path.exists():
        return Decision(verdict="allow", reason="NO_BULWARK")
    try:
        bw = bulwark_mod.parse_file(bw_path)
    except Exception:  # noqa: BLE001
        return Decision(verdict="allow", reason="BULWARK_PARSE_FAIL")
    # Map msg → event (copy of supervisor's shim)
    op = str(msg.get("op") or "")
    layer_for_op = {
        "fs.read": "fs", "fs.write": "fs", "fs.unlink": "fs",
        "fs.rename": "fs", "fs.stat": "fs", "fs.open": "fs",
        "fs.mkdir": "nav", "fs.rmdir": "nav", "fs.chdir": "nav",
        "fs.chmod": "perm", "fs.chown": "perm",
        "exec.spawn": "exec", "net.connect": "net",
    }.get(op)
    if not layer_for_op:
        return Decision(verdict="allow", reason="UNHANDLED_OP")
    event: dict[str, Any] = {"layer": layer_for_op}
    if layer_for_op in ("fs", "nav", "perm"):
        event["path"] = str(msg.get("path") or "")
    elif layer_for_op == "exec":
        event["path"] = str(msg.get("command") or "")
        event["argv"] = " ".join(str(a) for a in (msg.get("args") or []))
    elif layer_for_op == "net":
        event["host"] = str(msg.get("host") or "")
        event["port"] = msg.get("port")
    d = bulwark_mod.evaluate(bw, event)
    if d.rule is None:
        return Decision(verdict="allow", reason="DEFAULT_ALLOW")
    verdict_map = {
        "deny": "deny", "allow": "allow", "approval": "approval",
    }
    return Decision(
        verdict=verdict_map.get(d.action, "allow"),
        reason=f"BULWARK_{d.action.upper()}",
        rule_id=d.rule.rule_id,
        touched=event.get("path") or event.get("host") or "",
    )
