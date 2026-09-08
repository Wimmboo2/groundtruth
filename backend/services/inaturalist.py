"""iNaturalist client - answers "is this species native *here*?".

Two hops, because a browser gives us coordinates but iNaturalist wants a
place id:

  1. GET /v1/places/nearby  - coordinates -> place id (prefer state level)
  2. GET /v1/taxa           - species + place id -> establishment_means

No authentication required. Both hops degrade to None on any failure, so a
slow or unreachable iNaturalist never breaks an identification.
"""

import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger("invasive-id.inaturalist")

# iNaturalist admin_level values: -10 continent, 0 country, 10 state, 20 county.
_STATE_LEVEL = 10
_COUNTRY_LEVEL = 0

# Coordinates -> (place_id, place_name). Cached because judges photographing
# things in the same place shouldn't trigger a lookup every time. Keyed on
# coordinates rounded to ~1 km, which is far finer than state boundaries.
_place_cache: dict[tuple[float, float], tuple[int, Optional[str]]] = {}

# scientific_name + place_id -> establishment means.
_means_cache: dict[tuple[str, int], Optional[str]] = {}

# Cheap protection against unbounded growth over a long-running deploy.
_MAX_CACHE_ENTRIES = 500


def _remember(cache: dict, key, value):
    if len(cache) >= _MAX_CACHE_ENTRIES:
        cache.clear()
    cache[key] = value
    return value


def _pick_place(places: list[dict]) -> Optional[dict]:
    """Choose the most useful place from a nearby-places list.

    A state is the sweet spot: specific enough that "introduced in Georgia"
    means something, broad enough that iNaturalist actually has establishment
    data for it. Counties often have none.
    """
    if not places:
        return None

    for level in (_STATE_LEVEL, _COUNTRY_LEVEL):
        matches = [p for p in places if p.get("admin_level") == level]
        if matches:
            # Smallest area wins when several places share a level.
            return min(matches, key=lambda p: p.get("bbox_area") or float("inf"))

    return min(places, key=lambda p: p.get("bbox_area") or float("inf"))


async def resolve_place_id(
    lat: Optional[float],
    lng: Optional[float],
) -> tuple[int, Optional[str]]:
    """Coordinates -> (place_id, place_name).

    Falls back to place id 1 (United States) whenever coordinates are absent
    or the lookup fails, since this app is scoped to the US.
    """
    if lat is None or lng is None:
        return config.INATURALIST_FALLBACK_PLACE_ID, None

    cache_key = (round(lat, 2), round(lng, 2))
    if cache_key in _place_cache:
        return _place_cache[cache_key]

    # A small box around the point. Too tight and the endpoint can return
    # nothing; this is roughly 10 km and reliably catches the enclosing state.
    delta = 0.05
    params = {
        "nelat": lat + delta,
        "nelng": lng + delta,
        "swlat": lat - delta,
        "swlng": lng - delta,
        "per_page": 30,
    }

    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{config.INATURALIST_API_BASE}/places/nearby", params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("iNaturalist places/nearby failed: %s", exc)
        return config.INATURALIST_FALLBACK_PLACE_ID, None

    # results is an object of lists here, not a list: {standard: [], community: []}
    results = payload.get("results") or {}
    candidates = list(results.get("standard") or [])
    if not candidates:
        candidates = list(results.get("community") or [])

    place = _pick_place(candidates)
    if not place or not place.get("id"):
        return config.INATURALIST_FALLBACK_PLACE_ID, None

    resolved = (
        int(place["id"]),
        place.get("display_name") or place.get("name"),
    )
    logger.info("Resolved (%.4f, %.4f) to place %s (id %d)", lat, lng, resolved[1], resolved[0])
    return _remember(_place_cache, cache_key, resolved)


async def get_establishment_means(scientific_name: str, place_id: int) -> Optional[str]:
    """Return 'native', 'introduced', 'endemic', etc. for a species in a place.

    Returns None when iNaturalist has no opinion, which is common for
    genus-level names and obscure taxa.
    """
    name = (scientific_name or "").strip()
    if not name:
        return None

    cache_key = (name.lower(), place_id)
    if cache_key in _means_cache:
        return _means_cache[cache_key]

    params = {
        "q": name,
        "rank": "species",
        "preferred_place_id": place_id,
        "per_page": 5,
    }

    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{config.INATURALIST_API_BASE}/taxa", params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("iNaturalist taxa lookup failed for %r: %s", name, exc)
        return None

    results = payload.get("results") or []
    if not results:
        return _remember(_means_cache, cache_key, None)

    # Prefer an exact name match; iNaturalist's fuzzy search can put a
    # different species first.
    target = name.lower()
    taxon = next((t for t in results if (t.get("name") or "").lower() == target), results[0])

    means = None
    establishment = taxon.get("establishment_means")
    if isinstance(establishment, dict):
        means = establishment.get("establishment_means")
    if not means:
        means = taxon.get("preferred_establishment_means")

    means = (means or "").strip().lower() or None
    logger.info("iNaturalist: %s in place %d -> %s", name, place_id, means or "no data")
    return _remember(_means_cache, cache_key, means)
