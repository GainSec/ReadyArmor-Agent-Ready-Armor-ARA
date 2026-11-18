"""Materialize ``net:connection`` evidence from the seccomp-egress ledger.

The seccomp guard already logs every ``connect()`` decision (host/port/verdict/
reason/enforcer, ``component="seccomp-egress"``, ``event="decision"``) to the
global debug ledger — a complete, maximal record. Binding in the hot guard loop
would risk backing up the single-threaded notif handler, so ``net:connection``
evidence is materialized **lazily** from that ledger at filing time, filtered to
the connections relevant to an entry (host / pid / time window), and deduped by
content hash in the binder.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _ledger_path(state_root: Any) -> Path:
    from ara import debug_log
    return Path(state_root) / "logs" / debug_log.DEFAULT_LOG_NAME


def read_connections(
    state_root: Any,
    session_id: str | None,
    *,
    host: str | None = None,
    pid: int | None = None,
    since: float | None = None,
    until: float | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Read seccomp-egress connect decisions from the debug ledger, filtered.
    Best-effort — returns whatever it read on any error."""
    out: list[dict] = []
    try:
        p = _ledger_path(state_root)
        if not p.exists():
            return out
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "seccomp-egress" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("component") != "seccomp-egress" or r.get("event") != "decision":
                    continue
                if session_id and r.get("session_id") != session_id:
                    continue
                f = r.get("fields") or {}
                if host is not None and f.get("host") != host:
                    continue
                if pid is not None and f.get("pid") != pid:
                    continue
                ts = r.get("ts") or f.get("ts")
                if since is not None and ts is not None and ts < since:
                    continue
                if until is not None and ts is not None and ts > until:
                    continue
                out.append({
                    "host": f.get("host"), "port": f.get("port"),
                    "verdict": f.get("verdict"), "reason": f.get("reason"),
                    "enforcer": f.get("enforcer"), "family": f.get("family"),
                    "op": f.get("op"), "pid": f.get("pid"), "ts": ts,
                    "latency_ms": f.get("latency_ms"),
                })
                if len(out) >= limit:
                    break
    except Exception:  # noqa: BLE001
        return out
    return out


def materialize_net_connections(
    session_dir: Any,
    state_root: Any,
    session_id: str | None,
    *,
    host: str | None = None,
    pid: int | None = None,
    since: float | None = None,
    until: float | None = None,
    activation_id: str | None = None,
    limit: int = 1000,
) -> list[str]:
    """Bind matching connect decisions as ``net:connection`` evidence. Idempotent
    by content hash (each decision's unique ts keeps records distinct). Returns
    the bound evidence ids. Best-effort — never raises."""
    conns = read_connections(
        state_root, session_id, host=host, pid=pid, since=since, until=until, limit=limit,
    )
    if not conns:
        return []
    try:
        from ara.runtime.binder.api import EvidenceBinder
    except Exception:  # noqa: BLE001
        return []

    ids: list[str] = []
    binder = None
    try:
        binder = EvidenceBinder(Path(session_dir))
        for c in conns:
            if not c.get("host"):
                continue
            try:
                meta: dict[str, Any] = {"host": str(c["host"])}
                for k in ("port", "verdict", "reason", "enforcer", "family", "op", "pid"):
                    v = c.get(k)
                    if v is None:
                        continue
                    meta[k] = v if k in ("port", "family", "pid") else str(v)
                rec = binder.put(
                    json.dumps(c, sort_keys=True, default=str).encode("utf-8"),
                    source="net:connection",
                    source_metadata=meta,
                    summary=f"connect {c.get('host')}:{c.get('port')} {c.get('verdict')}",
                    content_type="application/json",
                    activation_id=activation_id,
                )
                ids.append(rec.id)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return ids
    finally:
        try:
            if binder is not None:
                binder.close()
        except Exception:  # noqa: BLE001
            pass
    return ids
