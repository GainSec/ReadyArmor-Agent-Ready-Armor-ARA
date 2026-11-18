"""Best-effort process liveness helpers for Armory status surfaces."""
from __future__ import annotations

import errno
import os
import subprocess
from typing import Any


def is_pid_live(pid: Any) -> bool:
    """Return true only when a PID exists and is not visibly defunct."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False

    try:
        res = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return True
    status = (res.stdout or "").strip()
    if res.returncode != 0 or not status:
        return False
    return not status.upper().startswith("Z")
