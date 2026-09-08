"""Pydantic request/response models.

These define the contract the frontend consumes. The identify response
matches the shape agreed in the plan, plus two additive fields
(`establishment_means`, `place_name`) the result card uses for context.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

# Shared vocabulary. `unknown` exists so a failed identification is still a
# valid, renderable result rather than an error the frontend has to catch.
Status = Literal["invasive", "introduced", "native", "unknown"]
IdSource = Literal["plantnet", "gemini", "none"]


class Explainer(BaseModel):
    summary: str
    what_to_do: list[str] = Field(default_factory=list)


class IdentifyResponse(BaseModel):
    common_name: str
    scientific_name: str
    id_source: IdSource
    confidence: float = Field(ge=0.0, le=1.0)
    status: Status
    explainer: Explainer

    # Additive context, not part of the minimum contract.
    establishment_means: Optional[str] = None
    place_name: Optional[str] = None
    category: Optional[str] = None
    # True when the species matched the curated invasive list, so the UI can
    # distinguish "we have vetted guidance for this" from a generic answer.
    curated_match: bool = False


class SightingCreate(BaseModel):
    """Client-supplied sighting. `created_at` is deliberately absent - the
    database generates it, so a client cannot backdate or forge a timestamp."""

    common_name: str = Field(min_length=1, max_length=200)
    scientific_name: str = Field(min_length=1, max_length=200)
    status: Status
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class Sighting(SightingCreate):
    id: int
    created_at: str


class HealthResponse(BaseModel):
    ok: bool
    missing_config: list[str] = Field(default_factory=list)
