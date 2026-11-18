"""Import-compat shim for the deleted module. Re-exports from
``ara._walls_shim`` so legacy callers (``from ara.walls import
parse_file``, ``from .walls import Walls``, …) keep working.

New code must not import this. Bulwark (``ara.bulwark``) is the
policy layer.
"""

from __future__ import annotations

from ._walls_shim import *  # noqa: F401,F403
from ._walls_shim import (  # noqa: F401 — explicit re-exports for linters
    Walls,
    PathRule,
    CommandRule,
    ApprovalRule,
    NetRule,
    EnvRule,
    AllowExecRule,
    WallsValidationError,
    parse,
    parse_file,
    validate,
    validate_file,
    scrub_env,
    find_env_match,
    find_match,
    find_command_match,
    find_approval_match,
    find_net_match,
    find_allow_exec_match,
    find_touched_path,
    extract_path_candidates,
    matches,
    home_for,
    _glob_to_regex,
)
