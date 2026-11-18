"""Unit tests for the Bulwark parser + evaluator.

Covers: section headers (scope expressions), action parsing, modifiers
(ttl / xN / then: / highlight), per-layer match semantics, precedence,
counter exhaustion, regex escape hatch, and round-trip serialisation.
"""

from __future__ import annotations

import time

import pytest

from ara import bulwark as bw


# ---------------------------------------------------------------------------
# Scope expressions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "header,armor,expected",
    [
        ("global", None, True),
        ("global", "orion", True),
        ("armor: orion", "orion", True),
        ("armor: orion", "atlas", False),
        ("armor: orion+atlas", "atlas", True),
        ("armor: orion+atlas", "vega", False),
        ("armor: hermes-default", "hermes-default", True),
        ("armor: hermes-default", "hermes", False),
        ("armor: default+hermes-default", "hermes-default", True),
        ("armor: *-atlas", "atlas", False),
        ("armor: *-atlas", "orion", True),
        ("armor: *", None, False),
        ("armor: *", "orion", True),
        ("global -armor: hermes-default", "hermes-default", False),
        ("global -armor: hermes-default", "default", True),
        ("global -armor: orion", "orion", False),
        ("global -armor: orion+atlas", "atlas", False),
        ("global -armor: orion", "vega", True),
    ],
)
def test_scope_evaluation(header: str, armor: str | None, expected: bool) -> None:
    expr = bw._parse_scope_expr(header)
    assert bw.scope_matches(expr, armor) is expected


def test_bad_scope_header() -> None:
    with pytest.raises(ValueError):
        bw._parse_scope_expr("neither-global-nor-armor")
    with pytest.raises(ValueError):
        bw._parse_scope_expr("armor:")


# ---------------------------------------------------------------------------
# Rule parsing
# ---------------------------------------------------------------------------

def test_parse_simple_rules() -> None:
    text = """\
## global
deny  net   *.doubleclick.net
allow url   GET https://api.github.com/*

## armor: orion
deny  fs    /etc/**
approval exec /usr/bin/curl
"""
    b = bw.parse(text)
    assert not b.parse_errors, b.parse_errors
    assert len(b.rules) == 4
    actions = [r.action for r in b.rules]
    layers = [r.layer for r in b.rules]
    assert actions == ["deny", "allow", "deny", "approval"]
    assert layers == ["net", "url", "fs", "exec"]


def test_rule_with_modifiers() -> None:
    text = """\
## global
deny ttl 1h x3 then:fallthrough highlight url https://api.evil.com
"""
    b = bw.parse(text)
    assert not b.parse_errors
    r = b.rules[0]
    assert r.modifiers.ttl_seconds == 3600
    assert r.modifiers.max_hits == 3
    assert r.modifiers.then_mode == "fallthrough"
    assert r.modifiers.highlight is True


def test_config_section() -> None:
    text = """\
## config
counter_exhaust_default: approval

## global
allow net *.example.com
"""
    b = bw.parse(text)
    assert not b.parse_errors
    assert b.config.counter_exhaust_default == "approval"


def test_rule_before_scope_is_error() -> None:
    text = "deny net *.evil.com\n"
    b = bw.parse(text)
    assert any("before any ## scope" in e for e in b.parse_errors)


def test_bad_modifier() -> None:
    text = "## global\ndeny sometimes net *.evil.com\n"
    b = bw.parse(text)
    assert b.parse_errors


def test_duration_parser() -> None:
    assert bw._parse_duration("30s") == 30
    assert bw._parse_duration("15m") == 900
    assert bw._parse_duration("2h") == 7200
    assert bw._parse_duration("7d") == 7 * 86400
    with pytest.raises(ValueError):
        bw._parse_duration("5 weeks")


def test_trailing_note_stripped() -> None:
    text = '## global\ndeny net *.evil.com  # comment text\n'
    b = bw.parse(text)
    assert not b.parse_errors
    assert b.rules[0].note == "comment text"


def test_regex_escape_hatch() -> None:
    text = "## global\ndeny url regex:^https://.*\\.evil\\.com/.*$\n"
    b = bw.parse(text)
    assert not b.parse_errors
    assert b.rules[0].match.is_regex is True


# ---------------------------------------------------------------------------
# Layer match semantics
# ---------------------------------------------------------------------------

def _single(text: str) -> bw.Rule:
    b = bw.parse(text)
    assert not b.parse_errors, b.parse_errors
    return b.rules[0]


def test_net_match() -> None:
    r = _single("## global\ndeny net *.evil.com\n")
    assert bw.rule_matches(r, {"layer": "net", "host": "api.evil.com", "port": 443})
    assert not bw.rule_matches(r, {"layer": "net", "host": "safe.com", "port": 443})


def test_net_port_narrow() -> None:
    r = _single("## global\ndeny net *.evil.com:8080\n")
    assert bw.rule_matches(r, {"layer": "net", "host": "x.evil.com", "port": 8080})
    assert not bw.rule_matches(r, {"layer": "net", "host": "x.evil.com", "port": 443})


def test_fs_match() -> None:
    r = _single("## global\ndeny fs /etc/**\n")
    assert bw.rule_matches(r, {"layer": "fs", "path": "/etc/passwd"})
    assert bw.rule_matches(r, {"layer": "fs", "path": "/etc/nested/dir/file"})
    assert not bw.rule_matches(r, {"layer": "fs", "path": "/var/log/x"})


def test_nav_match() -> None:
    r = _single("## global\ndeny nav /root/**\n")
    assert bw.rule_matches(r, {"layer": "nav", "path": "/root/.ssh"})


def test_perm_match_any_op() -> None:
    r = _single("## global\ndeny perm /etc/**\n")
    assert bw.rule_matches(
        r, {"layer": "perm", "path": "/etc/passwd", "op": "chmod"}
    )
    assert bw.rule_matches(
        r, {"layer": "perm", "path": "/etc/passwd", "op": "chattr"}
    )


def test_perm_match_op_narrowed() -> None:
    r = _single("## global\ndeny perm op=chattr /etc/**\n")
    assert bw.rule_matches(
        r, {"layer": "perm", "path": "/etc/passwd", "op": "chattr"}
    )
    assert not bw.rule_matches(
        r, {"layer": "perm", "path": "/etc/passwd", "op": "chmod"}
    )


def test_exec_match() -> None:
    r = _single("## global\ndeny exec /usr/bin/curl\n")
    assert bw.rule_matches(
        r, {"layer": "exec", "path": "/usr/bin/curl", "argv": "curl https://x"}
    )


def test_exec_basename_rule_matches_resolved_binary() -> None:
    r = _single("## global\ndeny exec curl\n")
    assert bw.rule_matches(
        r, {"layer": "exec", "path": "/usr/bin/curl", "argv": "https://x"}
    )


def test_exec_basename_rule_matches_shell_inner_command() -> None:
    r = _single("## global\ndeny exec curl\n")
    assert bw.rule_matches(
        r,
        {
            "layer": "exec",
            "path": "/bin/bash",
            "argv": "-c curl https://x",
        },
    )


def test_url_match_with_method() -> None:
    r = _single("## global\ndeny url POST https://api.stripe.com/v1/charges\n")
    assert bw.rule_matches(
        r, {"layer": "url", "method": "POST", "url": "https://api.stripe.com/v1/charges"}
    )
    assert not bw.rule_matches(
        r, {"layer": "url", "method": "GET", "url": "https://api.stripe.com/v1/charges"}
    )


def test_acp_method_match_literal() -> None:
    r = _single("## global\ndeny acp chat.inject\n")
    assert bw.rule_matches(r, {"layer": "acp", "method": "chat.inject"})
    assert not bw.rule_matches(r, {"layer": "acp", "method": "chat.history"})


def test_acp_method_match_regex() -> None:
    r = _single("## global\ndeny acp regex:^cron\\.\n")
    assert bw.rule_matches(r, {"layer": "acp", "method": "cron.add"})
    assert bw.rule_matches(r, {"layer": "acp", "method": "cron.run"})
    assert not bw.rule_matches(r, {"layer": "acp", "method": "chat.send"})


def test_oc_tool_specific_match() -> None:
    r = _single("## global\ndeny oc:bash regex:rm\\s+-rf\n")
    assert bw.rule_matches(
        r, {"layer": "oc:bash", "tool": "bash", "target": "rm -rf /tmp/x"}
    )
    assert not bw.rule_matches(
        r, {"layer": "oc:write", "tool": "write", "target": "rm -rf /tmp/x"}
    )


def test_oc_family_catchall() -> None:
    r = _single("## global\ndeny oc regex:rm\\s+-rf\n")
    assert bw.rule_matches(
        r, {"layer": "oc:bash", "tool": "bash", "target": "rm -rf /"}
    )
    assert bw.rule_matches(
        r, {"layer": "oc:write", "tool": "write", "target": "rm -rf stuff"}
    )


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_precedence_global_deny_beats_armor_allow() -> None:
    text = """\
## global
deny net *.evil.com

## armor: orion
allow net *.evil.com
"""
    b = bw.parse(text)
    d = bw.evaluate(
        b, {"layer": "net", "host": "api.evil.com", "port": 443}, armor_id="orion"
    )
    assert d.action == "deny"


def test_armor_deny_beats_global_approval() -> None:
    text = """\
## global
approval net *.api.com

## armor: orion
deny net *.api.com
"""
    b = bw.parse(text)
    d = bw.evaluate(
        b, {"layer": "net", "host": "x.api.com", "port": 443}, armor_id="orion"
    )
    assert d.action == "deny"


def test_default_allow_when_no_match() -> None:
    text = "## global\ndeny net *.evil.com\n"
    b = bw.parse(text)
    d = bw.evaluate(b, {"layer": "net", "host": "safe.com", "port": 443})
    assert d.action == "allow"
    assert d.rule is None


# ---------------------------------------------------------------------------
# Modifiers at evaluation time
# ---------------------------------------------------------------------------

def test_counter_fallthrough() -> None:
    # When an allow rule with then:fallthrough exhausts, it becomes
    # inert; the next evaluation falls through to the default decision
    # (no deny rule present → default allow).
    text = """\
## global
allow x1 then:fallthrough net api.example.com
"""
    b = bw.parse(text)
    state = bw.RuntimeState()
    ev = {"layer": "net", "host": "api.example.com", "port": 443}
    assert bw.evaluate(b, ev, state=state).action == "allow"
    d2 = bw.evaluate(b, ev, state=state)
    assert d2.action == "allow"
    assert d2.rule is None  # default decision, not the exhausted rule


def test_counter_default_deny() -> None:
    text = """\
## global
allow x1 net api.example.com
"""
    b = bw.parse(text)
    state = bw.RuntimeState()
    ev = {"layer": "net", "host": "api.example.com", "port": 443}
    assert bw.evaluate(b, ev, state=state).action == "allow"
    d2 = bw.evaluate(b, ev, state=state)
    assert d2.action == "deny"
    assert "counter_exhausted" in d2.trace[0]


def test_ttl_expiry() -> None:
    text = "## global\ndeny ttl 1s net *.evil.com\n"
    b = bw.parse(text)
    ev = {"layer": "net", "host": "x.evil.com", "port": 443}
    now = time.time()
    d = bw.evaluate(b, ev, now=now)
    assert d.action == "deny"
    # 2 seconds later, rule should be expired → default allow.
    d2 = bw.evaluate(b, ev, now=now + 2)
    assert d2.action == "allow"


def test_highlight_flag_propagates() -> None:
    text = "## global\nallow highlight net api.example.com\n"
    b = bw.parse(text)
    d = bw.evaluate(b, {"layer": "net", "host": "api.example.com", "port": 443})
    assert d.action == "allow"
    assert d.highlight is True


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------

def test_round_trip(tmp_path) -> None:
    src = """\
## config
counter_exhaust_default: approval

## global
deny  net   *.evil.com
allow ttl 1h x2 highlight url GET https://api.github.com/*

## armor: orion
approval fs /etc/**
"""
    b = bw.parse(src)
    assert not b.parse_errors
    out = tmp_path / "round.md"
    bw.write_back(b, out)
    b2 = bw.parse_file(out)
    assert not b2.parse_errors
    assert [r.action for r in b2.rules] == [r.action for r in b.rules]
    assert [r.layer for r in b2.rules] == [r.layer for r in b.rules]
    assert b2.config.counter_exhaust_default == "approval"
