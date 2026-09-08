"""Gemini client - vision fallback for non-plants, plus the explainer text.

Two jobs:

1. `identify_organism` - runs when Pl@ntNet rejects an image or is unsure.
   Handles insects, animals, fish and anything else Pl@ntNet won't touch.
2. `generate_explainer` - turns a status into human guidance. For species on
   the curated list we feed the vetted facts into the prompt so the model
   grounds on them rather than improvising control advice, which matters
   when that advice concerns herbicides or a plant that burns skin.

Both use structured output (response_mime_type + response_schema) so we get
parseable JSON instead of prose we'd have to scrape. Both return None on
failure; the caller always has a fallback.
"""

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

import config

logger = logging.getLogger("invasive-id.gemini")


@dataclass
class GeminiIdResult:
    common_name: str
    scientific_name: str
    confidence: float
    is_organism: bool


_ID_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "is_organism": {
            "type": "BOOLEAN",
            "description": "True only if the image clearly shows a living organism (plant, insect, animal, fish, fungus).",
        },
        "common_name": {"type": "STRING"},
        "scientific_name": {
            "type": "STRING",
            "description": "Binomial name, e.g. 'Lycorma delicatula'. Empty string if unsure.",
        },
        "confidence": {
            "type": "NUMBER",
            "description": "Confidence in the identification from 0.0 to 1.0.",
        },
    },
    "required": ["is_organism", "common_name", "scientific_name", "confidence"],
}

_EXPLAINER_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "summary": {
            "type": "STRING",
            "description": "Two or three sentences, plain language, explaining what this status means for this person here.",
        },
        "what_to_do": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Three to five concrete, specific actions.",
        },
    },
    "required": ["summary", "what_to_do"],
}

_ID_PROMPT = """You are identifying an organism from a photograph for an invasive species reporting app.

Identify the single most prominent living organism in this image.

Rules:
- Set is_organism to false if the image shows no clear living organism (a landscape, a building, a person, an object, a blurry mess). Do not guess in that case.
- Give the most specific scientific name you can justify. Prefer a binomial (genus + species). If you can only reach genus level, give the genus and lower your confidence.
- common_name should be the name an ordinary person would recognise.
- Be honest in `confidence`. A blurry or partial photo should score low. Overconfidence here causes real harm, because the answer drives advice about pesticides and plant removal.
"""


def _endpoint() -> str:
    return f"{config.GEMINI_API_BASE}/models/{config.GEMINI_MODEL}:generateContent"


async def _call(parts: list[dict], schema: dict, *, max_tokens: int) -> Optional[dict]:
    """POST to Gemini and return the parsed JSON object, or None on any failure."""
    if not config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not set; skipping Gemini.")
        return None

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": schema,
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
        },
    }
    headers = {
        # Header auth keeps the key out of URLs and request logs.
        "x-goog-api-key": config.GEMINI_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(_endpoint(), headers=headers, json=body)
    except httpx.TimeoutException:
        logger.warning("Gemini timed out.")
        return None
    except httpx.HTTPError as exc:
        logger.warning("Gemini request failed: %s", exc)
        return None

    if response.status_code == 429:
        logger.error("Gemini rate limit hit (429). Check your free-tier quota in AI Studio.")
        return None
    if response.status_code in (401, 403):
        logger.error("Gemini rejected the API key (HTTP %s). Check GEMINI_API_KEY.", response.status_code)
        return None
    if response.status_code == 404:
        logger.error(
            "Gemini model %r not found (404). Set GEMINI_MODEL to a model your key can access.",
            config.GEMINI_MODEL,
        )
        return None
    if response.status_code != 200:
        logger.warning("Gemini returned HTTP %s: %s", response.status_code, response.text[:300])
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Gemini returned a non-JSON body.")
        return None

    block_reason = (payload.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        logger.warning("Gemini blocked the prompt: %s", block_reason)
        return None

    candidates = payload.get("candidates") or []
    if not candidates:
        logger.warning("Gemini returned no candidates.")
        return None

    candidate = candidates[0] or {}
    finish_reason = candidate.get("finishReason")
    if finish_reason and finish_reason not in ("STOP", "MAX_TOKENS"):
        # SAFETY, RECITATION, etc. - the content is unusable.
        logger.warning("Gemini stopped early: %s", finish_reason)
        return None

    parts_out = ((candidate.get("content") or {}).get("parts")) or []
    # Concatenate text parts; a truncated response can arrive split.
    text = "".join(p.get("text", "") for p in parts_out).strip()
    if not text:
        logger.warning("Gemini returned an empty text part.")
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        if finish_reason == "MAX_TOKENS":
            logger.warning("Gemini response was truncated by maxOutputTokens; JSON incomplete.")
        else:
            logger.warning("Gemini returned unparseable JSON: %s", text[:300])
        return None

    if not isinstance(parsed, dict):
        logger.warning("Gemini returned JSON that is not an object.")
        return None

    return parsed


async def identify_organism(image_bytes: bytes, mime_type: str) -> Optional[GeminiIdResult]:
    """Identify anything in the photo. Used when Pl@ntNet declines."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    parts = [
        {"text": _ID_PROMPT},
        {"inline_data": {"mime_type": mime_type, "data": encoded}},
    ]

    data = await _call(parts, _ID_SCHEMA, max_tokens=512)
    if data is None:
        return None

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    scientific_name = str(data.get("scientific_name") or "").strip()
    common_name = str(data.get("common_name") or "").strip() or scientific_name

    return GeminiIdResult(
        common_name=common_name,
        scientific_name=scientific_name,
        confidence=confidence,
        is_organism=bool(data.get("is_organism")),
    )


async def generate_explainer(
    *,
    common_name: str,
    scientific_name: str,
    status: str,
    place_name: Optional[str],
    establishment_means: Optional[str],
    curated: Optional[dict] = None,
) -> Optional[dict]:
    """Write the summary and action list. Returns {'summary', 'what_to_do'} or None."""
    location = place_name or "the United States"

    context_lines = [
        f"Species: {common_name} ({scientific_name})",
        f"Location: {location}",
        f"Status determined by the app: {status}",
    ]
    if establishment_means:
        context_lines.append(f"iNaturalist establishment means for this place: {establishment_means}")

    if curated:
        # Ground the model on vetted facts so it does not improvise removal or
        # safety advice. This is the difference between "cut it back" and
        # "do not touch this, its sap causes burns".
        context_lines.append(
            "This species is on our curated invasive list. Use these verified facts as your source of truth:"
        )
        context_lines.append(f"- Why it matters: {curated.get('why_it_matters', '')}")
        actions = curated.get("what_to_do") or []
        if actions:
            context_lines.append("- Recommended actions: " + " | ".join(actions))

    if status == "invasive":
        instruction = (
            "Explain why this species is a problem in this location and what the reader should do. "
            "Be direct and practical. If the curated facts include a safety warning, lead with it."
        )
    elif status == "introduced":
        instruction = (
            "Explain that this species is non-native here but is not on our list of recognised problem species. "
            "Be measured - do not alarm the reader or tell them to destroy it. Suggest monitoring and reporting."
        )
    elif status == "native":
        instruction = (
            "Explain that this species belongs here and is part of the local ecosystem. "
            "Be reassuring. Suggest ways to appreciate or support it. Do NOT suggest removing it."
        )
    else:
        instruction = (
            "We could not confirm this species' status in this location. Explain that honestly, "
            "and suggest how the reader could find out more. Do not guess at a status."
        )

    prompt = (
        "You are writing for a public invasive species app used by ordinary people, not scientists.\n\n"
        + "\n".join(context_lines)
        + "\n\n"
        + instruction
        + "\n\nWrite `summary` as two or three plain-language sentences. "
        "Write `what_to_do` as three to five short, concrete actions someone could actually take today. "
        "Avoid jargon. Never recommend handling a dangerous organism bare-handed. "
        "Do not recommend a specific herbicide product by brand name."
    )

    data = await _call([{"text": prompt}], _EXPLAINER_SCHEMA, max_tokens=800)
    if data is None:
        return None

    summary = str(data.get("summary") or "").strip()
    raw_actions = data.get("what_to_do")
    actions = [str(a).strip() for a in raw_actions if str(a).strip()] if isinstance(raw_actions, list) else []

    if not summary:
        return None

    return {"summary": summary, "what_to_do": actions}
