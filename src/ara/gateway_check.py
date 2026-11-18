"""Gateway port discovery for the WebSocket tap + shim redirect.

The contained worker's gateway port is read from ``~/.openclaw/openclaw.json``
(falling back to :data:`DEFAULT_GATEWAY_PORT`). ``armory/app.py`` uses it to
point the always-on ``gateway_ws_tap`` at the right upstream, and ``launch.py``
exports it as ``ARA_GATEWAY_REAL_PORT`` so the libc shim can rewrite connect()
targets (defense-in-depth on top of the contained ``openclaw.json`` rewrite).

Note: this module used to also host an OpenClaw gateway *supervision* probe
(``probe``/``emit_event``). That was dead weight in the hermes world and was
removed 2026-07-13. Contained-hermes ownership + rogue detection now lives in
``ara.hermes_guard``.
"""

from __future__ import annotations

import json
from pathlib import Path


DEFAULT_GATEWAY_PORT = 18789


def discover_gateway_port() -> int:
    """Prefer the port in ``~/.openclaw/openclaw.json``, else 18789."""
    cfg = Path.home() / ".openclaw" / "openclaw.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
            port = data.get("gateway", {}).get("port")
            if isinstance(port, int) and 1 <= port <= 65535:
                return port
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return DEFAULT_GATEWAY_PORT
