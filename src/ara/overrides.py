"""Import-compat shim. See ``ara._overrides_shim``."""

from __future__ import annotations

from ._overrides_shim import *  # noqa: F401,F403
from ._overrides_shim import (  # noqa: F401
    Entry,
    Store,
    new_entry_id,
    path_for,
    load,
    load_at,
    save,
    save_at,
    add_override,
    add_blackhole,
    add_override_at,
    add_blackhole_at,
    remove,
    remove_at,
    find,
    parse_ttl,
)
