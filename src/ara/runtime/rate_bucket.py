"""Per-(engagement, host) rate-limit token bucket.

Per spec ``armor-trim-v0.1-amendment.md`` §9. The trim's ``rate_limits``
config seeds buckets at trim apply time; the channel layer calls
``acquire(...)`` before dispatching outbound requests.

Token-bucket model:

- Each ``(engagement_id, host)`` pair has its own bucket.
- ``capacity`` = the configured RPS (also the max burst).
- ``refill_rate`` = ``capacity`` tokens per second.
- ``acquire(n=1)`` succeeds if ≥ n tokens are available; otherwise
  returns the time until enough tokens accumulate.

Persistence: ``<state>/rate_limits/<engagement_id>/<host_safe>.json``.
Each bucket persists ``{tokens, last_refill, capacity, refill_rate}``.
``host`` is sanitized for filesystem safety (colons → ``_``).

Overflow modes (selected by trim):

- ``queue`` (default): caller waits ``time_until_available`` seconds.
- ``deny``: caller hard-fails immediately.

This module returns the verdict; the channel layer is responsible for
either sleeping (queue) or returning an error (deny).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time as _time
from dataclasses import asdict, dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Bucket model
# ---------------------------------------------------------------------------

@dataclass
class Bucket:
    capacity: float                            # max tokens (also = burst)
    refill_rate: float                         # tokens per second
    tokens: float                              # current balance
    last_refill: float                         # epoch seconds

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Bucket":
        return cls(
            capacity=float(d.get("capacity") or 0.0),
            refill_rate=float(d.get("refill_rate") or 0.0),
            tokens=float(d.get("tokens") or 0.0),
            last_refill=float(d.get("last_refill") or 0.0),
        )


@dataclass
class AcquireResult:
    granted: bool
    tokens_consumed: float
    wait_seconds: float                        # 0.0 if granted, else delay
    bucket_after: Bucket
    reason: str = ""


# ---------------------------------------------------------------------------
# Pure refill math (testable without disk)
# ---------------------------------------------------------------------------

def refill(bucket: Bucket, now_epoch: float) -> Bucket:
    """Advance ``bucket`` to ``now_epoch``. Pure.

    A bucket with ``last_refill <= 0`` is uninitialized — fill to capacity.
    Clock skew (``now < last_refill``) leaves tokens untouched and just
    advances the timestamp; we don't credit time travel.
    """
    if bucket.last_refill <= 0:
        bucket.tokens = bucket.capacity
        bucket.last_refill = now_epoch
        return bucket
    if now_epoch <= bucket.last_refill:
        bucket.last_refill = now_epoch
        return bucket
    elapsed = now_epoch - bucket.last_refill
    bucket.tokens = min(
        bucket.capacity,
        bucket.tokens + elapsed * bucket.refill_rate,
    )
    bucket.last_refill = now_epoch
    return bucket


def try_consume(
    bucket: Bucket, n: float, now_epoch: float,
) -> AcquireResult:
    """Pure: refill + maybe-consume. Does not touch disk."""
    refill(bucket, now_epoch)
    if bucket.capacity <= 0:
        # Disabled / unlimited — no-op grant. Capacity 0 = unlimited
        # by convention (caller declined to set a limit for this host).
        return AcquireResult(
            granted=True, tokens_consumed=0.0, wait_seconds=0.0,
            bucket_after=bucket, reason="unlimited (capacity=0)",
        )
    if bucket.tokens >= n:
        bucket.tokens -= n
        return AcquireResult(
            granted=True, tokens_consumed=n, wait_seconds=0.0,
            bucket_after=bucket, reason="acquired",
        )
    deficit = n - bucket.tokens
    wait = deficit / bucket.refill_rate if bucket.refill_rate > 0 else float("inf")
    return AcquireResult(
        granted=False, tokens_consumed=0.0,
        wait_seconds=wait, bucket_after=bucket,
        reason=f"insufficient tokens (need {n}, have {bucket.tokens:.3f})",
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()


def _safe_host(host: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return s or "unknown"


def buckets_dir(state_root: Path, engagement_id: str) -> Path:
    return Path(state_root) / "rate_limits" / engagement_id


def bucket_path(state_root: Path, engagement_id: str, host: str) -> Path:
    return buckets_dir(state_root, engagement_id) / f"{_safe_host(host)}.json"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def _load(path: Path) -> Bucket | None:
    if not path.is_file():
        return None
    try:
        return Bucket.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def _save(path: Path, bucket: Bucket) -> None:
    _atomic_write(path, json.dumps(bucket.to_dict(), indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Seeding from trim config
# ---------------------------------------------------------------------------

def seed_buckets(
    state_root: Path, engagement_id: str, rate_limits: dict, *,
    now_epoch: float | None = None,
) -> dict[str, Bucket]:
    """Materialize buckets for this engagement from the trim's
    ``rate_limits`` block. Idempotent — re-seeding updates capacity
    + refill rate but preserves current ``tokens`` so a re-applied
    trim doesn't reset the operator's running budget mid-engagement.

    Returns the keyed bucket map for visibility.
    """
    if now_epoch is None:
        now_epoch = _time.time()
    out: dict[str, Bucket] = {}
    if not rate_limits:
        return out
    global_rps = float(rate_limits.get("global_rps") or 0.0)
    per_host = dict(rate_limits.get("per_host") or {})

    def _upsert(host: str, rps: float) -> Bucket:
        if rps <= 0:
            return Bucket(0.0, 0.0, 0.0, now_epoch)
        p = bucket_path(state_root, engagement_id, host)
        prior = _load(p)
        if prior is None:
            b = Bucket(
                capacity=rps, refill_rate=rps, tokens=rps,
                last_refill=now_epoch,
            )
        else:
            # Preserve running tokens; cap at the (possibly updated) capacity.
            b = Bucket(
                capacity=rps, refill_rate=rps,
                tokens=min(prior.tokens, rps),
                last_refill=prior.last_refill or now_epoch,
            )
        _save(p, b)
        return b

    if global_rps > 0:
        out["__global__"] = _upsert("__global__", global_rps)
    for host, rps in per_host.items():
        try:
            rate = float(rps)
        except (TypeError, ValueError):
            continue
        out[host] = _upsert(host, rate)
    return out


# ---------------------------------------------------------------------------
# Acquire (the actual call the channel makes)
# ---------------------------------------------------------------------------

def acquire(
    state_root: Path, engagement_id: str, host: str, *,
    n: float = 1.0, now_epoch: float | None = None,
) -> AcquireResult:
    """Attempt to consume ``n`` tokens from the (engagement, host)
    bucket. If a per-host bucket is configured, that's checked first;
    then the global bucket. Both must grant for the request to proceed.

    If no buckets exist for this engagement, behaves as unlimited.
    """
    if now_epoch is None:
        now_epoch = _time.time()
    with _LOCK:
        # Per-host first (more specific).
        host_bucket = _load(bucket_path(state_root, engagement_id, host))
        if host_bucket is not None:
            res = try_consume(host_bucket, n, now_epoch)
            _save(bucket_path(state_root, engagement_id, host), res.bucket_after)
            if not res.granted:
                return res
        # Global next.
        global_bucket = _load(bucket_path(state_root, engagement_id, "__global__"))
        if global_bucket is not None:
            res = try_consume(global_bucket, n, now_epoch)
            _save(bucket_path(state_root, engagement_id, "__global__"), res.bucket_after)
            if not res.granted:
                return res
            return res
        if host_bucket is None:
            # Neither bucket exists — unlimited.
            return AcquireResult(
                granted=True, tokens_consumed=0.0, wait_seconds=0.0,
                bucket_after=Bucket(0.0, 0.0, 0.0, now_epoch),
                reason="no rate-limit bucket configured",
            )
        # host_bucket existed and granted.
        return AcquireResult(
            granted=True, tokens_consumed=n, wait_seconds=0.0,
            bucket_after=host_bucket, reason="acquired (host-only)",
        )


def get_bucket(
    state_root: Path, engagement_id: str, host: str,
) -> Bucket | None:
    return _load(bucket_path(state_root, engagement_id, host))


__all__ = [
    "AcquireResult", "Bucket",
    "acquire", "bucket_path", "buckets_dir", "get_bucket",
    "refill", "seed_buckets", "try_consume",
]
