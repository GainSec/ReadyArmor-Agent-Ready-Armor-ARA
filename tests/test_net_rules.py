"""DEPRECATED: covers the legacy walls+overrides+policy evaluator, retired in the Bulwark rollout. Tests preserved as documentation; Bulwark equivalents in test_bulwark.py / test_bulwark_supervisor.py."""

import pytest
pytestmark = pytest.mark.skip(reason='legacy policy evaluator retired — see test_bulwark*.py')

from pathlib import Path
from ara import walls as walls_mod
from ara.policy import evaluate


def _walls(text: str) -> walls_mod.Walls:
    return walls_mod.parse(text, home=Path("/home/test"))


def test_parse_deny_net():
    w = _walls("## deny_net\n*.openai.com:*\nexample.com:22\n*:8080\n")
    assert len(w.net_rules) == 3
    assert w.net_rules[0].host_pattern == "*.openai.com"
    assert w.net_rules[0].port_pattern == "*"
    assert w.net_rules[1].host_pattern == "example.com"
    assert w.net_rules[1].port_pattern == "22"
    assert w.net_rules[2].host_pattern == "*"
    assert w.net_rules[2].port_pattern == "8080"


def test_net_connect_deny():
    w = _walls("## deny_net\napi.openai.com:*\n*:22\n")
    d = evaluate(w, {"op": "net.connect", "host": "api.openai.com", "port": 443})
    assert d.verdict == "deny" and d.reason == "DENY_NET_BLOCKED"
    assert d.rule_id.startswith("deny_net:")
    d = evaluate(w, {"op": "net.connect", "host": "github.com", "port": 22})
    assert d.verdict == "deny"
    d = evaluate(w, {"op": "net.connect", "host": "example.com", "port": 80})
    assert d.verdict == "allow"


def test_net_approval():
    w = _walls("## approval_net\n*.stripe.com:*\n")
    d = evaluate(w, {"op": "net.connect", "host": "api.stripe.com", "port": 443})
    assert d.verdict == "approval" and d.rule_id.startswith("approval_net:")


def test_net_host_case_insensitive():
    w = _walls("## deny_net\nAPI.openai.com:*\n")
    d = evaluate(w, {"op": "net.connect", "host": "api.OPENAI.com", "port": 443})
    assert d.verdict == "deny"


def test_net_override_and_blackhole():
    from ara.overrides import Store, Entry
    import time
    w = _walls("## deny_net\n*.banned.com:*\n")
    # Override allows specific host:port even when deny_net matches.
    ov_store = Store(
        overrides=[Entry(id="ov1", kind="path", pattern="safe.banned.com:443",
                         added_at=time.time())],
        blackholes=[],
    )
    d = evaluate(
        w, {"op": "net.connect", "host": "safe.banned.com", "port": 443},
        override_store=ov_store,
    )
    assert d.verdict == "allow" and d.reason == "ALLOW_OVERRIDE"
    # Blackhole wins over allow-by-default.
    bh_store = Store(
        overrides=[],
        blackholes=[Entry(id="bh1", kind="path", pattern="leak.com:*",
                          added_at=time.time())],
    )
    d = evaluate(
        w, {"op": "net.connect", "host": "leak.com", "port": 1234},
        override_store=bh_store,
    )
    assert d.verdict == "deny" and d.reason == "DENY_BLACKHOLE"


def test_malformed_net_query():
    w = _walls("")
    d = evaluate(w, {"op": "net.connect", "host": "", "port": 80})
    assert d.verdict == "deny" and d.reason == "MALFORMED_QUERY"
