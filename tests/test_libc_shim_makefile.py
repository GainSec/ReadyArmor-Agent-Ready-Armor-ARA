from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from ara.launch import _libc_shim_status


REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE_DIR = REPO_ROOT / "src" / "guard" / "libc_shim"


def test_libc_shim_makefile_uses_platform_safe_link_flags() -> None:
    proc = subprocess.run(
        ["make", "-B", "-n", "-C", str(MAKEFILE_DIR), "clean", "all"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    link_line = next(line for line in lines if "libara_shim.so" in line and " -o " in line)

    if platform.system() == "Darwin":
        assert "--no-as-needed" not in link_line
    else:
        assert "-Wl,--no-as-needed" in link_line


def test_libc_shim_status_rejects_macho_on_linux(tmp_path: Path) -> None:
    shim = tmp_path / "libara_shim.so"
    shim.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 32)

    ok, reason = _libc_shim_status(shim, platform_name="linux")

    assert ok is False
    assert "not a Linux ELF" in reason


def test_libc_shim_status_accepts_elf_on_linux(tmp_path: Path) -> None:
    shim = tmp_path / "libara_shim.so"
    shim.write_bytes(b"\x7fELF" + b"\x00" * 32)

    ok, reason = _libc_shim_status(shim, platform_name="linux")

    assert ok is True
    assert reason == "ok"
