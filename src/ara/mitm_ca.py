"""ARA MITM certificate authority.

Generates an ARA-owned CA on first use and signs short-lived leaf certs
for whatever host the proxy needs to MITM. Shells out to ``openssl`` so
there's no Python crypto dependency.

Directory layout under ``<state_root>/mitm/``:

    ca.key, ca.crt        -- the ARA root
    certs/<host>.key      -- leaf private keys (cached)
    certs/<host>.crt      -- leaf certs signed by the root
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path


CA_SUBJECT = "/CN=ARA MITM Root/O=ARA Supervisor/OU=Dev"
LEAF_DAYS = 30
LEAF_RENEW_WITHIN_SECONDS = 24 * 60 * 60


class CA:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root / "mitm"
        self.certs_dir = self.root / "certs"
        self.ca_key = self.root / "ca.key"
        self.ca_crt = self.root / "ca.crt"
        self._lock = threading.Lock()

    # ---- setup ----

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.certs_dir.mkdir(parents=True, exist_ok=True)
        if self.ca_key.exists() and self.ca_crt.exists():
            return
        with self._lock:
            if self.ca_key.exists() and self.ca_crt.exists():
                return
            # RSA 2048 is enough for MITM purposes and widely supported.
            subprocess.run(
                ["openssl", "genrsa", "-out", str(self.ca_key), "2048"],
                check=True, capture_output=True,
            )
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-new", "-nodes",
                    "-key", str(self.ca_key),
                    "-sha256", "-days", "3650",
                    "-subj", CA_SUBJECT,
                    "-out", str(self.ca_crt),
                ],
                check=True, capture_output=True,
            )
            os.chmod(self.ca_key, 0o600)
            os.chmod(self.ca_crt, 0o644)

    # ---- leaf certs ----

    def leaf_paths(self, host: str) -> tuple[Path, Path]:
        safe = host.replace(":", "_")
        return (
            self.certs_dir / f"{safe}.key",
            self.certs_dir / f"{safe}.crt",
        )

    def sign_leaf(self, host: str) -> tuple[Path, Path]:
        """Return (key_path, crt_path) for ``host``, signing if missing."""
        self.ensure()
        key, crt = self.leaf_paths(host)
        if key.exists() and crt.exists() and self._leaf_is_fresh(crt):
            return key, crt
        with self._lock:
            if key.exists() and crt.exists() and self._leaf_is_fresh(crt):
                return key, crt
            csr = self.certs_dir / f"{host.replace(':', '_')}.csr"
            ext = self.certs_dir / f"{host.replace(':', '_')}.ext"
            try:
                subprocess.run(
                    ["openssl", "genrsa", "-out", str(key), "2048"],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    [
                        "openssl", "req", "-new",
                        "-key", str(key),
                        "-subj", f"/CN={host}",
                        "-out", str(csr),
                    ],
                    check=True, capture_output=True,
                )
                ext.write_text(
                    "authorityKeyIdentifier=keyid,issuer\n"
                    "basicConstraints=CA:FALSE\n"
                    "keyUsage=digitalSignature, keyEncipherment\n"
                    "extendedKeyUsage=serverAuth\n"
                    f"subjectAltName=DNS:{host}\n"
                )
                subprocess.run(
                    [
                        "openssl", "x509", "-req",
                        "-in", str(csr),
                        "-CA", str(self.ca_crt),
                        "-CAkey", str(self.ca_key),
                        "-CAcreateserial",
                        "-out", str(crt),
                        "-days", str(LEAF_DAYS),
                        "-sha256",
                        "-extfile", str(ext),
                    ],
                    check=True, capture_output=True,
                )
                os.chmod(key, 0o600)
                os.chmod(crt, 0o644)
            finally:
                for p in (csr, ext):
                    if p.exists():
                        p.unlink()
            return key, crt

    def _leaf_is_fresh(self, crt: Path) -> bool:
        """Return True when a cached leaf cert is valid beyond the renew window."""
        result = subprocess.run(
            [
                "openssl", "x509",
                "-checkend", str(LEAF_RENEW_WITHIN_SECONDS),
                "-noout",
                "-in", str(crt),
            ],
            check=False,
            capture_output=True,
        )
        return result.returncode == 0
