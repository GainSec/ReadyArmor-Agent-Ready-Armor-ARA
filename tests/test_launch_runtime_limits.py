from __future__ import annotations

from ara import launch as launch_mod


def test_raise_nofile_limit_lifts_soft_limit(monkeypatch) -> None:
    calls = []

    class FakeResource:
        RLIMIT_NOFILE = 7

        @staticmethod
        def getrlimit(kind):
            assert kind == FakeResource.RLIMIT_NOFILE
            return (1024, 8192)

        @staticmethod
        def setrlimit(kind, value):
            calls.append((kind, value))

    monkeypatch.setattr(launch_mod, "resource", FakeResource, raising=False)

    launch_mod._raise_nofile_limit(min_soft=4096)

    assert calls == [(FakeResource.RLIMIT_NOFILE, (4096, 8192))]


def test_raise_nofile_limit_never_exceeds_hard_limit(monkeypatch) -> None:
    calls = []

    class FakeResource:
        RLIMIT_NOFILE = 7

        @staticmethod
        def getrlimit(kind):
            return (1024, 2048)

        @staticmethod
        def setrlimit(kind, value):
            calls.append((kind, value))

    monkeypatch.setattr(launch_mod, "resource", FakeResource, raising=False)

    launch_mod._raise_nofile_limit(min_soft=4096)

    assert calls == [(FakeResource.RLIMIT_NOFILE, (2048, 2048))]


def test_undici_default_uses_runtime_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert launch_mod._undici_module_path({}) == str(
        tmp_path / ".npm-global/lib/node_modules/openclaw/node_modules/undici"
    )


def test_undici_override_expands_runtime_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert launch_mod._undici_module_path({"ARA_UNDICI_MODULE_PATH": "~/node/undici"}) == str(
        tmp_path / "node/undici"
    )
