"""Channel registry — two-tier:

- **Class registry** (``register_class`` / ``all_classes``): populated at
  module import for every Channel subclass. The 4.1 catalog generator
  introspects this — name, schema, sink_subsystem, allowed_callers,
  schema fields are all class-level, so no engagement binding is needed.

- **Instance registry** (``register`` / ``get`` / ``all_channels``):
  populated by :func:`runtime.bootstrap.install_engagement_channels`
  with constructed instances bound to a ``sessions_root``. Used at
  runtime dispatch: ``registry.get('lead.file').call(body, ctx)``.

Both registries are thread-safe + idempotent. Tests reset them via the
``unregister_*_for_tests`` helpers below.
"""
from __future__ import annotations

import threading

from ara.runtime.channels.base import Channel


_REGISTRY: dict[str, Channel] = {}
_CLASS_REGISTRY: dict[str, type[Channel]] = {}
_LOCK = threading.Lock()


class DuplicateChannelError(Exception):
    """Raised on attempts to register two distinct channels under the same name."""


def register(channel: Channel) -> None:
    with _LOCK:
        existing = _REGISTRY.get(channel.name)
        if existing is not None and existing is not channel:
            raise DuplicateChannelError(
                f"channel {channel.name!r} already registered "
                f"({type(existing).__name__} vs {type(channel).__name__})"
            )
        _REGISTRY[channel.name] = channel


def get(name: str) -> Channel:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no channel registered under {name!r}") from None


def has(name: str) -> bool:
    return name in _REGISTRY


def all_channels() -> list[Channel]:
    with _LOCK:
        return sorted(_REGISTRY.values(), key=lambda c: c.name)


def unregister_for_tests(name: str | None = None) -> None:
    """Test-only: drop a name (or all names) from the instance registry.

    Note: does NOT clear the class registry — channel classes registered
    at module import are persistent for the process lifetime, the same
    way subclass relationships are. Use :func:`unregister_classes_for_tests`
    when a test specifically needs a clean class-registry slate.
    """
    with _LOCK:
        if name is None:
            _REGISTRY.clear()
        else:
            _REGISTRY.pop(name, None)


# ---------- class registry (for 4.1 catalog) ----------

class DuplicateChannelClassError(Exception):
    """Raised on attempts to register two distinct classes under the same name."""


def register_class(cls: type[Channel]) -> None:
    """Register a Channel subclass for catalog introspection.

    Idempotent for the same class. Raises if a *different* class tries
    to claim the same ``name``.
    """
    with _LOCK:
        existing = _CLASS_REGISTRY.get(cls.name)
        if existing is not None and existing is not cls:
            raise DuplicateChannelClassError(
                f"channel class {cls.name!r} already registered "
                f"({existing.__name__} vs {cls.__name__})"
            )
        _CLASS_REGISTRY[cls.name] = cls


def all_classes() -> list[type[Channel]]:
    with _LOCK:
        return sorted(_CLASS_REGISTRY.values(), key=lambda c: c.name)


def has_class(name: str) -> bool:
    return name in _CLASS_REGISTRY


def unregister_classes_for_tests(name: str | None = None) -> None:
    """Test-only: drop a name (or all names) from the class registry.

    Module-import side effects don't re-populate on a subsequent
    ``import`` (Python caches modules), so prefer
    :func:`snapshot_classes_for_tests` for a save/restore pattern when a
    test needs a clean slate without permanently wiping production
    classes for downstream tests.
    """
    with _LOCK:
        if name is None:
            _CLASS_REGISTRY.clear()
        else:
            _CLASS_REGISTRY.pop(name, None)


def snapshot_classes_for_tests() -> dict[str, type[Channel]]:
    """Snapshot the current class registry; pair with
    :func:`restore_classes_for_tests` in test fixtures."""
    with _LOCK:
        return dict(_CLASS_REGISTRY)


def restore_classes_for_tests(snapshot: dict[str, type[Channel]]) -> None:
    """Replace the class registry with ``snapshot``."""
    with _LOCK:
        _CLASS_REGISTRY.clear()
        _CLASS_REGISTRY.update(snapshot)
