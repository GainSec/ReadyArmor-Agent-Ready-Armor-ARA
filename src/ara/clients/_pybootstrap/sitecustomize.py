# ARA bootstrap shim. Python's site machinery imports this at startup
# because PYTHONPATH (set by ara launch) puts this directory first.
# We delegate to the real installer in ara.clients.sitecustomize so the
# logic lives in one place.
import os, sys
try:
    from ara.clients import sitecustomize as _ara_install
    _ = _ara_install
except Exception as _e:
    if os.environ.get("ARA_HOOK_DEBUG"):
        sys.stderr.write(f"[ara-hook] bootstrap failed: {_e!r}\n")
