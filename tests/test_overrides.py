"""DEPRECATED: covers the legacy walls+overrides+policy evaluator, retired in the Bulwark rollout. Tests preserved as documentation; Bulwark equivalents in test_bulwark.py / test_bulwark_supervisor.py."""

import pytest
pytestmark = pytest.mark.skip(reason='legacy policy evaluator retired — see test_bulwark*.py')

from pathlib import Path

from ara import overrides


def test_add_load_remove(short_tmp: Path):
    state = short_tmp / ".ara"
    entry = overrides.add_override(state, kind="path", pattern="/home/user/docs/**",
                                   reason="ok")
    assert entry.id
    store = overrides.load(state)
    assert len(store.overrides) == 1
    assert store.overrides[0].pattern == "/home/user/docs/**"

    entry2 = overrides.add_blackhole(state, kind="command", pattern="rm")
    assert entry2.id != entry.id
    store = overrides.load(state)
    assert len(store.blackholes) == 1
    assert store.blackholes[0].pattern == "rm"

    assert overrides.remove(state, entry.id) is True
    store = overrides.load(state)
    assert len(store.overrides) == 0
    assert len(store.blackholes) == 1


def test_policy_consults_overrides(short_tmp: Path):
    from ara.policy import evaluate
    from ara.walls import parse

    walls = parse("## deny_path\n/etc/shadow\n")
    # Without override, denied.
    d = evaluate(walls, {"op": "fs.read", "path": "/etc/shadow"})
    assert d.verdict == "deny"
    # Now add an override.
    state = short_tmp / ".ara"
    overrides.add_override(state, kind="path", pattern="/etc/shadow",
                           reason="test")
    store = overrides.load(state)
    d = evaluate(walls, {"op": "fs.read", "path": "/etc/shadow"},
                 override_store=store)
    assert d.verdict == "allow"
    assert d.reason == "ALLOW_OVERRIDE"


def test_policy_consults_blackholes(short_tmp: Path):
    from ara.policy import evaluate
    from ara.walls import parse

    walls = parse("")  # no rules
    state = short_tmp / ".ara"
    overrides.add_blackhole(state, kind="path", pattern="/secret/**")
    store = overrides.load(state)
    d = evaluate(walls, {"op": "fs.read", "path": "/secret/x"}, override_store=store)
    assert d.verdict == "deny"
    assert d.reason == "DENY_BLACKHOLE"


def test_path_parametric_and_merge(short_tmp: Path):
    """load_at/save_at + Store.merge let callers use any directory."""
    a = short_tmp / "a" / "overrides.json"
    b = short_tmp / "b" / "overrides.json"
    e1 = overrides.add_override_at(a, kind="path", pattern="/x/**")
    e2 = overrides.add_blackhole_at(b, kind="command", pattern="rm")
    sa = overrides.load_at(a)
    sb = overrides.load_at(b)
    assert [e.id for e in sa.overrides] == [e1.id]
    assert [e.id for e in sb.blackholes] == [e2.id]
    merged = sa.merge(sb)
    assert {e.pattern for e in merged.overrides} == {"/x/**"}
    assert {e.pattern for e in merged.blackholes} == {"rm"}
    # global wins on id collision
    from dataclasses import replace
    dup = replace(e1, pattern="/different")
    sb2 = overrides.Store(overrides=[dup])
    assert sa.merge(sb2).overrides[0].pattern == "/x/**"
    assert overrides.remove_at(a, e1.id) is True
    assert overrides.load_at(a).overrides == []


def test_ttl_expiry(short_tmp: Path):
    import time
    state = short_tmp / ".ara"
    # Entry with already-past expiry is expired.
    e = overrides.add_override(state, kind="path", pattern="/x",
                               expires_at=time.time() - 10)
    assert e.is_expired()
    # Entry with future expiry is not expired.
    e = overrides.add_override(state, kind="path", pattern="/y",
                               expires_at=time.time() + 60)
    assert not e.is_expired()
    # Entry with no expiry never expires.
    e = overrides.add_override(state, kind="path", pattern="/z")
    assert e.expires_at is None
    assert not e.is_expired()


def test_parse_ttl():
    from ara.overrides import parse_ttl
    assert parse_ttl("5m") == 300.0
    assert parse_ttl("2h") == 7200.0
    assert parse_ttl("1d") == 86400.0
    assert parse_ttl("90s") == 90.0
    assert parse_ttl("45") == 45.0
    import pytest
    with pytest.raises(ValueError): parse_ttl("")
    with pytest.raises(ValueError): parse_ttl("bogus")


def test_expired_override_is_not_consulted(short_tmp: Path):
    import time
    from ara.policy import evaluate
    from ara.walls import parse
    state = short_tmp / ".ara"
    overrides.add_override(state, kind="path", pattern="/etc/shadow",
                           expires_at=time.time() - 1)
    store = overrides.load(state)
    walls = parse("## deny_path\n/etc/shadow\n")
    # Even though an allow-override is on disk, it's expired, so deny wins.
    d = evaluate(walls, {"op": "fs.read", "path": "/etc/shadow"},
                 override_store=store)
    assert d.verdict == "deny"
