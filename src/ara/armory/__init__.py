"""Armory — operator web UI for ARA.

Stdlib-only (http.server + ThreadingHTTPServer). No build step, no CSS yet.
Zero-theme HTML generated server-side; styling lands in a later pass.
"""

from .app import run_armory  # noqa: F401
