"""Bulwark — unified policy rule layer.

Parses ``bulwark.md`` into a set of scoped rules and evaluates them against
runtime events (net/fs/nav/perm/exec/url/oc). Replaces WALLS (syscall) +
Allowlist (overrides/blackholes) + Rhema-as-rules.

See ``docs/BULWARK-SPEC.md`` for the full spec.

Rule syntax::

    <action>[ <modifier>]* <layer>[:<sublayer>] <match...>  [# note]

Section headers define scope::

    ## global
    ## armor: orion
    ## armor: orion+atlas
    ## armor: *-atlas
    ## global -armor: orion+atlas
    ## config

Precedence inside each scope is file order (top = highest priority). Across
scopes: global deny → armor deny → global approval → armor approval →
global allow → armor allow → default.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------

_VALID_ACTIONS = {"allow", "deny", "approval", "rewrite", "shadow_rewrite"}
_VALID_LAYERS = {
    "net", "fs", "nav", "perm", "exec", "url", "env", "ask", "reply",
    "acp",   # OpenClaw ACP method names (e.g. "chat.inject", "cron.add")
}
_COUNTER_EXHAUST_MODES = {"deny", "allow", "approval", "fallthrough"}


@dataclass(frozen=True)
class ScopeExpr:
    """Parsed scope expression from a section header.

    ``include_global`` — rule applies to sessions regardless of armor.
    ``include_armors`` — explicit set of armor IDs the rule applies to
        (or ``{"*"}`` meaning "any armor").
    ``exclude_armors`` — armor IDs to subtract from the include set.
    """
    include_global: bool
    include_armors: frozenset[str]
    exclude_armors: frozenset[str]
    raw: str


@dataclass(frozen=True)
class Match:
    """Layer-specific match target. Only the fields relevant to the layer
    are populated; the rest stay ``None``."""
    # Common: every match has a primary pattern string used for display.
    primary: str
    is_regex: bool = False
    # Layer extras:
    host: str | None = None        # net, url
    port: str | None = None        # net
    method: str | None = None      # url
    path: str | None = None        # fs, nav, perm, exec, url
    op: str | None = None          # perm (chmod/chattr/setuid/...)
    argv: str | None = None        # exec
    tool: str | None = None        # oc:<tool> — the sublayer
    target: str | None = None      # oc:<tool> match body


@dataclass(frozen=True)
class Modifiers:
    ttl_seconds: int | None = None
    max_hits: int | None = None            # from xN
    then_mode: str | None = None           # overrides counter_exhaust_default
    highlight: bool = False
    rewrite_spec: dict[str, Any] | None = None   # for action=rewrite/shadow_rewrite


@dataclass(frozen=True)
class Rule:
    rule_id: str
    scope: ScopeExpr
    layer: str                  # "net" | "fs" | "nav" | "perm" | "exec" | "url" | "oc:<tool>"
    action: str                 # "allow" | "deny" | "approval" | "rewrite" | "shadow_rewrite"
    match: Match
    modifiers: Modifiers
    lineno: int
    raw: str
    note: str | None = None
    created_at: float = 0.0     # unix ts, populated at parse time (for ttl)


@dataclass
class Config:
    counter_exhaust_default: str = "deny"   # member of _COUNTER_EXHAUST_MODES


@dataclass
class Bulwark:
    rules: tuple[Rule, ...] = ()
    config: Config = field(default_factory=Config)
    parse_errors: tuple[str, ...] = ()
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# Stateful runtime (counters + expired rule ids)
# ---------------------------------------------------------------------------

@dataclass
class RuntimeState:
    """Per-process mutable state that parallels the immutable rule list.

    ``hits`` — per-rule-id monotonically increasing fire count.
    ``disabled`` — rule_ids that have been retired (counter exhausted or TTL
        elapsed). Evaluated lazily.
    """
    hits: dict[str, int] = field(default_factory=dict)
    disabled: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_TTL_UNIT = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_XN = re.compile(r"^x(\d+)$", re.IGNORECASE)
_THEN = re.compile(r"^then:(\w+)$", re.IGNORECASE)


def parse(text: str, *, source: Path | None = None) -> Bulwark:
    rules: list[Rule] = []
    errors: list[str] = []
    cfg = Config()
    now = time.time()
    scope: ScopeExpr | None = None
    in_config = False
    counter = 0

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        # Heading?
        m = _HEADING.match(line)
        if m:
            header = m.group(1).strip()
            if header == "config":
                in_config = True
                scope = None
                continue
            in_config = False
            try:
                scope = _parse_scope_expr(header)
            except ValueError as exc:
                errors.append(f"line {lineno}: bad scope `{header}`: {exc}")
                scope = None
            continue
        # Comment lines at section level
        if line.startswith("#") and not _HEADING.match(line):
            continue
        # Config body
        if in_config:
            try:
                _apply_config_line(cfg, line)
            except ValueError as exc:
                errors.append(f"line {lineno}: {exc}")
            continue
        # Rule line outside any scope?
        if scope is None:
            errors.append(f"line {lineno}: rule before any ## scope header")
            continue
        # Parse rule
        try:
            rule_body, note = _split_note(line)
            rule = _parse_rule(rule_body, scope, lineno, raw, note, now)
        except ValueError as exc:
            errors.append(f"line {lineno}: {exc}")
            continue
        counter += 1
        rule = _with_id(rule, f"r{counter}")
        rules.append(rule)

    return Bulwark(
        rules=tuple(rules),
        config=cfg,
        parse_errors=tuple(errors),
        source_path=source,
    )


def parse_file(path: Path) -> Bulwark:
    return parse(path.read_text(), source=path)


def _split_note(line: str) -> tuple[str, str | None]:
    """Strip a trailing ``# note`` (not to be confused with a leading ``#``
    which is a whole-line comment and would have been filtered already)."""
    # Respect escaped `#` by finding only unquoted hashes after at least one
    # non-space char.
    out = []
    i = 0
    in_single = False
    in_double = False
    while i < len(line):
        c = line[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "#" and not in_single and not in_double:
            body = "".join(out).rstrip()
            note = line[i + 1 :].strip() or None
            return body, note
        out.append(c)
        i += 1
    return line, None


def _apply_config_line(cfg: Config, line: str) -> None:
    if ":" not in line:
        raise ValueError(f"config line missing ':': {line!r}")
    key, _, val = line.partition(":")
    key = key.strip().lower()
    val = val.strip().lower()
    if key == "counter_exhaust_default":
        if val not in _COUNTER_EXHAUST_MODES:
            raise ValueError(
                f"counter_exhaust_default must be one of "
                f"{sorted(_COUNTER_EXHAUST_MODES)}, got {val!r}"
            )
        cfg.counter_exhaust_default = val
    else:
        raise ValueError(f"unknown config key: {key!r}")


def _parse_scope_expr(header: str) -> ScopeExpr:
    """Parse ``global``, ``armor: a+b-c``, ``global -armor: x+y``, etc."""
    raw = header
    head, _, rest = header.partition(" ")
    head = head.strip().lower().rstrip(":")
    rest = rest.strip()
    include_global = False
    include_armors: set[str] = set()
    exclude_armors: set[str] = set()

    if head == "global":
        include_global = True
        if rest:
            if not rest.startswith("-armor"):
                raise ValueError(
                    f"after `global`, only `-armor: <ids>` is allowed; got {rest!r}"
                )
            # strip "-armor:" prefix
            _, _, ids = rest.partition(":")
            for tok, sign in _split_armor_expr(ids.strip()):
                if sign == "+":
                    exclude_armors.add(tok)
                else:
                    include_armors.add(tok)
    elif head == "armor":
        if not rest:
            raise ValueError("`armor:` needs at least one id")
        for tok, sign in _split_armor_expr(rest):
            if sign == "+":
                include_armors.add(tok)
            else:
                exclude_armors.add(tok)
    else:
        raise ValueError(f"scope must be `global` or `armor: ...`; got {header!r}")

    return ScopeExpr(
        include_global=include_global,
        include_armors=frozenset(include_armors),
        exclude_armors=frozenset(exclude_armors),
        raw=raw,
    )


def _split_armor_expr(expr: str) -> list[tuple[str, str]]:
    """Split armor scope ids.

    ``+`` separates ids. Hyphens are valid inside armor ids (for example
    ``hermes-default``), so ``-`` is only treated as exclusion for the
    historical wildcard shorthand ``*-atlas`` or an explicit token like
    ``+-atlas``. ``*`` is allowed as a wildcard id.
    """
    expr = expr.strip()
    if expr.startswith("*-"):
        rest = expr[2:].strip()
        if not rest:
            raise ValueError(f"empty armor expression: {expr!r}")
        out = [("*", "+")]
        out.extend((tok.strip(), "-") for tok in rest.split("+") if tok.strip())
        return out

    out: list[tuple[str, str]] = []
    for raw_tok in expr.split("+"):
        tok = raw_tok.strip()
        if not tok:
            continue
        if tok.startswith("-"):
            tok = tok[1:].strip()
            sign = "-"
        else:
            sign = "+"
        if tok:
            out.append((tok, sign))
    # Drop empties, reject stray syntax
    cleaned = [(tok, sgn) for tok, sgn in out if tok]
    if not cleaned:
        raise ValueError(f"empty armor expression: {expr!r}")
    return cleaned


def _parse_rule(
    body: str,
    scope: ScopeExpr,
    lineno: int,
    raw: str,
    note: str | None,
    now: float,
) -> Rule:
    tokens = body.split()
    if not tokens:
        raise ValueError("empty rule line")
    action = tokens[0].lower()
    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"unknown action `{action}` (allowed: {sorted(_VALID_ACTIONS)})"
        )
    idx = 1

    # Collect modifiers until we see a layer token. Modifiers: ttl <d>,
    # xN, then:<mode>, highlight. Layer detection: if token is a known
    # layer name or starts with ``oc:``, stop collecting modifiers.
    mods = _ModBuilder()
    while idx < len(tokens):
        tok = tokens[idx]
        tl = tok.lower()
        if tl in _VALID_LAYERS or tl.startswith("oc:") or tl == "oc":
            break
        # ttl takes a following duration arg
        if tl == "ttl":
            if idx + 1 >= len(tokens):
                raise ValueError("`ttl` missing duration")
            mods.set_ttl(tokens[idx + 1])
            idx += 2
            continue
        if _XN.match(tl):
            mods.set_count(int(_XN.match(tl).group(1)))
            idx += 1
            continue
        tm = _THEN.match(tl)
        if tm:
            mode = tm.group(1).lower()
            if mode not in _COUNTER_EXHAUST_MODES:
                raise ValueError(
                    f"`then:{mode}` — mode must be one of {sorted(_COUNTER_EXHAUST_MODES)}"
                )
            mods.set_then(mode)
            idx += 1
            continue
        if tl == "highlight":
            mods.set_highlight()
            idx += 1
            continue
        raise ValueError(f"unknown modifier or layer token: {tok!r}")

    if idx >= len(tokens):
        raise ValueError("missing layer after action/modifiers")
    layer_tok = tokens[idx].lower()
    idx += 1
    rest = tokens[idx:]

    if layer_tok == "oc" or layer_tok.startswith("oc:"):
        layer, match = _parse_oc(layer_tok, rest)
    elif layer_tok in _VALID_LAYERS:
        layer = layer_tok
        match = _parse_layer_match(layer, rest)
    else:
        raise ValueError(f"unknown layer: {layer_tok!r}")

    # Rewrite-action body is a JSON spec; today we accept a raw string in
    # the match slot and let the caller parse/apply. For now, only allow
    # rewrite/shadow_rewrite on oc:* layers (ask/reply-adjacent) and on
    # url (body mutation). The shape is validated at apply time.
    if action in {"rewrite", "shadow_rewrite"}:
        allowed = layer in ("url", "ask", "reply") or layer == "oc" or layer.startswith("oc:")
        if not allowed:
            raise ValueError(
                f"action `{action}` only valid on url/ask/reply/oc:* layers; "
                f"got {layer}"
            )

    return Rule(
        rule_id="",  # assigned by parse()
        scope=scope,
        layer=layer,
        action=action,
        match=match,
        modifiers=mods.build(),
        lineno=lineno,
        raw=raw,
        note=note,
        created_at=now,
    )


class _ModBuilder:
    def __init__(self) -> None:
        self.ttl: int | None = None
        self.count: int | None = None
        self.then: str | None = None
        self.hl: bool = False

    def set_ttl(self, token: str) -> None:
        self.ttl = _parse_duration(token)

    def set_count(self, n: int) -> None:
        if n <= 0:
            raise ValueError("xN must be positive")
        self.count = n

    def set_then(self, mode: str) -> None:
        self.then = mode

    def set_highlight(self) -> None:
        self.hl = True

    def build(self) -> Modifiers:
        return Modifiers(
            ttl_seconds=self.ttl,
            max_hits=self.count,
            then_mode=self.then,
            highlight=self.hl,
        )


def _parse_duration(tok: str) -> int:
    m = _TTL_UNIT.match(tok)
    if not m:
        raise ValueError(f"bad ttl `{tok}` (expected like 30s, 15m, 2h, 7d)")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _parse_layer_match(layer: str, rest: list[str]) -> Match:
    if not rest:
        raise ValueError(f"{layer}: missing match target")
    is_regex, tail = _strip_regex(rest)
    primary = " ".join(tail)
    if layer == "net":
        host, port = _split_host_port(primary)
        return Match(primary=primary, is_regex=is_regex, host=host, port=port)
    if layer in ("fs", "nav"):
        return Match(primary=primary, is_regex=is_regex, path=primary)
    if layer == "perm":
        # Optional `op=<name>` prefix token (kept inside primary for display).
        op = None
        if tail and tail[0].lower().startswith("op="):
            op = tail[0].split("=", 1)[1].strip()
            tail = tail[1:]
            if not tail:
                raise ValueError("perm: need path after op=")
            primary = " ".join(tail)
        return Match(primary=primary, is_regex=is_regex, path=primary, op=op)
    if layer == "exec":
        argv = None
        if tail and tail[0].lower().startswith("argv="):
            argv = tail[0].split("=", 1)[1].strip()
            tail = tail[1:]
            if not tail:
                raise ValueError("exec: need binary path after argv=")
            primary = " ".join(tail)
        return Match(primary=primary, is_regex=is_regex, path=primary, argv=argv)
    if layer == "env":
        # Match target is a var-name glob, case-sensitive.
        return Match(primary=primary, is_regex=is_regex, target=primary)
    if layer == "acp":
        # Match target is the ACP method name, e.g. "chat.inject",
        # "cron.*", "regex:^sessions\\..*". Params/body inspection is
        # best done with separate ask/reply rules against the same
        # frame's content.
        return Match(primary=primary, is_regex=is_regex, target=primary)
    if layer in ("ask", "reply"):
        # Content substring / regex against the LLM request (ask) or
        # response (reply) body. Globs here match on substring semantics
        # rather than whole-string, since prompts are long.
        return Match(primary=primary, is_regex=is_regex, target=primary)
    if layer == "url":
        method = None
        first = tail[0] if tail else ""
        if first.isupper() and first.isalpha() and 3 <= len(first) <= 8:
            method = first
            tail = tail[1:]
            if not tail:
                raise ValueError("url: need URL after method")
            primary = " ".join(tail)
        host, path = _split_url(primary if not is_regex else primary)
        return Match(
            primary=primary, is_regex=is_regex, method=method,
            host=host, path=path,
        )
    raise ValueError(f"internal: unhandled layer {layer}")


def _parse_oc(layer_tok: str, rest: list[str]) -> tuple[str, Match]:
    # layer_tok is either ``oc`` (family catch-all) or ``oc:<tool>``
    tool: str | None = None
    if layer_tok != "oc":
        _, _, tool = layer_tok.partition(":")
        if not tool:
            raise ValueError("oc: layer missing sublayer (e.g. oc:read)")
    if not rest:
        raise ValueError(f"{layer_tok}: missing match target")
    is_regex, tail = _strip_regex(rest)
    primary = " ".join(tail)
    layer = f"oc:{tool}" if tool else "oc"
    return layer, Match(
        primary=primary, is_regex=is_regex,
        tool=tool, target=primary,
    )


def _strip_regex(tokens: list[str]) -> tuple[bool, list[str]]:
    if not tokens:
        return False, tokens
    head = tokens[0]
    if head.lower().startswith("regex:"):
        tail = [head[len("regex:") :]] + tokens[1:]
        # Drop empty leading if regex: was attached loosely to next token.
        if tail and not tail[0]:
            tail = tail[1:]
        return True, tail
    return False, list(tokens)


def _split_host_port(s: str) -> tuple[str, str]:
    if ":" not in s:
        return s, "*"
    host, _, port = s.rpartition(":")
    return host or "*", port or "*"


def _split_url(s: str) -> tuple[str | None, str | None]:
    """Rough URL split — returns (host, path-or-glob). Doesn't need to be
    RFC-grade; MITM already knows host/path at match time."""
    # Accept scheme://host/... or host/path or just a glob.
    body = s
    if "://" in body:
        body = body.split("://", 1)[1]
    if "/" in body:
        host, _, path = body.partition("/")
        return host or None, "/" + path
    return body or None, None


def _with_id(rule: Rule, rid: str) -> Rule:
    return Rule(
        rule_id=rid, scope=rule.scope, layer=rule.layer, action=rule.action,
        match=rule.match, modifiers=rule.modifiers, lineno=rule.lineno,
        raw=rule.raw, note=rule.note, created_at=rule.created_at,
    )


# ---------------------------------------------------------------------------
# Scope evaluation
# ---------------------------------------------------------------------------

def scope_matches(scope: ScopeExpr, armor_id: str | None) -> bool:
    """True if a session running ``armor_id`` (None = no equipped armor) is
    covered by this scope expression."""
    # Explicit exclude short-circuits.
    if armor_id and armor_id in scope.exclude_armors:
        return False
    if scope.include_global:
        return True
    if not armor_id:
        return False
    if "*" in scope.include_armors:
        return True
    return armor_id in scope.include_armors


# ---------------------------------------------------------------------------
# Layer matching
# ---------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _compile(pattern: str, is_regex: bool) -> re.Pattern[str]:
    if is_regex:
        try:
            return re.compile(pattern)
        except re.error:
            return re.compile(re.escape(pattern))
    return _glob_to_regex(pattern)


def _str_match(pattern: str | None, is_regex: bool, candidate: str | None) -> bool:
    if not candidate or not pattern:
        return False
    compiled = _compile(pattern, is_regex)
    return bool(compiled.search(candidate) if is_regex else compiled.match(candidate))


_SHELL_BASENAMES = {"sh", "bash", "dash", "zsh", "ksh", "ash"}


def _exec_candidates(path: str, argv: str) -> set[str]:
    candidates = {path}
    basename = Path(path).name if path else ""
    if basename:
        candidates.add(basename)
    if path and argv:
        candidates.add(f"{path} {argv}")

    if basename in _SHELL_BASENAMES and argv:
        try:
            tokens = shlex.split(argv)
        except ValueError:
            tokens = argv.split()
        for flag in ("-c", "-lc"):
            if flag not in tokens:
                continue
            idx = tokens.index(flag)
            if idx + 1 >= len(tokens):
                continue
            inner = " ".join(tokens[idx + 1 :]).strip()
            if not inner:
                continue
            candidates.add(inner)
            try:
                inner_tokens = shlex.split(inner)
            except ValueError:
                inner_tokens = inner.split()
            if inner_tokens:
                inner_cmd = inner_tokens[0]
                candidates.add(inner_cmd)
                candidates.add(Path(inner_cmd).name)
            break
    return {c for c in candidates if c}


def _exec_rule_matches(m: Match, path: str, argv: str) -> bool:
    pattern = m.path or ""
    if not pattern:
        return False
    if "/" in pattern:
        if not _str_match(pattern, m.is_regex, path):
            return False
    else:
        if not any(_str_match(pattern, m.is_regex, c) for c in _exec_candidates(path, argv)):
            return False
    if m.argv and not _str_match(m.argv, m.is_regex, argv):
        return False
    return True


def rule_matches(rule: Rule, event: dict[str, Any]) -> bool:
    """Return True if ``rule`` fires for ``event``.

    ``event`` is a dict with at least ``layer`` plus layer-specific keys.
    ::

        net:   {layer: "net",  host, port}
        fs:    {layer: "fs",   path}
        nav:   {layer: "nav",  path}
        perm:  {layer: "perm", path, op}      # op like "chmod", "chattr"
        exec:  {layer: "exec", path, argv}
        url:   {layer: "url",  method, host, path}
        oc:    {layer: "oc:<tool>", tool, target}
    """
    layer = event.get("layer")
    if rule.layer != layer and not (rule.layer == "oc" and str(layer or "").startswith("oc:")):
        return False

    m = rule.match
    if rule.layer == "net":
        host = event.get("host") or ""
        port = str(event.get("port") or "")
        if not _str_match(m.host or "*", m.is_regex, host):
            return False
        if (m.port or "*") != "*" and not _str_match(m.port, False, port):
            return False
        return True

    if rule.layer in ("fs", "nav"):
        path = event.get("path") or ""
        return _str_match(m.path or "", m.is_regex, path)

    if rule.layer == "perm":
        path = event.get("path") or ""
        if m.op and event.get("op") != m.op:
            return False
        return _str_match(m.path or "", m.is_regex, path)

    if rule.layer == "exec":
        path = event.get("path") or ""
        argv = event.get("argv") or ""
        return _exec_rule_matches(m, path, argv)

    if rule.layer == "env":
        name = event.get("name") or ""
        return _str_match(m.target or m.primary, m.is_regex, name)

    if rule.layer == "acp":
        method = event.get("method") or ""
        return _str_match(m.target or m.primary, m.is_regex, method)

    if rule.layer in ("ask", "reply"):
        # Content matching uses substring semantics for globs (containment).
        content = event.get("content") or ""
        if m.is_regex:
            return _str_match(m.primary, True, content)
        pat = (m.target or m.primary) or ""
        return pat in content

    if rule.layer == "url":
        if m.method and event.get("method", "").upper() != m.method.upper():
            return False
        # Use the raw primary for match so the operator's pattern stays intact.
        url = event.get("url")
        if url is None:
            host = event.get("host") or ""
            path = event.get("path") or ""
            url = (host + path) if (host or path) else ""
        return _str_match(m.primary, m.is_regex, url)

    if rule.layer.startswith("oc:") or rule.layer == "oc":
        if rule.layer != "oc":
            # strict tool slot
            if event.get("tool") != m.tool:
                return False
        target = event.get("target") or ""
        return _str_match(m.target or m.primary, m.is_regex, target)

    return False


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    action: str                  # "allow" | "deny" | "approval" | "rewrite" | "shadow_rewrite"
    rule: Rule | None            # None if no rule matched (default)
    highlight: bool = False
    rewrite_spec: dict[str, Any] | None = None
    # For counter-exhausted rules that fell through, we may want to attach
    # a trace.
    trace: tuple[str, ...] = ()


_DEFAULT_DECISION = Decision(action="allow", rule=None)


def _rule_expired(rule: Rule, state: RuntimeState, now: float) -> bool:
    if rule.rule_id in state.disabled:
        return True
    if rule.modifiers.ttl_seconds is not None:
        if now >= rule.created_at + rule.modifiers.ttl_seconds:
            state.disabled.add(rule.rule_id)
            return True
    if rule.modifiers.max_hits is not None:
        if state.hits.get(rule.rule_id, 0) >= rule.modifiers.max_hits:
            # Exhausted rules remain in the list but are treated per-then.
            return False  # handled in evaluate (may still fire via then:)
    return False


_PRECEDENCE = (
    ("deny", True),
    ("deny", False),
    ("approval", True),
    ("approval", False),
    ("allow", True),
    ("allow", False),
)


def evaluate(
    bw: Bulwark,
    event: dict[str, Any],
    *,
    armor_id: str | None = None,
    state: RuntimeState | None = None,
    now: float | None = None,
) -> Decision:
    """Evaluate ``event`` against bulwark rules.

    Returns a Decision. Pass ``state`` (same instance across calls) to get
    correct counter/ttl semantics; a fresh state is used if omitted
    (suitable for tests)."""
    state = state if state is not None else RuntimeState()
    now = now if now is not None else time.time()

    # Filter rules to those whose scope matches.
    applicable = [r for r in bw.rules if scope_matches(r.scope, armor_id)]

    # Walk each precedence bucket; within a bucket, file order wins.
    for want_action, want_global in _PRECEDENCE:
        for rule in applicable:
            if rule.action != want_action:
                continue
            if rule.scope.include_global != want_global:
                # Note: include_global is a boolean — rules with
                # include_global=True are the "global" bucket.
                continue
            if _rule_expired(rule, state, now):
                continue
            if not rule_matches(rule, event):
                continue
            # Counter exhaustion? Apply then: / config default.
            mods = rule.modifiers
            if mods.max_hits is not None:
                current = state.hits.get(rule.rule_id, 0)
                if current >= mods.max_hits:
                    mode = mods.then_mode or bw.config.counter_exhaust_default
                    if mode == "fallthrough":
                        continue
                    # Fire the exhausted rule under the effective mode.
                    effective_action = mode if mode in _VALID_ACTIONS else "deny"
                    return Decision(
                        action=effective_action,
                        rule=rule,
                        highlight=mods.highlight,
                        rewrite_spec=mods.rewrite_spec,
                        trace=(f"counter_exhausted:{mode}",),
                    )
            # Record hit and fire under the rule's declared action.
            state.hits[rule.rule_id] = state.hits.get(rule.rule_id, 0) + 1
            return Decision(
                action=rule.action,
                rule=rule,
                highlight=mods.highlight,
                rewrite_spec=mods.rewrite_spec,
            )

    return _DEFAULT_DECISION


# ---------------------------------------------------------------------------
# Helpers for callers
# ---------------------------------------------------------------------------

def iter_matching_rules(bw: Bulwark, armor_id: str | None) -> list[Rule]:
    """Rules applicable to a given armor (for UI listings)."""
    return [r for r in bw.rules if scope_matches(r.scope, armor_id)]


def split_layer(rule: Rule) -> tuple[str, str | None]:
    """Return (family, sublayer). For ``oc:read`` → ``("oc", "read")``;
    for ``fs`` → ``("fs", None)``."""
    if ":" in rule.layer:
        fam, _, sub = rule.layer.partition(":")
        return fam, sub
    return rule.layer, None


def write_back(bw: Bulwark, path: Path) -> None:
    """Serialize Bulwark back to disk. Preserves original ``raw`` lines
    where possible, else re-renders from structured fields."""
    out: list[str] = []
    if bw.config.counter_exhaust_default != "deny":
        out.append("## config")
        out.append(f"counter_exhaust_default: {bw.config.counter_exhaust_default}")
        out.append("")
    # Group rules by scope header string.
    groups: dict[str, list[Rule]] = {}
    order: list[str] = []
    for r in bw.rules:
        key = r.scope.raw
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    for key in order:
        out.append(f"## {key}")
        for r in groups[key]:
            out.append(r.raw.strip() or _render_rule(r))
        out.append("")
    path.write_text("\n".join(out).rstrip() + "\n")


def _render_rule(r: Rule) -> str:
    parts = [r.action]
    if r.modifiers.ttl_seconds is not None:
        parts.append(f"ttl {_fmt_duration(r.modifiers.ttl_seconds)}")
    if r.modifiers.max_hits is not None:
        parts.append(f"x{r.modifiers.max_hits}")
    if r.modifiers.then_mode:
        parts.append(f"then:{r.modifiers.then_mode}")
    if r.modifiers.highlight:
        parts.append("highlight")
    parts.append(r.layer)
    if r.match.is_regex:
        parts.append("regex:" + r.match.primary)
    else:
        parts.append(r.match.primary)
    line = " ".join(parts)
    if r.note:
        line += f"  # {r.note}"
    return line


def _fmt_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
