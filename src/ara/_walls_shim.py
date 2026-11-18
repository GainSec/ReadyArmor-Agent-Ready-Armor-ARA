"""Minimal in-tree replacement for the deleted ``ara.walls`` module.

WALLS.md files are migrated to ``bulwark.md`` on first armory startup
and renamed to ``.bulwark-migrated.bak``; the parser survives only so
``bulwark_migrate`` can read legacy files and so legacy callsites that
reference ``Walls`` / ``parse_file`` / ``scrub_env`` keep importing.

Matches the subset of the old module that was actually imported:
  * dataclasses (PathRule, CommandRule, NetRule, ApprovalRule,
    EnvRule, AllowExecRule, Walls)
  * ``parse`` / ``parse_file`` (full parser, needed by migration)
  * ``WallsValidationError`` + ``validate`` / ``validate_file``
  * ``scrub_env`` (strip env vars at worker spawn)
  * ``find_env_match``
  * helpers (``_glob_to_regex``, ``find_match``, ``find_approval_match``,
    ``find_command_match``, ``find_net_match``, ``find_allow_exec_match``,
    ``find_touched_path``, ``extract_path_candidates``)

New policy work does NOT go here — Bulwark (``ara.bulwark``) is the
layer. This module is retained purely to keep the migration + a few
inspect_cli paths alive during the transition.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PathRule:
    rule_id: str
    pattern: str
    raw: str
    is_regex: bool = False


@dataclass(frozen=True)
class CommandRule:
    rule_id: str
    pattern: str
    raw: str
    is_regex: bool = False


@dataclass(frozen=True)
class ApprovalRule:
    rule_id: str
    kind: str
    pattern: str
    raw: str
    is_regex: bool = False


@dataclass(frozen=True)
class EnvRule:
    rule_id: str
    pattern: str
    raw: str


@dataclass(frozen=True)
class NetRule:
    rule_id: str
    pattern: str
    host_pattern: str
    port_pattern: str
    raw: str


@dataclass(frozen=True)
class AllowExecRule:
    rule_id: str
    pattern: str
    raw: str
    is_regex: bool = False


@dataclass(frozen=True)
class Walls:
    path_rules: tuple = ()
    command_rules: tuple = ()
    approval_rules: tuple = ()
    net_rules: tuple = ()
    env_rules: tuple = ()
    allow_exec_rules: tuple = ()
    exec_posture: str = "denylist"


class WallsValidationError(Exception):
    """Raised when WALLS.md has a structural or pattern-level problem."""


_HEADING = re.compile(r"^##\s+(\w+)\s*$")
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash"}


def _strip_regex_prefix(line: str) -> tuple[bool, str]:
    low = line.lstrip()
    if low.lower().startswith("regex:"):
        return True, low[6:].strip()
    return False, line


def _expand_user(pattern: str, *, home: Path) -> str:
    if pattern.startswith("~/"):
        return str(home / pattern[2:])
    if pattern == "~":
        return str(home)
    return pattern


def _split_net_pattern(line: str) -> tuple[str, str]:
    if ":" not in line:
        return line, "*"
    host, _, port = line.rpartition(":")
    if not port:
        port = "*"
    return host or "*", port


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


def parse(walls_text: str, *, home: Path | None = None) -> Walls:
    home = home or Path.home()
    section: str | None = None
    path_rules: list[PathRule] = []
    cmd_rules: list[CommandRule] = []
    approval_rules: list[ApprovalRule] = []
    net_rules: list[NetRule] = []
    env_rules: list[EnvRule] = []
    allow_exec_rules: list[AllowExecRule] = []
    tag_patterns: dict[str, list[str]] = {}
    counters = {"path": 0, "cmd": 0, "approval": 0, "net": 0, "env": 0, "allow_exec": 0}
    exec_posture = "denylist"

    # First pass: collect tag definitions.
    for raw in walls_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADING.match(line)
        if m:
            section = m.group(1)
            continue
        if line.startswith("#"):
            continue
        if section == "tags":
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            name, pattern = parts
            tag_patterns.setdefault(name, []).append(pattern)

    def _expand_tag(body: str) -> list[str]:
        if body.startswith("@"):
            return list(tag_patterns.get(body[1:].strip(), []))
        return [body]

    section = None
    for raw in walls_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADING.match(line)
        if m:
            section = m.group(1)
            continue
        if line.startswith("#"):
            continue
        if section == "tags":
            continue
        if section == "deny_path":
            is_regex, body = _strip_regex_prefix(line)
            for pat in _expand_tag(body):
                counters["path"] += 1
                expanded = pat if is_regex else _expand_user(pat, home=home)
                path_rules.append(PathRule(
                    rule_id=f"deny_path:{counters['path']}",
                    pattern=expanded, raw=line, is_regex=is_regex,
                ))
        elif section == "deny_command":
            is_regex, body = _strip_regex_prefix(line)
            for pat in _expand_tag(body):
                counters["cmd"] += 1
                cmd_rules.append(CommandRule(
                    rule_id=f"deny_command:{counters['cmd']}",
                    pattern=pat, raw=line, is_regex=is_regex,
                ))
        elif section == "approval_path":
            is_regex, body = _strip_regex_prefix(line)
            for pat in _expand_tag(body):
                counters["approval"] += 1
                expanded = pat if is_regex else _expand_user(pat, home=home)
                approval_rules.append(ApprovalRule(
                    rule_id=f"approval_path:{counters['approval']}",
                    kind="path", pattern=expanded, raw=line, is_regex=is_regex,
                ))
        elif section == "approval_command":
            is_regex, body = _strip_regex_prefix(line)
            for pat in _expand_tag(body):
                counters["approval"] += 1
                approval_rules.append(ApprovalRule(
                    rule_id=f"approval_command:{counters['approval']}",
                    kind="command", pattern=pat, raw=line, is_regex=is_regex,
                ))
        elif section == "deny_net":
            host, port = _split_net_pattern(line)
            counters["net"] += 1
            net_rules.append(NetRule(
                rule_id=f"deny_net:{counters['net']}",
                pattern=line, host_pattern=host, port_pattern=port, raw=line,
            ))
        elif section == "approval_net":
            counters["approval"] += 1
            approval_rules.append(ApprovalRule(
                rule_id=f"approval_net:{counters['approval']}",
                kind="net", pattern=line, raw=line,
            ))
        elif section == "deny_env":
            counters["env"] += 1
            env_rules.append(EnvRule(
                rule_id=f"deny_env:{counters['env']}",
                pattern=line, raw=line,
            ))
        elif section == "allow_exec":
            is_regex, body = _strip_regex_prefix(line)
            for pat in _expand_tag(body):
                counters["allow_exec"] += 1
                allow_exec_rules.append(AllowExecRule(
                    rule_id=f"allow_exec:{counters['allow_exec']}",
                    pattern=pat, raw=line, is_regex=is_regex,
                ))
        elif section == "posture":
            if ":" in line:
                key, _, val = line.partition(":")
                if key.strip().lower() == "exec":
                    v = val.strip().lower()
                    if v in ("allowlist", "denylist"):
                        exec_posture = v
    return Walls(
        path_rules=tuple(path_rules),
        command_rules=tuple(cmd_rules),
        approval_rules=tuple(approval_rules),
        net_rules=tuple(net_rules),
        env_rules=tuple(env_rules),
        allow_exec_rules=tuple(allow_exec_rules),
        exec_posture=exec_posture,
    )


def parse_file(path: Path, *, home: Path | None = None) -> Walls:
    return parse(path.read_text(), home=home)


def validate(
    walls_text: str, *, home: Path | None = None, strict: bool = True,
) -> list[str]:
    # Shallow stub — parses and returns empty warnings. The real
    # validator lived in the old module but isn't depended on now
    # that Bulwark is the policy source.
    try:
        parse(walls_text, home=home)
        return []
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise WallsValidationError(str(exc))
        return [str(exc)]


def validate_file(path: Path, *, home: Path | None = None) -> list[str]:
    return validate(path.read_text(), home=home)


def find_env_match(walls: Walls, var_name: str) -> EnvRule | None:
    if not var_name:
        return None
    for rule in walls.env_rules:
        if _glob_to_regex(rule.pattern).match(var_name):
            return rule
    return None


_CONTROL_PLANE_ENV = {
    "ARA_SOCKET",
    "ARA_SESSION_TOKEN",
    "ARA_SESSION_ID",
    "ARA_SUPERVISOR_PID",
    "ARA_TRANSCRIPT_PROTOCOL",
}


def scrub_env(
    env: dict[str, str], walls: Walls,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    clean: dict[str, str] = {}
    stripped: list[tuple[str, str]] = []
    for k, v in env.items():
        if k in _CONTROL_PLANE_ENV:
            clean[k] = v
            continue
        r = find_env_match(walls, k)
        if r is not None:
            stripped.append((k, r.rule_id))
            continue
        clean[k] = v
    return clean, stripped


# ---- path / command / net matchers (used by inspect_cli) ------------

def _rule_regex(pattern: str, is_regex: bool) -> re.Pattern[str]:
    if is_regex:
        try:
            return re.compile(pattern)
        except re.error:
            return re.compile(re.escape(pattern))
    return _glob_to_regex(pattern)


def _rule_match(pattern: str, is_regex: bool, candidate: str) -> bool:
    if not candidate:
        return False
    p = _rule_regex(pattern, is_regex)
    return bool(p.search(candidate) if is_regex else p.match(candidate))


def _resolve(path: str) -> str:
    p = Path(path).expanduser()
    try:
        return str(p.resolve(strict=False))
    except OSError:
        return str(p.absolute())


def matches(rule: PathRule, path: str) -> bool:
    r = _resolve(path)
    return (
        _rule_match(rule.pattern, rule.is_regex, r)
        or _rule_match(rule.pattern, rule.is_regex, path)
    )


def find_match(walls: Walls, path: str) -> PathRule | None:
    for rule in walls.path_rules:
        if matches(rule, path):
            return rule
    return None


def _net_match(
    host_pattern: str, port_pattern: str,
    host: str, port: "int | str | None",
) -> bool:
    host_re = _glob_to_regex(host_pattern.lower())
    if not host_re.match(host.lower()):
        return False
    if port_pattern == "*":
        return True
    port_str = str(port) if port is not None else ""
    return bool(_glob_to_regex(port_pattern).match(port_str))


def find_net_match(
    walls: Walls, host: str, port: "int | str | None" = None,
) -> NetRule | None:
    for rule in walls.net_rules:
        if _net_match(rule.host_pattern, rule.port_pattern, host, port):
            return rule
    return None


def find_command_match(
    walls: Walls, command: str, args: list[str] | None = None,
) -> CommandRule | None:
    args = args or []
    basename = os.path.basename(command) if command else ""
    joined = " ".join([command, *args]) if args else command
    candidates: set[str] = {command, basename, joined}
    if basename in _SHELLS and "-c" in args:
        idx = args.index("-c")
        if idx + 1 < len(args):
            inner = args[idx + 1]
            inner_tokens = inner.split()
            if inner_tokens:
                candidates.add(inner_tokens[0])
                candidates.add(os.path.basename(inner_tokens[0]))
                candidates.add(inner)
    for rule in walls.command_rules:
        for c in candidates:
            if c and _rule_match(rule.pattern, rule.is_regex, c):
                return rule
    return None


def find_approval_match(
    walls: Walls, op: str, path: str | None = None,
    command: str | None = None, args: list[str] | None = None,
    host: str | None = None, port: "int | str | None" = None,
) -> ApprovalRule | None:
    for rule in walls.approval_rules:
        if rule.kind == "path" and path and op.startswith("fs."):
            if _rule_match(rule.pattern, rule.is_regex, path):
                return rule
        if rule.kind == "command" and command and op == "exec.spawn":
            basename = os.path.basename(command) if command else ""
            for c in (command, basename):
                if c and _rule_match(rule.pattern, rule.is_regex, c):
                    return rule
        if rule.kind == "net" and op == "net.connect" and host is not None:
            h, p = _split_net_pattern(rule.pattern)
            if _net_match(h, p, host, port):
                return rule
    return None


def find_allow_exec_match(
    walls: Walls, command: str, args: list[str] | None = None,
) -> AllowExecRule | None:
    if not command:
        return None
    basename = os.path.basename(command)
    joined = f"{command} {' '.join(str(a) for a in args)}" if args else command
    for rule in walls.allow_exec_rules:
        for c in (command, basename, joined):
            if c and _rule_match(rule.pattern, rule.is_regex, c):
                return rule
    return None


def extract_path_candidates(
    command: str, args: list[str], *, cwd: str | None = None,
    home: Path | None = None,
) -> list[str]:
    """Paths an argv might reference. Used by inspect_cli explain.
    Shallow — no shell introspection recursion."""
    home = home or Path.home()
    out: list[str] = []
    seen: set[str] = set()

    def _push(s: str) -> None:
        if s == "~":
            s = str(home)
        elif s.startswith("~/"):
            s = str(home) + s[1:]
        if not os.path.isabs(s):
            base = cwd if cwd else str(home)
            s = os.path.join(base, s)
        s = os.path.normpath(s)
        if s not in seen:
            seen.add(s)
            out.append(s)

    for a in args:
        if not isinstance(a, str):
            continue
        if a.startswith("-") and not (a.startswith("/") or a.startswith("./")):
            if "=" in a:
                val = a.split("=", 1)[1]
                if val and not any(c.isspace() for c in val) and \
                   (val.startswith(("/", "~", "./", "../")) or "/" in val):
                    _push(val)
            continue
        if not a or any(c.isspace() for c in a):
            continue
        if a.startswith(("/", "~", "./", "../")) or "/" in a:
            _push(a)
    return out


def find_touched_path(
    walls: Walls, command: str, args: list[str], *, cwd: str | None = None,
) -> "tuple[PathRule, str] | None":
    for candidate in extract_path_candidates(command, args, cwd=cwd):
        hit = find_match(walls, candidate)
        if hit is not None:
            return hit, candidate
    return None


def home_for(env: "dict[str, str] | None" = None) -> Path:
    source = dict(env) if env is not None else dict(os.environ)
    return Path(source.get("HOME", str(Path.home())))
