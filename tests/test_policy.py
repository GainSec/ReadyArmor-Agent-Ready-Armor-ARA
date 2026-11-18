"""DEPRECATED: these tests cover the legacy walls+overrides evaluator
(``policy.evaluate``) which has been retired. Bulwark is the only
policy source now. Tests are kept in-repo as documentation of the old
contract but marked skipped; bulwark equivalents live in
``test_bulwark.py`` and ``test_bulwark_supervisor.py``.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="policy.evaluate retired — see test_bulwark.py")

from ara.policy import evaluate  # noqa: E402
from ara.walls import parse  # noqa: E402


def test_allow_when_no_rule_matches(tmp_path: Path):
    walls = parse("## deny_path\n~/.ssh/**\n", home=tmp_path)
    d = evaluate(walls, {"op": "fs.read", "path": str(tmp_path / "notes.txt")})
    assert d.verdict == "allow"


def test_deny_with_reason_and_rule_id(tmp_path: Path):
    walls = parse("## deny_path\n~/.ssh/**\n", home=tmp_path)
    d = evaluate(walls, {"op": "fs.read", "path": str(tmp_path / ".ssh/id_rsa")})
    assert d.verdict == "deny"
    assert d.reason == "DENY_FILE_PATH_BLOCKED"
    assert d.rule_id == "deny_path:1"


def test_malformed_query_denies(tmp_path: Path):
    walls = parse("## deny_path\n~/.ssh/**\n", home=tmp_path)
    d = evaluate(walls, {"op": "fs.read"})  # missing path
    assert d.verdict == "deny"
    assert d.reason == "MALFORMED_QUERY"


def test_unknown_op_allows_in_poc(tmp_path: Path):
    walls = parse("## deny_path\n~/.ssh/**\n", home=tmp_path)
    d = evaluate(walls, {"op": "some.future.op", "path": "/anything"})
    assert d.verdict == "allow"


def test_decision_to_dict_drops_nones(tmp_path: Path):
    walls = parse("## deny_path\n~/.ssh/**\n", home=tmp_path)
    allowed = evaluate(walls, {"op": "fs.read", "path": "/ok"}).to_dict()
    assert allowed == {"verdict": "allow"}

    denied_path = str(tmp_path / ".ssh/id_rsa")
    denied = evaluate(
        walls, {"op": "fs.read", "path": denied_path}
    ).to_dict()
    assert denied == {
        "verdict": "deny",
        "reason": "DENY_FILE_PATH_BLOCKED",
        "rule_id": "deny_path:1",
        "touched": denied_path,
    }
