"""Mechanical danger classification for target actions.

Two tiers of danger, matched by conservative token patterns against the
op's target (URL/nav path segments, or exec command basename):

  - ``irreversible`` — destroys state that cannot be recovered without
    performing the act itself (factory reset, disk format, firmware flash,
    data wipe). Safety is logically *unprovable* without doing it, so these
    are ALWAYS operator-gated: hard-denied, auto-added to the armor bulwark,
    and never auto-granted on a timeout.
  - ``provable`` — state-changing but potentially scoped/reversible (delete,
    disable, reboot, reset). These may be cleared by crumb-backed evidence
    (or an operator), but are never *blind* auto-granted on a timeout.

Everything else is ``none`` (routine) and is eligible for timeout auto-grant.

Design rules that keep false positives out of the agent's routine work:
  - Network/nav targets are matched on path SEGMENTS split into tokens
    (``/cgi-bin/factoryreset`` -> ``factoryreset``), never on the raw string
    or query params, so ``?format=json`` does not trip ``format``.
  - exec.spawn is classified on the COMMAND BASENAME only, never on argv
    keywords — the agent reaches targets by spawning ``bash``/``python`` with
    a URL in the args, and scanning those args would mis-flag every routine
    recon step. The destructive *network* request it makes is caught at the
    URL layer instead; genuinely destructive *binaries* are caught here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

NONE = "none"
PROVABLE = "provable"
IRREVERSIBLE = "irreversible"

# Single path-segment tokens that on their own denote an irreversible action.
# Kept tight: each is an unambiguous data-destruction verb, rarely an
# incidental/benign endpoint name. Firmware/sysupgrade/reflash are
# deliberately NOT here — those endpoints are browsable and often
# recoverable, so they are left for the operator/judge, not hard-blocked.
_IRREVERSIBLE_TOKENS = frozenset({
    "factoryreset", "factorydefault", "factorydefaults", "factoryrestore",
    "hardreset", "restoredefault", "restoredefaults", "restorefactory",
    "mkfs", "wipefs", "secureerase", "zerofill", "reformat",
})
# Two tokens that must co-occur in the path to flag — catches ``factory-reset``
# / ``disk_erase`` without flagging the common single words on their own.
_IRREVERSIBLE_COMBOS = (
    ("factory", "reset"), ("factory", "default"), ("factory", "restore"),
    ("disk", "format"), ("disk", "erase"), ("nand", "erase"),
    ("flash", "erase"), ("secure", "erase"), ("full", "wipe"),
    ("data", "wipe"),
)

# A GET to a static asset is never a destructive action; skip classification
# so endpoint tokens inside asset names (e.g. ``reset.css``) don't flag.
_STATIC_EXT = frozenset({
    "css", "js", "mjs", "map", "png", "jpg", "jpeg", "gif", "svg", "ico",
    "webp", "bmp", "woff", "woff2", "ttf", "eot", "otf",
})

# State-changing but potentially reversible/scoped — require proof, not deny.
_PROVABLE_TOKENS = frozenset({
    "delete", "remove", "unlink", "drop", "truncate", "purge", "destroy",
    "disable", "deactivate", "revoke", "reset", "reboot", "restart",
    "shutdown", "poweroff", "halt", "setprop", "downfile",
})

# exec command basenames that are inherently destructive, beyond the armor's
# standing rm/dd/chmod/chown denies. Matched on the basename (mkfs.ext4 ->
# mkfs), never on argv.
_IRREVERSIBLE_COMMANDS = frozenset({
    "mkfs", "mke2fs", "wipefs", "sgdisk", "fdisk", "parted", "shred",
    "blkdiscard", "nvme",
})

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class DangerFlag:
    tier: str        # NONE | PROVABLE | IRREVERSIBLE
    target: str      # the classified target string (path or command basename)
    pattern: str = ""  # matched token/combo/command ("" when tier is NONE)
    reason: str = ""   # human-readable why ("" when tier is NONE)
    layer: str = ""    # op type, bulwark-style: "net" | "exec" | "" (n/a)
    method: str = ""   # HTTP method for the net layer, "" otherwise

    @property
    def is_danger(self) -> bool:
        return self.tier != NONE


def _segments(path: str) -> list[list[str]]:
    """Path -> list of segments, each split into lowercase alnum tokens."""
    out: list[list[str]] = []
    for seg in PurePosixPath(str(path or "")).parts:
        toks = [t for t in _TOKEN_SPLIT.split(seg.lower()) if t]
        if toks:
            out.append(toks)
    return out


def classify_path(path: str, method: str = "") -> DangerFlag:
    """Classify a URL/nav path (a network action) by its segments; query
    string ignored. Every result carries ``layer='net'`` and the request
    method. Irreversible combos/tokens are matched against the path-wide token
    union; static-asset requests short-circuit to NONE.
    """
    m = str(method or "").upper()
    raw_last = PurePosixPath(str(path or "")).name.lower()
    if "." in raw_last and raw_last.rsplit(".", 1)[-1] in _STATIC_EXT:
        return DangerFlag(NONE, str(path or ""), layer="net", method=m)
    segs = _segments(path)
    alltok: set[str] = set()
    for toks in segs:
        alltok.update(toks)
    for a, b in _IRREVERSIBLE_COMBOS:
        if a in alltok and b in alltok:
            return DangerFlag(
                IRREVERSIBLE, path, f"{a}+{b}",
                f"irreversible endpoint token '{a} {b}'", "net", m,
            )
    hit = alltok & _IRREVERSIBLE_TOKENS
    if hit:
        p = sorted(hit)[0]
        return DangerFlag(
            IRREVERSIBLE, path, p, f"irreversible endpoint token '{p}'",
            "net", m,
        )
    hit = alltok & _PROVABLE_TOKENS
    if hit:
        p = sorted(hit)[0]
        return DangerFlag(
            PROVABLE, path, p, f"state-changing endpoint token '{p}'",
            "net", m,
        )
    return DangerFlag(NONE, str(path or ""), layer="net", method=m)


def classify_url(url: str, method: str = "") -> DangerFlag:
    try:
        path = urlsplit(str(url or "")).path or str(url or "")
    except Exception:  # noqa: BLE001
        path = str(url or "")
    return classify_path(path, method)


def classify_command(command) -> DangerFlag:
    base = PurePosixPath(str(command or "")).name.lower()
    root = base.split(".", 1)[0]
    if base in _IRREVERSIBLE_COMMANDS or root in _IRREVERSIBLE_COMMANDS:
        return DangerFlag(
            IRREVERSIBLE, base, base, f"irreversible command '{base}'", "exec",
        )
    return DangerFlag(NONE, base, layer="exec")


def classify_event(op: str, msg: dict) -> DangerFlag:
    """Classify a guard/MITM message. Only ACTIONABLE layers are classified —
    an outbound target request (net) or an exec spawn (exec). File reads,
    directory listings, and other fs/nav ops are never classified: reading or
    enumerating something whose name contains a danger word never *triggers*
    anything.
    """
    op = str(op or "")
    method = str(msg.get("method") or "").upper()
    if msg.get("url") or op in ("browser.nav", "rhema.request", "net.request"):
        return classify_url(
            str(msg.get("url") or msg.get("path") or ""), method)
    if op == "exec.spawn":
        return classify_command(msg.get("command"))
    return DangerFlag(NONE, "")


def is_auto_grant_eligible(op: str, msg: dict) -> bool:
    """True when an approval timeout may be auto-granted for this op —
    i.e. it is routine (not danger-classified)."""
    return classify_event(op, msg).tier == NONE


__all__ = [
    "NONE", "PROVABLE", "IRREVERSIBLE", "DangerFlag",
    "classify_path", "classify_url", "classify_command",
    "classify_event", "is_auto_grant_eligible",
]
