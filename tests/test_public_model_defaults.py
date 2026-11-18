"""Local model defaults avoid dependencies on an operator deployment."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ara import mitm_proxy, task_action_gate, temper_driver, trims
from ara.armory import debug_views
from ara.forge import loop_driver


@pytest.mark.parametrize("invoke", [
    temper_driver._default_model_invoke,
    loop_driver._stateless_model_invoke,
])
def test_stateless_model_default_uses_loopback(monkeypatch, invoke):
    import urllib.request
    for name in ("ARA_FORGE_MODEL_ENDPOINT", "ARA_TEMPER_MODEL_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "result"}}]}).encode()

    def open_request(request, timeout):
        requests.append(request.full_url)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert invoke("system", "user") == "result"
    assert requests == ["http://127.0.0.1:1234/v1/chat/completions"]


def test_model_health_default_uses_loopback(monkeypatch):
    import urllib.request
    monkeypatch.delenv("ARA_MODEL_HEALTH_URL", raising=False)
    monkeypatch.setattr(debug_views, "_MODEL_CACHE", {"ts": 0.0, "up": None})
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_request(url, timeout):
        requests.append(url)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert debug_views._model_up() is True
    assert requests == ["http://127.0.0.1:1234/v1/models"]


def test_public_model_hosts_retain_mediation():
    for host in ("api.openai.com", "api.anthropic.com"):
        assert mitm_proxy._should_mitm(host) is True
        assert mitm_proxy._provider_for(host) in {"openai", "anthropic"}
        assert task_action_gate.is_task_control_plane_action({
            "op": "net.connect", "host": host, "port": 443,
        }) is True


def test_generated_trim_has_only_loopback_control_plane_defaults():
    assert trims._CONTROL_PLANE_NET_ALLOWS == ("127.*:*", "::1:*")


@pytest.mark.parametrize("name", ["ara-task", "forge", "temper"])
def test_seed_profile_uses_local_model_endpoint(name):
    seed = Path(temper_driver.__file__).parent / "seed/hermes-profiles" / name / "config.yaml"
    config = yaml.safe_load(seed.read_text())
    assert config["model"]["provider"] == "lmstudio"
    assert config["model"]["base_url"] == "http://127.0.0.1:1234/v1"
