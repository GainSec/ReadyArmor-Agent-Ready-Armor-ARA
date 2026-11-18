"""MITM-side exec-deny notice injection.

When Bulwark hard-blocks an exec, ``exec_approvals_proxy`` queues a one-shot
notice via ``ara.exec_deny_notices``. On the agent's next model turn the MITM
proxy drains it and prepends a system message telling the agent what was blocked
and to skip and continue. Mirrors the pause-and-ask inject wiring.
"""
from __future__ import annotations

import json
from pathlib import Path

from ara import armors as armors_mod
from ara import exec_deny_notices as edn
from ara import trims as trims_mod
from ara.mitm_proxy import MitmProxy


def _setup_engagement(tmp_path: Path) -> None:
    armors_mod.create_armor(tmp_path, "blacksmith")
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    meta = trims_mod.derive_trim(tmp_path, "blacksmith", engagement={
        "engagement_id": "eng1", "operator_id": "ops",
        "target": {"primary": "https://x", "scope_allows": ["x"], "env": "prod"},
        "posture": "blackbox",
        "active_testing": True,
    })
    trims_mod.apply_trim(tmp_path, "blacksmith", meta.trim_id)


def _proxy(tmp_path: Path) -> MitmProxy:
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    return MitmProxy(state_root=tmp_path, session_dir=session_dir)


def _req_body() -> bytes:
    return json.dumps({
        "model": "qwen",
        "messages": [{"role": "user", "content": "keep scanning"}],
        "stream": True,
    }).encode()


_REQ_PARSED = {"start": "POST /v1/chat/completions HTTP/1.1"}


def test_inject_names_blocked_term_and_skips(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    edn.append(tmp_path, rule_id="r188", matched_token="reboot",
               cmd_summary="bash -c '...reboot...'", ts=1.0)
    proxy = _proxy(tmp_path)

    out = proxy._try_apply_exec_deny_notice(
        host="api.openai.com", req_parsed=_REQ_PARSED,
        req_body=_req_body(), provider="openai",
    )
    assert out is not None
    payload = json.loads(out)
    assert payload["messages"][0]["role"] == "system"
    sys_msg = payload["messages"][0]["content"]
    assert "`reboot`" in sys_msg
    assert "skip" in sys_msg.lower()
    # Original user turn preserved after the injected note.
    assert payload["messages"][1]["role"] == "user"
    # One-shot: queue drained, so a second turn injects nothing.
    assert not (tmp_path / edn.QUEUE_NAME).exists()
    out2 = proxy._try_apply_exec_deny_notice(
        host="api.openai.com", req_parsed=_REQ_PARSED,
        req_body=_req_body(), provider="openai",
    )
    assert out2 is None


def test_no_notice_no_injection(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    proxy = _proxy(tmp_path)
    out = proxy._try_apply_exec_deny_notice(
        host="api.openai.com", req_parsed=_REQ_PARSED,
        req_body=_req_body(), provider="openai",
    )
    assert out is None


def test_skips_non_llm_path_without_draining(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    edn.append(tmp_path, rule_id="r188", matched_token="reboot",
               cmd_summary="x", ts=1.0)
    proxy = _proxy(tmp_path)
    out = proxy._try_apply_exec_deny_notice(
        host="api.openai.com",
        req_parsed={"start": "GET /v1/models HTTP/1.1"},
        req_body=_req_body(), provider="openai",
    )
    assert out is None
    # Notice must NOT be consumed by a non-model request.
    assert (tmp_path / edn.QUEUE_NAME).exists()


def test_malformed_body_does_not_drain(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    edn.append(tmp_path, rule_id="r188", matched_token="reboot",
               cmd_summary="x", ts=1.0)
    proxy = _proxy(tmp_path)
    out = proxy._try_apply_exec_deny_notice(
        host="api.openai.com", req_parsed=_REQ_PARSED,
        req_body=b"not json", provider="openai",
    )
    assert out is None
    # Parse failure happens before drain — notice preserved.
    assert (tmp_path / edn.QUEUE_NAME).exists()


def test_coalesces_multiple_denies(tmp_path: Path) -> None:
    _setup_engagement(tmp_path)
    for tok in ("reboot", "poweroff", "reboot"):
        edn.append(tmp_path, rule_id="r188", matched_token=tok,
                   cmd_summary="x", ts=1.0)
    proxy = _proxy(tmp_path)
    out = proxy._try_apply_exec_deny_notice(
        host="api.openai.com", req_parsed=_REQ_PARSED,
        req_body=_req_body(), provider="openai",
    )
    assert out is not None
    sys_msg = json.loads(out)["messages"][0]["content"]
    assert "`reboot`" in sys_msg and "`poweroff`" in sys_msg
    assert sys_msg.count("`reboot`") == 1  # dup collapsed
