"""Invasive species identification API.

Flow for POST /api/identify:

  photo -> Pl@ntNet (also acts as the plant/not-plant router)
        -> Gemini vision, only if Pl@ntNet rejects or is unsure
        -> iNaturalist: is this species native at these coordinates?
        -> curated invasive list: is it a recognised problem?
        -> combined status
        -> Gemini explainer, grounded on curated facts where we have them

The design rule throughout: an identification should degrade, never crash.
Any single upstream service can be down, slow, rate-limited or wrong, and
the endpoint still returns a valid, renderable result.
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
import ratelimit
from schemas import (
    Explainer,
    HealthResponse,
    IdentifyResponse,
    Sighting,
    SightingCreate,
)
from services import db, gemini, inaturalist, invasive, plantnet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("invasive-id")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.log_startup_summary()
    # Load the curated list at boot so a malformed JSON file shows up in the
    # deploy logs rather than on a judge's first upload.
    if invasive.loaded_species_count() == 0:
        logger.error("Curated invasive species list is empty or failed to load.")
    yield


app = FastAPI(
    title="Invasive Species ID API",
    description="Identify a species from a photo and find out whether it is a problem where you are.",
    version="1.0.0",
    lifespan=lifespan,
)

# Restricted to the origins named in ALLOWED_ORIGIN. If that is unset, browser
# calls will be blocked - which is the safe default, and the startup log says so.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --- Helpers ---------------------------------------------------------------

def _parse_coord(raw: Optional[str], lo: float, hi: float) -> Optional[float]:
    """Parse an optional coordinate from form data.

    Tolerant on purpose: a browser that sends an empty string for a declined
    geolocation prompt should be treated as "no location", not as a 422.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in ("null", "undefined", "nan"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value or not (lo <= value <= hi):  # NaN or out of range
        return None
    return value


def _unidentified(reason: str) -> IdentifyResponse:
    """A clean, renderable 'we could not identify this' result.

    Returned with HTTP 200 so the frontend renders a friendly card instead of
    having to catch an exception for an outcome that is entirely normal.
    """
    return IdentifyResponse(
        common_name="Unknown",
        scientific_name="",
        id_source="none",
        confidence=0.0,
        status="unknown",
        explainer=Explainer(
            summary=reason,
            what_to_do=[
                "Take another photo in good light, filling the frame with the organism",
                "Focus on distinctive features - leaves and flowers for plants, markings for insects and animals",
                "Avoid photographing several different species at once",
            ],
        ),
    )


# --- Routes ----------------------------------------------------------------

@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness plus a configuration report.

    Deliberately exempt from rate limiting so it stays usable as a warm-up
    ping against Render's free-tier cold starts.
    """
    return HealthResponse(ok=True, missing_config=config.missing_required())


@app.post(
    "/api/identify",
    response_model=IdentifyResponse,
    dependencies=[Depends(ratelimit.limit_identify)],
)
async def identify(
    photo: UploadFile = File(...),
    lat: Optional[str] = Form(None),
    lng: Optional[str] = Form(None),
) -> IdentifyResponse:
    content_type = (photo.content_type or "").split(";")[0].strip().lower()
    if content_type not in config.ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Please upload a JPEG, PNG or WebP image.",
        )
    # Some clients send the non-standard "image/jpg". Gemini only accepts the
    # real MIME type, so normalise before it reaches any upstream service.
    if content_type == "image/jpg":
        content_type = "image/jpeg"

    # Read once and reuse. Reading twice would hand Pl@ntNet the bytes and
    # Gemini an empty stream.
    image_bytes = await photo.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    if len(image_bytes) > config.MAX_UPLOAD_BYTES:
        limit_mb = config.MAX_UPLOAD_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"That image is too large. Please upload a photo under {limit_mb:.0f} MB.",
        )

    latitude = _parse_coord(lat, -90.0, 90.0)
    longitude = _parse_coord(lng, -180.0, 180.0)
    # A single coordinate on its own is meaningless.
    if latitude is None or longitude is None:
        latitude = longitude = None

    # 1. Pl@ntNet first. A None result means "not a plant, or not sure".
    common_name = scientific_name = ""
    confidence = 0.0
    id_source: str = "none"

    pn = await plantnet.identify(image_bytes, photo.filename or "upload.jpg", content_type)
    if pn is not None:
        common_name = pn.common_name
        scientific_name = pn.scientific_name
        confidence = pn.confidence
        id_source = "plantnet"
    else:
        # 2. Gemini vision fallback - insects, animals, fish, fungi.
        gm = await gemini.identify_organism(image_bytes, content_type)
        if gm is None:
            return _unidentified(
                "We could not identify this photo. Our plant service did not recognise it, "
                "and our backup identification service is unavailable right now."
            )
        if not gm.is_organism or not gm.scientific_name:
            return _unidentified(
                "We could not find a clear plant or animal in this photo. "
                "Try getting closer, with the organism filling most of the frame."
            )
        common_name = gm.common_name
        scientific_name = gm.scientific_name
        confidence = gm.confidence
        id_source = "gemini"

    # 3. Where are we, and is this species native there?
    place_id, place_name = await inaturalist.resolve_place_id(latitude, longitude)
    establishment_means = await inaturalist.get_establishment_means(scientific_name, place_id)

    # 4 & 5. Curated list, then combined status.
    curated = invasive.find_curated(scientific_name, common_name)
    status = invasive.resolve_status(curated, establishment_means)

    logger.info(
        "Identified %r (%s) via %s: status=%s, means=%s, place=%s",
        common_name,
        scientific_name,
        id_source,
        status,
        establishment_means or "unknown",
        place_name or f"place#{place_id}",
    )

    # 6. Explainer. Falls back to curated text if Gemini is unavailable, so
    # this endpoint never fails just because the model had a bad minute.
    explainer_data = await gemini.generate_explainer(
        common_name=common_name,
        scientific_name=scientific_name,
        status=status,
        place_name=place_name,
        establishment_means=establishment_means,
        curated=curated,
    )
    if not explainer_data or not explainer_data.get("what_to_do"):
        fallback = invasive.fallback_explainer(status, common_name, curated, place_name)
        if explainer_data and explainer_data.get("summary"):
            # Keep a good summary, borrow the actions.
            explainer_data["what_to_do"] = fallback["what_to_do"]
        else:
            explainer_data = fallback

    return IdentifyResponse(
        common_name=common_name,
        scientific_name=scientific_name,
        id_source=id_source,  # type: ignore[arg-type]
        confidence=round(min(max(confidence, 0.0), 1.0), 3),
        status=status,  # type: ignore[arg-type]
        explainer=Explainer(**explainer_data),
        establishment_means=establishment_means,
        place_name=place_name,
        category=invasive.category_of(curated),
        curated_match=curated is not None,
    )


@app.post(
    "/api/sightings",
    response_model=Sighting,
    status_code=201,
    dependencies=[Depends(ratelimit.limit_create_sighting)],
)
async def create_sighting(sighting: SightingCreate) -> Sighting:
    # created_at is omitted on purpose - Postgres generates it.
    payload = sighting.model_dump()

    try:
        row = await db.insert_sighting(payload)
    except db.DatabaseUnavailable as exc:
        logger.error("Could not save sighting: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Could not save your sighting right now. Please try again shortly.",
        ) from exc

    return Sighting(**row)


@app.get(
    "/api/sightings",
    response_model=list[Sighting],
    dependencies=[Depends(ratelimit.limit_list_sightings)],
)
async def list_sightings() -> list[Sighting]:
    try:
        rows = await db.list_sightings(limit=500)
    except db.DatabaseUnavailable as exc:
        logger.error("Could not list sightings: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Could not load sightings right now. Please try again shortly.",
        ) from exc

    sightings: list[Sighting] = []
    for row in rows:
        try:
            sightings.append(Sighting(**row))
        except Exception:  # noqa: BLE001
            # One malformed row should not blank the whole map.
            logger.warning("Skipping malformed sighting row: %r", row)
    return sightings


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "Invasive Species ID API",
            "docs": "/docs",
            "health": "/api/health",
        }
    )
