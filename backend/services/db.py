"""Supabase persistence for the sightings table.

We talk to Supabase over HTTPS (PostgREST) rather than opening a Postgres
connection. That is a deliberate deployment choice: Render's free tier has
no IPv6 outbound, and Supabase's free-tier *direct* Postgres connection is
IPv6-only, so a plain connection string fails there in a way that is
annoying to diagnose. HTTPS sidesteps it entirely.

supabase-py is synchronous, so every call is pushed to a worker thread.
Calling it directly from an async route would block the event loop and stall
every other request while one insert is in flight.
"""

import asyncio
import logging
from typing import Any, Optional

import config

logger = logging.getLogger("invasive-id.db")

TABLE = "sightings"

# Columns we read back. Explicit rather than "*" so a schema change cannot
# silently start leaking new columns to the public API.
_COLUMNS = "id,common_name,scientific_name,status,latitude,longitude,created_at"

_client: Optional[Any] = None
_client_failed = False


class DatabaseUnavailable(RuntimeError):
    """Raised when Supabase is not configured or not reachable."""


def _get_client():
    """Lazily create the Supabase client.

    Lazy so that a missing SUPABASE_URL does not stop the whole service from
    booting - /api/health should still come up and tell you what is wrong.
    """
    global _client, _client_failed

    if _client is not None:
        return _client
    if _client_failed:
        raise DatabaseUnavailable("Supabase client could not be created.")

    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        _client_failed = True
        raise DatabaseUnavailable("SUPABASE_URL and SUPABASE_KEY are not set.")

    try:
        from supabase import create_client

        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        logger.info("Supabase client initialised.")
        return _client
    except Exception as exc:  # noqa: BLE001 - surface any client init failure identically
        _client_failed = True
        logger.error("Failed to initialise Supabase client: %s", exc)
        raise DatabaseUnavailable(str(exc)) from exc


def _insert_sync(payload: dict) -> dict:
    client = _get_client()
    response = client.table(TABLE).insert(payload).execute()
    rows = response.data or []
    if not rows:
        raise DatabaseUnavailable("Insert returned no rows.")
    return rows[0]


def _list_sync(limit: int) -> list[dict]:
    client = _get_client()
    response = (
        client.table(TABLE)
        .select(_COLUMNS)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


async def insert_sighting(payload: dict) -> dict:
    """Insert one sighting and return the saved row.

    `created_at` is intentionally not sent: Postgres fills it with now(), so
    a client cannot backdate or forge a timestamp.
    """
    try:
        return await asyncio.to_thread(_insert_sync, payload)
    except DatabaseUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to insert sighting: %s", exc)
        raise DatabaseUnavailable(str(exc)) from exc


async def list_sightings(limit: int = 500) -> list[dict]:
    """Most recent sightings first, capped so one busy day cannot bloat the map."""
    try:
        return await asyncio.to_thread(_list_sync, limit)
    except DatabaseUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list sightings: %s", exc)
        raise DatabaseUnavailable(str(exc)) from exc
