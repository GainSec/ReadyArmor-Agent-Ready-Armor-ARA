"""The minimal cross-cutting hardening temper — a hand-authored, high-signal
default instead of forged filler.

Reviewing full temper forges over pentest material showed the valuable part of a
temper is a small, task-INDEPENDENT set of doctrines: output hygiene (don't leak
secrets), standing safety principles, and destructive-action gates. That set is
not derivable per-element from pentest PDFs (which describe target weaknesses —
weapon/knowledge material), so it is authored once here.

Which tools an agent MAY run is deliberately NOT here — that is the weapon's
allowlist scoped by the trim, not armor doctrine. This temper only hardens how
the agent behaves regardless of the task.

Install with :func:`install_baseline_temper` (writes its own policy files via
``tempers.ingest``); equip/unequip like any temper. Reversible and inert until
equipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import tempers

BASELINE_TEMPER_ID = "baseline-hardening"
BASELINE_TEMPER_NAME = "Baseline Agent Hardening"


def _c(destination, thread_type, title, rationale, risk, proposed_text, control,
       scope=("global",)):
    return {
        "destination": destination, "thread_type": thread_type, "title": title,
        "rationale": rationale, "risk": risk, "proposed_text": proposed_text,
        "control": control, "scope": list(scope),
        "default_decision": "keep_for_review", "source": "baseline-hardening",
        "citations": [{"manifest_element_id": "baseline"}],
    }


# The doctrines. Grouped: output hygiene, safety doctrine, destructive gates.
BASELINE_DOCTRINES = [
    # --- output hygiene (enforced canaries + an advisory redaction) ---
    _c("canary", "secret_handling", "Credential-shaped output tripwire",
       "The agent must not surface raw credentials it encounters.",
       "Agent echoes a discovered password/API key/token in its reply.",
       "Trip when a reply contains credential-shaped material; report presence only.",
       {"signal": "credential_in_output",
        "pattern": r"(?i)(password|passwd|api[_-]?key|secret[_-]?key|token|bearer)\s*[:=]\s*\S{6,}",
        "severity": "high", "trip_when": "reply contains a key/value credential",
        "response": "redact + flag for operator"}),
    _c("canary", "secret_handling", "Private-key material tripwire",
       "Raw private keys must never appear in agent output.",
       "Agent pastes a PEM private-key block into a reply or log.",
       "Trip on PEM private-key headers in output.",
       {"signal": "private_key_in_output",
        "pattern": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
        "severity": "high", "trip_when": "reply contains a private-key block",
        "response": "redact + flag"}),
    _c("canary", "secret_handling", "Cloud access-key tripwire",
       "Cloud provider keys are high-value secrets the agent must not disclose.",
       "Agent reveals an AWS/GCP-shaped access key.",
       "Trip on cloud access-key shapes.",
       {"signal": "cloud_key_in_output",
        "pattern": r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35})",
        "severity": "high", "trip_when": "reply contains a cloud access key",
        "response": "redact + flag"}),
    _c("rhema", "prompt_mediation", "Redact secret-shaped reply content",
       "Reply mediation redacts secret-shaped bytes before they reach the operator/log.",
       "Agent output leaks a token or key inline.",
       "Rewrite credential-shaped byte strings in replies to a placeholder.",
       {"action": "rewrite", "layer": "reply",
        "pattern": r"(?i)(?:bearer\s+[a-z0-9._\-]{20,}|(?:api[_-]?key|secret|token)\s*[:=]\s*\S{8,})",
        "replacement": "[REDACTED_SECRET]",
        "rationale": "Report secret presence, not raw values."}),
    # --- safety doctrine (prompt-level absolutes) ---
    _c("absolute", "safety_boundary", "Never disclose raw secrets",
       "Standing doctrine: report the presence and location of secrets, never the raw value.",
       "Agent discloses raw credential/key/token material.",
       "You must never output raw secret values (passwords, keys, tokens); report only that a secret exists and where.",
       {"principle": "Report secret presence and location; never disclose raw secret values.",
        "allowed": ["presence", "location", "type", "risk"],
        "restricted": ["raw secret values"],
        "exception_path": "Operator explicitly approves a single disclosure with justification."}),
    _c("absolute", "safety_boundary", "Keep evidence within the engagement workspace",
       "Standing doctrine against exfiltration.",
       "Agent uploads or sends target data/evidence outside the sanctioned workspace.",
       "Do not send, upload, or copy target data or evidence outside the engagement workspace and approved delivery channels.",
       {"principle": "No exfiltration of target data/evidence outside the sanctioned workspace.",
        "allowed": ["local workspace", "approved delivery channels"],
        "restricted": ["external uploads", "pastebins", "personal storage"],
        "exception_path": "Operator approves a specific destination."}),
    _c("absolute", "safety_boundary", "Default to read-only on the target",
       "Standing doctrine: don't mutate target state without cause.",
       "Agent modifies or destroys target data during what should be assessment.",
       "Treat the target as read-only by default; do not modify, delete, or overwrite target data or configuration without explicit authorization.",
       {"principle": "Read-only by default; mutation of target state requires authorization.",
        "allowed": ["read", "observe", "enumerate"],
        "restricted": ["write", "delete", "overwrite target state"],
        "exception_path": "Trim/operator authorizes active/destructive testing."}),
    # --- destructive-action gates (enforced approvals) ---
    _c("approval_gate", "operator_approval", "Gate irreversible device operations",
       "Bricking/wiping a device is irreversible; require a human before it.",
       "Agent flashes/wipes/factory-resets a device unprompted.",
       "Require operator approval before any irreversible device operation.",
       # exec triggers must NOT contain '/', or bulwark scopes them to the
       # binary path and never inspects argv. Match command/arg tokens instead.
       {"surface": "exec",
        "trigger": r"(?i)(\bwrite_flash\b|\berase_flash\b|factory[_-]?reset|fastboot\s+(?:flash|erase|-w)|\bmkfs\b)",
        "required_context": "operator approves the specific irreversible operation",
        "approval_prompt": "Approve an IRREVERSIBLE device/disk operation?"}),
    _c("approval_gate", "operator_approval", "Gate destructive filesystem operations",
       "Bulk-destructive filesystem ops need a human.",
       "Agent runs rm -rf / dd / format that destroys data.",
       "Require operator approval before bulk-destructive filesystem operations.",
       # no '/' in the trigger (see note above) — match the destructive verb.
       {"surface": "exec",
        "trigger": r"(?i)(\brm\s+-rf\b|\bshred\b|\bdd\b[^\n]*\bof=|\bmkfs\b)",
        "required_context": "operator approves the destructive command",
        "approval_prompt": "Approve a DESTRUCTIVE filesystem operation?"}),
    _c("approval_gate", "operator_approval", "Gate target credential/account changes",
       "Changing target auth state is high-impact and often out of scope.",
       "Agent changes a password, adds/removes a user, or rotates keys on the target.",
       "Require operator approval before modifying any target account or credential.",
       {"surface": "exec",
        "trigger": r"(?i)\b(passwd|useradd|userdel|usermod|net\s+user|chpasswd|adduser|deluser)\b",
        "required_context": "operator approves the account/credential change",
        "approval_prompt": "Approve modifying a target account/credential?"}),
]


@dataclass
class _BaselineItem:
    """Minimal TemperItem-shaped object for tempers.ingest / plan_item."""
    destination: str
    control: dict
    title: str = ""
    proposed_text: str = ""
    citations: list = field(default_factory=list)


def baseline_items() -> list:
    return [
        _BaselineItem(
            destination=d["destination"], control=dict(d["control"]),
            title=d["title"], proposed_text=d["proposed_text"],
            citations=list(d["citations"]),
        )
        for d in BASELINE_DOCTRINES
    ]


def install_baseline_temper(state_root: Path, *, equip: bool = False) -> dict:
    """Materialize the baseline hardening temper into its own policy files.
    Optionally equip it. Idempotent (re-ingest overwrites its own files)."""
    meta = tempers.ingest(
        state_root, BASELINE_TEMPER_ID, baseline_items(),
        name=BASELINE_TEMPER_NAME, source_forge=None,
    )
    if equip:
        tempers.equip(state_root, BASELINE_TEMPER_ID)
    return meta
