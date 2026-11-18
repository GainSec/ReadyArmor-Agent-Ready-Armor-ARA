import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTDEPO = Path.home() / "ARA" / "TESTDEPO"


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """Short tmpdir under ~/ARA/TESTDEPO/ so AF_UNIX paths fit in 104 chars."""
    TESTDEPO.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="ara-", dir=str(TESTDEPO)))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True, scope="session")
def _validate_path_budget() -> None:
    # Warn early if TESTDEPO is placed somewhere that would blow the socket limit.
    probe = TESTDEPO / "ara-XXXXXXXX" / ".ara" / "sessions" / ("0" * 32) / "guard.sock"
    if len(os.fsencode(probe)) > 104:
        raise RuntimeError(
            f"TESTDEPO path too long for AF_UNIX sockets: {probe} "
            f"({len(os.fsencode(probe))} bytes; limit 104)"
        )
