"""In-memory sliding-window rate limiting.

Deliberately dependency-free. Render's free tier runs a single uvicorn
worker, so process-local counters are accurate there. Counters reset when
Render spins the service down after inactivity, which is acceptable: the
global ceiling still does its real job during any active window.

Two layers:

1. Per-IP  - RATE_LIMIT_PER_MIN and RATE_LIMIT_PER_DAY, applied to every
   API route. Stops one person hammering the app.
2. Global  - GLOBAL_IDENTIFY_PER_DAY, applied only to /api/identify. This
   is the layer that actually protects the Pl@ntNet and Gemini free
   quotas, since a per-IP limit does nothing against distributed traffic.
"""

import logging
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

import config

logger = logging.getLogger("invasive-id.ratelimit")

_MINUTE = 60
_DAY = 86_400

# key -> deque[timestamp]. One deque per (ip, window) pair.
_minute_hits: dict[str, deque[float]] = {}
_day_hits: dict[str, deque[float]] = {}
# Global identify timestamps, not keyed by IP.
_global_identify_hits: deque[float] = deque()

# All mutation happens under this lock. Requests are served concurrently by
# the event loop, so read-modify-write on these dicts must be atomic or two
# simultaneous requests can both observe "4 hits" and both be allowed.
_lock = threading.Lock()

# Pruning empty keys on every single request would be wasteful, so we sweep
# periodically instead. Without this the dicts grow one entry per unique IP,
# forever.
_PRUNE_INTERVAL_SECONDS = 300
_last_prune = 0.0


def client_ip(request: Request) -> str:
    """Best-effort real client IP.

    Render terminates TLS at a proxy, so `request.client.host` is the
    *proxy's* address. Using it would put every visitor on the planet into a
    single shared bucket and 429 the app almost immediately. The real client
    is the first entry of X-Forwarded-For.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first

    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _trim(hits: deque[float], now: float, window: float) -> None:
    """Drop timestamps that have aged out of the window."""
    cutoff = now - window
    while hits and hits[0] <= cutoff:
        hits.popleft()


def _prune_if_due(now: float) -> None:
    """Remove keys whose windows are fully expired, so memory stays bounded.

    Caller must hold _lock.
    """
    global _last_prune
    if now - _last_prune < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune = now

    for store, window in ((_minute_hits, _MINUTE), (_day_hits, _DAY)):
        stale = []
        for key, hits in store.items():
            _trim(hits, now, window)
            if not hits:
                stale.append(key)
        for key in stale:
            del store[key]

    _trim(_global_identify_hits, now, _DAY)


def _retry_after(hits: deque[float], now: float, window: float) -> int:
    """Seconds until the oldest hit falls out of the window (min 1)."""
    if not hits:
        return 1
    return max(1, int(hits[0] + window - now) + 1)


def _too_many(detail: str, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


def enforce(request: Request, *, scope: str, count_global: bool = False) -> None:
    """Apply per-IP and (optionally) global limits. Raises 429 when exceeded.

    `scope` namespaces the per-IP buckets by route, so the limits described
    in the plan apply per endpoint rather than being shared across all three.

    Nothing is recorded when a limit is hit, so a client being throttled
    cannot push its own reset time further away by continuing to retry.
    """
    ip = client_ip(request)
    key = f"{scope}:{ip}"
    now = time.monotonic()

    with _lock:
        _prune_if_due(now)

        minute_hits = _minute_hits.setdefault(key, deque())
        day_hits = _day_hits.setdefault(key, deque())

        _trim(minute_hits, now, _MINUTE)
        _trim(day_hits, now, _DAY)

        if len(minute_hits) >= config.RATE_LIMIT_PER_MIN:
            retry = _retry_after(minute_hits, now, _MINUTE)
            logger.info("Per-minute limit hit for %s on %s", ip, scope)
            raise _too_many(
                f"Rate limit reached ({config.RATE_LIMIT_PER_MIN} requests per minute). "
                f"Try again in {retry} seconds.",
                retry,
            )

        if len(day_hits) >= config.RATE_LIMIT_PER_DAY:
            retry = _retry_after(day_hits, now, _DAY)
            logger.info("Per-day limit hit for %s on %s", ip, scope)
            raise _too_many(
                f"Daily limit reached ({config.RATE_LIMIT_PER_DAY} requests per day). "
                f"Try again in about {max(1, retry // 3600)} hour(s).",
                retry,
            )

        if count_global:
            _trim(_global_identify_hits, now, _DAY)
            if len(_global_identify_hits) >= config.GLOBAL_IDENTIFY_PER_DAY:
                retry = _retry_after(_global_identify_hits, now, _DAY)
                logger.warning(
                    "GLOBAL daily identify ceiling (%d) reached.",
                    config.GLOBAL_IDENTIFY_PER_DAY,
                )
                raise _too_many(
                    "This app has reached its shared daily identification limit, "
                    "which protects its free species-ID quotas. Please try again tomorrow.",
                    retry,
                )
            _global_identify_hits.append(now)

        # Only recorded once every check above has passed.
        minute_hits.append(now)
        day_hits.append(now)


# --- FastAPI dependencies --------------------------------------------------
# Used as `dependencies=[Depends(limit_identify)]` on each route.

def limit_identify(request: Request) -> None:
    enforce(request, scope="identify", count_global=True)


def limit_create_sighting(request: Request) -> None:
    enforce(request, scope="sightings:create")


def limit_list_sightings(request: Request) -> None:
    enforce(request, scope="sightings:list")
