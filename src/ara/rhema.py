"""Import-compat shim. See ``ara._rhema_shim``."""

from __future__ import annotations

from ._rhema_shim import *  # noqa: F401,F403
from ._rhema_shim import (  # noqa: F401
    RhemaRule,
    load,
    save,
    evaluate,
    new_pair_id,
    new_id,
    add,
    remove,
    upsert_managed,
    remove_managed,
    purge_managed,
    synthesize_deny_sse,
    synthesize_deny_response,
    apply_rewrite_spec,
    active_absolute_rule,
    path_for,
)
