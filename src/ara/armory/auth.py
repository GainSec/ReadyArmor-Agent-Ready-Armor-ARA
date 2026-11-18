"""Token-based auth for the Armory.

Generate a random token on first run, persist it under the state root, and
require it as a cookie or `?t=` query string on every page. Localhost-only
is enforced at the socket bind level."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


TOKEN_FILE = "armory-token"
VIEWER_TOKEN_FILE = "armory-viewer-token"


def _ensure_file_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        tok = path.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    path.write_text(tok + "\n")
    os.chmod(path, 0o600)
    return tok


def ensure_token(state_root: Path) -> str:
    return _ensure_file_token(state_root / TOKEN_FILE)


def ensure_viewer_token(state_root: Path) -> str:
    """Derived read-only token. Lets a stakeholder open the dashboard
    for observation without the ability to POST mutations. Generated
    on first use and persisted under ``<state>/armory-viewer-token``."""
    return _ensure_file_token(state_root / VIEWER_TOKEN_FILE)


def read_token(state_root: Path) -> str | None:
    path = state_root / TOKEN_FILE
    if not path.exists():
        return None
    tok = path.read_text().strip()
    return tok or None
