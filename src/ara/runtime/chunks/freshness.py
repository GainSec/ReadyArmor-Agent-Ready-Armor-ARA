"""Finding-freshness validator (referenced from 1.4).

A finding is "fresh" if at least one of its evidence_refs:
  (a) was captured during the current activation_id, OR
  (b) was accessed (evidence.get / evidence.search hit) during the
      current activation, per audit log.

In v1, (a) is a hard signal from binder metadata; (b) is supported by
scanning the session's events.jsonl for ``channel.evidence.get`` /
``channel.evidence.search`` audit entries with the current activation
in ctx.
"""
from __future__ import annotations

import json
from pathlib import Path

from ara.runtime.binder.api import EvidenceBinder


def evidence_in_current_activation(
    finding_body: dict,
    *,
    binder: EvidenceBinder,
    audit_log_path: Path | None,
    current_activation_id: str,
) -> bool:
    refs = list(finding_body.get("evidence_refs") or [])
    if not refs:
        return False
    for ev_id in refs:
        rec = binder.get_record(ev_id)
        if rec is not None and rec.captured_during_activation_id == current_activation_id:
            return True
    if audit_log_path and audit_log_path.exists():
        with audit_log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                body = d.get("body") or {}
                ctx = body.get("ctx") or {}
                if ctx.get("activation_id") != current_activation_id:
                    continue
                ch = body.get("channel") or ""
                if ch in ("evidence.get", "evidence.search"):
                    return True
    return False
