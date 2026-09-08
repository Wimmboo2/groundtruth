"""Pl@ntNet client - the primary identification service.

Pl@ntNet also doubles as our plant/not-plant router. When its model decides
an image is not a plant (an insect, an animal, a landscape, a person), it
responds with HTTP 404 "Species not found". We deliberately do NOT send
`no-reject=true`, because that rejection is exactly the signal we want: a
404 means "hand this to Gemini instead", and it costs us nothing.

Every failure mode returns None rather than raising, so the caller can fall
through to Gemini uniformly.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

import config

logger = logging.getLogger("invasive-id.plantnet")


@dataclass
class PlantNetResult:
    common_name: str
    scientific_name: str
    confidence: float
    family: Optional[str] = None
    genus: Optional[str] = None


def _parse(payload: dict) -> Optional[PlantNetResult]:
    """Pull the top match out of a Pl@ntNet response.

    Shape (verified against the live API):
      results[0].score
      results[0].species.scientificNameWithoutAuthor
      results[0].species.commonNames[]
      results[0].species.genus.scientificName
      results[0].species.family.scientificName
    """
    results = payload.get("results") or []
    if not results:
        return None

    top = results[0] or {}
    species = top.get("species") or {}

    scientific_name = (species.get("scientificNameWithoutAuthor") or "").strip()
    if not scientific_name:
        # Without a scientific name we cannot query iNaturalist or match the
        # curated list, so this result is useless to us.
        return None

    try:
        score = float(top.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0

    common_names = species.get("commonNames") or []
    # commonNames is frequently empty; the scientific name is a better label
    # than a blank one.
    common_name = next((n.strip() for n in common_names if n and n.strip()), scientific_name)

    genus = (species.get("genus") or {}).get("scientificName")
    family = (species.get("family") or {}).get("scientificName")

    return PlantNetResult(
        common_name=common_name,
        scientific_name=scientific_name,
        confidence=score,
        genus=genus,
        family=family,
    )


async def identify(
    image_bytes: bytes,
    filename: str,
    content_type: str,
) -> Optional[PlantNetResult]:
    """Identify a plant. Returns None when Pl@ntNet rejects, errors, or is unsure.

    A None return is normal, not exceptional - it is how the caller learns to
    try Gemini.
    """
    if not config.PLANTNET_API_KEY:
        logger.warning("PLANTNET_API_KEY is not set; skipping Pl@ntNet.")
        return None

    url = f"{config.PLANTNET_API_BASE}/identify/{config.PLANTNET_PROJECT}"
    params = {
        "api-key": config.PLANTNET_API_KEY,
        "nb-results": 5,
        "lang": "en",
        # `no-reject` is intentionally omitted. See the module docstring.
    }
    files = {"images": (filename or "upload.jpg", image_bytes, content_type)}

    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, params=params, files=files)
    except httpx.TimeoutException:
        logger.warning("Pl@ntNet timed out; falling back.")
        return None
    except httpx.HTTPError as exc:
        logger.warning("Pl@ntNet request failed: %s", exc)
        return None

    if response.status_code == 404:
        # The expected "this is not a plant" path.
        logger.info("Pl@ntNet rejected the image (404) - routing to Gemini.")
        return None

    if response.status_code in (401, 403):
        logger.error("Pl@ntNet rejected the API key (HTTP %s). Check PLANTNET_API_KEY.", response.status_code)
        return None

    if response.status_code == 429:
        logger.error("Pl@ntNet quota exhausted (429). Falling back to Gemini.")
        return None

    if response.status_code != 200:
        logger.warning("Pl@ntNet returned HTTP %s: %s", response.status_code, response.text[:300])
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Pl@ntNet returned a non-JSON body.")
        return None

    remaining = payload.get("remainingIdentificationRequests")
    if isinstance(remaining, int) and remaining < 50:
        logger.warning("Pl@ntNet quota is running low: %d requests remaining.", remaining)

    result = _parse(payload)
    if result is None:
        logger.info("Pl@ntNet returned no usable result.")
        return None

    if result.confidence < config.PLANTNET_MIN_SCORE:
        logger.info(
            "Pl@ntNet match '%s' scored %.3f, below the %.2f threshold - falling back to Gemini.",
            result.scientific_name,
            result.confidence,
            config.PLANTNET_MIN_SCORE,
        )
        return None

    return result
