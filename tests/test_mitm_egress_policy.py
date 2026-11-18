from __future__ import annotations

from pathlib import Path

from ara import armors as armors_mod
from ara import task_session
from ara.mitm_proxy import MitmProxy


class _ConnectSock:
    def __init__(self) -> None:
        self.sent = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data


def _proxy(state: Path) -> MitmProxy:
    session = state / "sessions" / ("e" * 32)
    session.mkdir(parents=True)
    return MitmProxy(state, session)


def _write_task_loadout(state: Path) -> None:
    (state / "armors" / "blacksmith" / "absolutes").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "absolutes" / "default.md").write_text("armor\n")
    (state / "weapons" / "w-task").mkdir(parents=True)
    (state / "weapons" / "w-task" / "seal.json").write_text("{}")
    (state / "armors" / "blacksmith" / "trims" / "trim-a").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state / "armors" / "blacksmith" / "temper" / "proposals").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "temper" / "proposals" / "temp-a.json").write_text("{}")


def test_blacksmith_default_deny_blocks_blind_connect(short_tmp: Path) -> None:
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "blacksmith", description="forge-only")
    armors_mod.set_active_armor(state, "blacksmith")
    (state / "bulwark.md").write_text(
        "## armor: blacksmith\n"
        "allow net api.openai.com:443\n"
        "deny net *:*\n"
    )
    proxy = _proxy(state)
    sock = _ConnectSock()
    tunneled = []
    proxy._blind_tunnel = lambda host, port, client_sock: tunneled.append((host, port))  # type: ignore[method-assign]

    proxy._handle_connect("CONNECT duckduckgo.com:443 HTTP/1.1", sock)  # type: ignore[arg-type]

    assert tunneled == []
    assert b"403 Forbidden" in sock.sent


def test_blacksmith_allowlist_preserves_provider_connect(
    short_tmp: Path,
) -> None:
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "blacksmith", description="forge-only")
    armors_mod.set_active_armor(state, "blacksmith")
    (state / "bulwark.md").write_text(
        "## armor: blacksmith\n"
        "allow net api.openai.com:443\n"
        "allow net api.openai.com:443\n"
        "allow net api.anthropic.com:443\n"
        "deny net *:*\n"
    )
    proxy = _proxy(state)

    assert proxy._bulwark_allows_connect("api.openai.com", 443) is True
    assert proxy._bulwark_allows_connect("api.openai.com", 443) is True
    assert proxy._bulwark_allows_connect("api.anthropic.com", 443) is True
    assert proxy._bulwark_allows_connect("duckduckgo.com", 443) is False


def test_provider_http_url_is_not_denied_by_trim_target_url_scope(
    short_tmp: Path,
) -> None:
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "hermes-default", description="default")
    armors_mod.set_active_armor(state, "hermes-default")
    (state / "bulwark.md").write_text(
        "## global\n"
        "allow net api.openai.com:443\n"
        "allow url https://api.openai.com/**\n"
        "## armor: hermes-default\n"
        "allow url https://example.test/**\n"
        "deny url regex:^https?://(?!(?:example\\.test)(?::\\d+)?(?:/|$)).*$\n"
    )
    proxy = _proxy(state)

    provider_decision = proxy._bulwark_evaluate_http(
        scope="request",
        host="api.openai.com",
        method="POST",
        url_path="/chat/completions",
        body_json={"messages": [{"role": "user", "content": "test"}]},
        body_text='{"messages":[{"role":"user","content":"test"}]}',
    )
    assert provider_decision is None or provider_decision.rule is None

    off_scope_decision = proxy._bulwark_evaluate_http(
        scope="request",
        host="example.invalid",
        method="GET",
        url_path="/",
        body_json=None,
        body_text="",
    )
    assert off_scope_decision is not None
    assert off_scope_decision.action == "deny"
    assert off_scope_decision.rule is not None
    assert off_scope_decision.rule.layer == "url"


def test_task_gate_does_not_block_repeated_target_connect_or_provider(
    monkeypatch,
    short_tmp: Path,
) -> None:
    state = short_tmp / "state"
    state.mkdir()
    _write_task_loadout(state)
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    monkeypatch.setenv("ARA_TASK_RUN_ID", run.task_id)
    proxy = _proxy(state)
    tunneled = []
    proxy._blind_tunnel = lambda host, port, client_sock: tunneled.append((host, port))  # type: ignore[method-assign]

    sock1 = _ConnectSock()
    proxy._handle_connect("CONNECT target.example:443 HTTP/1.1", sock1)  # type: ignore[arg-type]
    assert tunneled == [("target.example", 443)]

    sock2 = _ConnectSock()
    proxy._handle_connect("CONNECT target.example:443 HTTP/1.1", sock2)  # type: ignore[arg-type]
    assert tunneled == [("target.example", 443), ("target.example", 443)]
    assert b"403 Forbidden" not in sock2.sent

    sock3 = _ConnectSock()
    assert proxy._try_enforce_task_connect(  # type: ignore[arg-type]
        "api.openai.com", 443, sock3,
    ) is False
    assert sock3.sent == b""
