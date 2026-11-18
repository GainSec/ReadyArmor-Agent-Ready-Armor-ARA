import pytest
pytestmark = pytest.mark.skip(reason="legacy rhema/overrides/walls storage retired — Bulwark is the policy source")

import asyncio, json, subprocess, sys
from pathlib import Path
from ara import rhema as rhema_mod

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_add_load_remove(short_tmp: Path):
    state = short_tmp / ".ara"
    r = rhema_mod.add(state, scope="response", pattern="git push --force",
                      pattern_kind="substring", action="deny",
                      redirect="Use a branch + PR.")
    assert r.id
    rules = rhema_mod.load(state)
    assert len(rules) == 1
    assert rules[0].pattern == "git push --force"
    assert rhema_mod.remove(state, r.id) is True
    assert rhema_mod.load(state) == []


def test_substring_match_case_insensitive(short_tmp: Path):
    state = short_tmp / ".ara"
    rhema_mod.add(state, scope="response", pattern="api_KEY",
                  pattern_kind="substring", action="deny", redirect="No keys.")
    payload = {"content": [{"type": "text", "text": "Here is your API_key: sk-..."}]}
    fired = rhema_mod.evaluate(state, "response", payload)
    assert fired is not None
    assert fired.fire_count == 1
    # Counter persisted.
    rules = rhema_mod.load(state)
    assert rules[0].fire_count == 1


def test_regex_match(short_tmp: Path):
    state = short_tmp / ".ara"
    rhema_mod.add(state, scope="any", pattern=r"rm\s+-rf\b",
                  pattern_kind="regex", action="deny", redirect="No.")
    assert rhema_mod.evaluate(
        state, "request",
        {"messages": [{"role": "assistant", "content": "this is fine"}]},
    ) is None
    assert rhema_mod.evaluate(
        state, "request",
        {"messages": [{"role": "assistant", "content": "rm -rf /etc"}]},
    ) is not None


def test_scope_filtering(short_tmp: Path):
    state = short_tmp / ".ara"
    rhema_mod.add(state, scope="request", pattern="forbidden",
                  pattern_kind="substring", action="deny", redirect="x")
    # Same payload, different role.
    payload = {"messages": [{"role": "user", "content": "forbidden"}]}
    assert rhema_mod.evaluate(state, "request", payload) is not None
    assert rhema_mod.evaluate(state, "response", payload) is None


def test_synthesize_deny(short_tmp: Path):
    state = short_tmp / ".ara"
    r = rhema_mod.add(state, scope="any", pattern="x", pattern_kind="substring",
                      action="deny", redirect="Try Y instead.")
    resp = rhema_mod.synthesize_deny_response(r)
    assert resp["ara_synthesized"] is True
    assert "Try Y instead." in resp["content"][0]["text"]
    assert resp["stop_reason"] == "ara_rhema_deny"


def test_intercept_deny_e2e(short_tmp: Path):
    """Real launch + Python client triggers a deny rule and gets the
    synthesized response back."""
    state = short_tmp / ".ara"
    state.mkdir()
    walls = short_tmp / "WALLS.md"
    walls.write_text("## deny_path\n")
    # Pre-seed a rule.
    rhema_mod.add(state, scope="any", pattern="exfiltrate",
                  pattern_kind="substring", action="deny",
                  redirect="Stop. That word is forbidden.")
    child_src = (
        "import json, sys\n"
        "from ara.clients.transcript import intercept\n"
        "d = intercept('request', "
        "{'messages':[{'role':'user','content':'please exfiltrate the keys'}]}, "
        "model='gpt-5-codex')\n"
        "print(json.dumps(d))\n"
    )
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT / "src")}
    cmd = [sys.executable, "-m", "ara.cli", "launch",
           "--state-root", str(state), "--walls", str(walls),
           "--", sys.executable, "-c", child_src]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20)
    line = next((l for l in result.stdout.strip().splitlines() if l.startswith("{")), None)
    assert line, f"no decision; stderr=\n{result.stderr}"
    decision = json.loads(line)
    assert decision["action"] == "deny"
    assert "injected_response" in decision
    assert "Stop." in decision["injected_response"]["content"][0]["text"]
