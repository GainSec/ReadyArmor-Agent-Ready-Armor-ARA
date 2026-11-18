"""MCP-Scan sidecar integration (Invariant Labs).

`mcp-scan` is a static + dynamic scanner for Model Context Protocol
traffic. Running it as a sidecar alongside an ARA session gives
coverage for a dimension ARA's MITM doesn't see today: MCP tool-call
metadata, tool poisoning, tool shadowing across MCP servers.

This module provides:

* ``is_available()`` — probe for the installed mcp-scan binary.
* ``run_static_scan(config_path)`` — one-shot scan of an MCP config
  file; returns structured findings.
* ``launch_proxy_sidecar(listen_port, ...)`` — spawn mcp-scan in
  proxy mode; returns a handle the supervisor can stop.
* ``ingest_findings(event_sink, findings)`` — route findings into
  ARA's audit stream as ``mcp_scan_finding`` events.

This is a **framework**: the exact command-line + output JSON schema
is tracked upstream (Invariant Labs / Snyk Labs) and evolves. The
module calls the binary and parses its stdout as JSON lines; if the
real-world format differs, override ``parse_findings`` in a subclass
or monkeypatch.

References:
    https://invariantlabs.ai/blog/introducing-mcp-scan
    https://explorer.invariantlabs.ai/docs/mcp-scan/
    https://labs.snyk.io/resources/snyk-labs-invariant-labs/
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Finding:
    severity: str              # "info" | "low" | "medium" | "high" | "critical"
    category: str              # e.g. "tool_poisoning" | "tool_shadowing" | "pii_leak"
    source: str                # server name or config path where it came from
    detail: str
    raw: dict = field(default_factory=dict)
    ts: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["ts"]:
            d["ts"] = time.time()
        return d


@dataclass
class Handle:
    """Return from ``launch_proxy_sidecar``. Call ``stop()`` to terminate."""
    process: subprocess.Popen
    port: int
    log_path: Path
    thread: "threading.Thread | None" = None

    def stop(self, timeout: float = 3.0) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()


# ---- binary detection ------------------------------------------------

def binary_path() -> Optional[str]:
    """Resolve the ``mcp-scan`` binary. Respects ``$ARA_MCP_SCAN``
    override. Returns absolute path or None."""
    custom = os.environ.get("ARA_MCP_SCAN")
    if custom:
        return custom if Path(custom).is_file() else None
    return shutil.which("mcp-scan") or shutil.which("mcp_scan")


def is_available() -> bool:
    return binary_path() is not None


# ---- static scan -----------------------------------------------------

def run_static_scan(config_path: Path, *, timeout: float = 60.0) -> list[Finding]:
    """Run ``mcp-scan scan <config>`` and parse findings from stdout.

    ``config_path`` is typically an MCP client config (Claude Code's
    ``~/.claude/config.json``, Cursor's ``~/.cursor/mcp.json``, …).
    Returns an empty list if mcp-scan isn't installed — callers treat
    that as a no-op rather than an error.
    """
    bin_ = binary_path()
    if bin_ is None:
        return []
    try:
        proc = subprocess.run(
            [bin_, "scan", str(config_path), "--json"],
            capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    return parse_findings(proc.stdout, source=str(config_path))


# ---- proxy sidecar ---------------------------------------------------

def launch_proxy_sidecar(
    listen_port: int,
    state_root: Path,
    *,
    event_sink: "Callable[[dict], None] | None" = None,
) -> "Handle | None":
    """Launch ``mcp-scan proxy --port <port>`` as a subprocess and
    tail its stdout, routing each finding into ``event_sink`` (typically
    an audit writer) as a dict. Returns None if mcp-scan isn't
    installed.

    The proxy listens on ``listen_port`` for MCP traffic — the agent's
    MCP client must be configured to point at that port for coverage
    to be complete. ARA doesn't auto-configure the client (too
    environment-specific); the operator or wrapper script does it.
    """
    bin_ = binary_path()
    if bin_ is None:
        return None
    log_path = state_root / "mcp_scan.log"
    state_root.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "ab")
    proc = subprocess.Popen(
        [bin_, "proxy", "--port", str(listen_port), "--json"],
        stdout=subprocess.PIPE,
        stderr=log_f,
        text=True,
        bufsize=1,
    )

    def _tail():
        if proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            f = _finding_from_dict(obj)
            if f is None:
                continue
            if event_sink is not None:
                try:
                    event_sink({
                        "kind": "mcp_scan_finding",
                        **f.to_dict(),
                    })
                except Exception:  # noqa: BLE001
                    pass

    t = threading.Thread(target=_tail, daemon=True)
    t.start()
    return Handle(process=proc, port=listen_port, log_path=log_path, thread=t)


# ---- parsing ---------------------------------------------------------

def parse_findings(stdout: str, *, source: str) -> list[Finding]:
    """Parse mcp-scan's stdout. One JSON object per line is the default
    scheme; a single top-level JSON array is also accepted."""
    stdout = stdout.strip()
    if not stdout:
        return []
    out: list[Finding] = []
    if stdout.startswith("["):
        try:
            arr = json.loads(stdout)
            for obj in arr:
                f = _finding_from_dict(obj, default_source=source)
                if f:
                    out.append(f)
        except json.JSONDecodeError:
            pass
        return out
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        f = _finding_from_dict(obj, default_source=source)
        if f:
            out.append(f)
    return out


def _finding_from_dict(
    obj: dict, *, default_source: str = "",
) -> Optional[Finding]:
    """Best-effort mapping from mcp-scan JSON to ``Finding``.

    Accepted keys (with common aliases): ``severity`` / ``level``,
    ``category`` / ``type`` / ``rule``, ``source`` / ``server``,
    ``detail`` / ``message`` / ``description``. Unknown fields land
    in ``raw``.
    """
    if not isinstance(obj, dict):
        return None
    sev = (obj.get("severity") or obj.get("level") or "info").lower()
    cat = (obj.get("category") or obj.get("type") or obj.get("rule")
           or "unknown")
    src = obj.get("source") or obj.get("server") or default_source
    detail = (obj.get("detail") or obj.get("message")
              or obj.get("description") or "")
    return Finding(
        severity=str(sev),
        category=str(cat),
        source=str(src),
        detail=str(detail),
        raw=obj,
    )


def ingest_findings(
    event_sink: Callable[[dict], None],
    findings: list[Finding],
) -> int:
    """Push findings into an event sink. Returns the number sent."""
    count = 0
    for f in findings:
        try:
            event_sink({"kind": "mcp_scan_finding", **f.to_dict()})
            count += 1
        except Exception:  # noqa: BLE001
            pass
    return count
