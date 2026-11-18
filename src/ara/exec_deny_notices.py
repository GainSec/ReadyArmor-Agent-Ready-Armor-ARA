"""One-shot queue bridging Bulwark exec-denies to the MITM proxy.

When a ``deny exec`` rule hard-blocks a command, ``exec_approvals_proxy`` still
returns the hard deny to openclaw (unchanged), and *additionally* appends a
notice here. On the agent's next model turn the MITM proxy drains the queue and
injects an agent-facing "you hit X, it's denied — skip and continue" system
message, so the agent stops re-issuing the blocked command instead of thrashing
against a silent block.

All I/O is best-effort: every function swallows its own errors and degrades to a
no-op / empty result. The security deny must never depend on this feedback path
working.

The queue lives at ``<state_root>/exec_deny_notices.jsonl`` — the run-scoped
state root that both the exec-approvals proxy (writer) and the MITM proxy
(reader) already hold. ARA runs one contained agent per state root, so the queue
is unambiguous.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

QUEUE_NAME = "exec_deny_notices.jsonl"

# Runaway guard: never drain (or list) more than this many records in one turn.
_MAX_RECORDS = 500
# Distinct matched terms named in the composed message before "…and N more".
_MAX_TOKENS_SHOWN = 8


def _queue_path(state_root: Path) -> Path:
    return Path(state_root) / QUEUE_NAME


def append(
    state_root: Path,
    *,
    rule_id: str,
    matched_token: str,
    cmd_summary: str,
    ts: float,
) -> None:
    """Append one deny notice. Best-effort — never raises."""
    rec = {
        "rule_id": str(rule_id or ""),
        "matched_token": str(matched_token or ""),
        "cmd_summary": str(cmd_summary or "")[:200],
        "ts": ts,
    }
    try:
        with _queue_path(state_root).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass


def drain(state_root: Path) -> list[dict]:
    """Read all queued notices and remove the file (one-shot).

    Best-effort: returns ``[]`` on any error. On success the file is deleted so
    each notice fires exactly once. Malformed lines are skipped; at most
    ``_MAX_RECORDS`` records are returned.
    """
    try:
        p = _queue_path(state_root)
        if not p.exists():
            return []
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        finally:
            # One-shot: remove even if parsing below fails, so a poison file
            # can't wedge the queue permanently.
            try:
                p.unlink()
            except OSError:
                pass
        out: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
                if len(out) >= _MAX_RECORDS:
                    break
        return out
    except Exception:  # noqa: BLE001
        return []


def compose_message(notices: list[dict]) -> str:
    """Compose the agent-facing system message from drained notices.

    Returns ``""`` when there is nothing actionable. Distinct matched terms are
    coalesced; duplicates from the same term retried across denies collapse to
    one entry.
    """
    if not notices:
        return ""
    tokens: list[str] = []
    seen: set[str] = set()
    rule_ids: list[str] = []
    for n in notices:
        if not isinstance(n, dict):
            continue
        rid = str(n.get("rule_id") or "").strip()
        if rid and rid not in rule_ids:
            rule_ids.append(rid)
        tok = str(n.get("matched_token") or "").strip()
        if tok and tok.lower() not in seen:
            seen.add(tok.lower())
            tokens.append(tok)

    rule_disp = rule_ids[0] if rule_ids else ""
    rule_clause = f" (bulwark rule `{rule_disp}`)" if rule_disp else ""

    if tokens:
        shown = tokens[:_MAX_TOKENS_SHOWN]
        extra = len(tokens) - len(shown)
        term_list = ", ".join(f"`{t}`" for t in shown)
        if extra > 0:
            term_list += f" …and {extra} more"
        label = "term" if (len(shown) == 1 and extra == 0) else "terms"
        term_clause = f" on the {label} {term_list}"
    else:
        term_clause = ""

    return (
        "⚠ Engagement policy blocked your previous command"
        f"{rule_clause}. It matched a destructive-op guardrail{term_clause}. "
        "Commands or requests involving reboot / factory-reset / flash / erase "
        "against the target are out of scope for this engagement and will "
        "always be denied — retrying them only wastes turns. Skip that action "
        "and continue with other in-scope work."
    )


def extract_matched_token(pattern: str, is_regex: bool, command: str) -> str:
    """Return the substring of ``command`` the deciding rule matched.

    Lowercased and trimmed, capped to 60 chars. Returns ``""`` when nothing is
    recoverable. For regex rules the first non-empty capture group is preferred
    (so a wordlist that trips ``(...\\breboot\\b...)`` yields ``reboot``, not the
    whole 80-path blob); for literal/glob rules the primary is returned when it
    appears verbatim.
    """
    if not command:
        return ""
    try:
        if is_regex and pattern:
            m = re.search(pattern, command)
            if m:
                grp = ""
                for g in (m.groups() or ()):
                    if g:
                        grp = g
                        break
                text = grp or m.group(0) or ""
                return text.strip().lower()[:60]
        elif pattern and str(pattern) in command:
            return str(pattern).strip().lower()[:60]
    except Exception:  # noqa: BLE001
        return ""
    return ""
