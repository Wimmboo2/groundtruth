"""Curated invasive list matching and final status resolution.

This module is the actual differentiator of the app. Identification tells
you *what* something is; this decides whether it is a problem *here*.

Matching has to be forgiving, because three different services name things
three different ways: Pl@ntNet returns clean binomials, Gemini sometimes
appends an authority, and taxonomy shifts underneath all of it (water
hyacinth moved from Eichhornia to Pontederia; Japanese knotweed has three
names in common use). Aliases in the JSON absorb most of that.

It also has to be forgiving in only one direction. Matching too loosely is
worse than missing a match: telling someone their ordinary garden honey bee
is an aggressive invasive is a real failure, so subspecies entries such as
Apis mellifera scutellata deliberately never match on their binomial alone.
"""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger("invasive-id.invasive")

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "invasive_species.json"

# Infraspecific rank markers that carry no matching information.
_RANK_MARKERS = {"var", "subsp", "ssp", "sp", "f", "cv", "forma", "x"}

# Establishment means values that mean "this belongs here".
_NATIVE_MEANS = {"native", "endemic"}
_INTRODUCED_MEANS = {"introduced", "naturalised", "naturalized", "invasive"}


def _normalize(name: str) -> str:
    """Lowercase a scientific name and strip authorship, punctuation and rank markers.

    'Pueraria montana var. lobata'    -> 'pueraria montana lobata'
    'Lycorma delicatula (White, 1845)' -> 'lycorma delicatula white'
    """
    if not name:
        return ""

    text = name.lower()
    # Authorities are usually parenthesised; drop them wholesale.
    text = re.sub(r"\([^)]*\)", " ", text)
    # Keep letters, spaces and hyphens; digits in an authority are noise.
    text = re.sub(r"[^a-z\s-]", " ", text)

    tokens = [t for t in text.split() if t and t not in _RANK_MARKERS]
    return " ".join(tokens)


def _binomial(normalized: str) -> str:
    """First two tokens - genus + species."""
    parts = normalized.split()
    return " ".join(parts[:2]) if len(parts) >= 2 else normalized


@lru_cache(maxsize=1)
def _load() -> tuple[list[dict], dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Load the curated list and build lookup indexes.

    Returns (entries, exact_index, binomial_index, common_name_index).

    Only entries whose canonical name is itself a binomial go into the
    binomial index. That is what stops 'Apis mellifera' matching the
    Africanized subspecies.
    """
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not load curated species list from %s: %s", _DATA_PATH, exc)
        return [], {}, {}, {}

    entries = raw.get("species") or []
    exact: dict[str, dict] = {}
    binomial: dict[str, dict] = {}
    common: dict[str, dict] = {}

    for entry in entries:
        names = [entry.get("scientific_name", "")] + list(entry.get("aliases") or [])
        for name in names:
            normalized = _normalize(name)
            if not normalized:
                continue
            exact.setdefault(normalized, entry)
            if len(normalized.split()) == 2:
                binomial.setdefault(normalized, entry)

        for label in [entry.get("common_name", "")] + list(entry.get("aliases") or []):
            key = " ".join(str(label).lower().split())
            if key:
                common.setdefault(key, entry)

    logger.info("Loaded %d curated invasive species.", len(entries))
    return entries, exact, binomial, common


def find_curated(scientific_name: str, common_name: str = "") -> Optional[dict]:
    """Look a species up in the curated list. Returns the entry or None."""
    _, exact, binomial_index, common_index = _load()

    normalized = _normalize(scientific_name)
    if normalized:
        if normalized in exact:
            return exact[normalized]
        # 'Pueraria montana var. lobata' should still find kudzu.
        candidate = _binomial(normalized)
        if candidate in binomial_index:
            return binomial_index[candidate]

    # Common names are a weaker signal, so they are only consulted when the
    # scientific name found nothing.
    key = " ".join((common_name or "").lower().split())
    if key and key in common_index:
        return common_index[key]

    return None


def resolve_status(
    curated: Optional[dict],
    establishment_means: Optional[str],
) -> str:
    """Combine the curated list and iNaturalist into one status.

    - On the curated list, and iNaturalist does not call it native -> invasive
    - Non-native according to iNaturalist                          -> introduced
    - Native or endemic                                            -> native
    - Nothing conclusive                                           -> unknown
    """
    means = (establishment_means or "").strip().lower()

    if curated:
        # The native guard. A curated species photographed inside its own
        # native range is not invasive there, and claiming otherwise is the
        # kind of error that undermines the whole app.
        if means in _NATIVE_MEANS:
            return "native"
        return "invasive"

    if means in _INTRODUCED_MEANS:
        return "introduced"
    if means in _NATIVE_MEANS:
        return "native"
    return "unknown"


def fallback_explainer(
    status: str,
    common_name: str,
    curated: Optional[dict],
    place_name: Optional[str],
) -> dict:
    """Explainer text used when Gemini is unavailable.

    The app must still say something useful if the model is rate-limited or
    down, so the curated entry is used verbatim where we have one.
    """
    location = place_name or "your area"

    if curated:
        return {
            "summary": (
                f"{common_name} is a recognised invasive species in the United States. "
                f"{curated.get('why_it_matters', '')}"
            ).strip(),
            "what_to_do": list(curated.get("what_to_do") or []),
        }

    if status == "introduced":
        return {
            "summary": (
                f"{common_name} is not native to {location}, but it is not on our list of "
                "recognised problem species. Non-native does not automatically mean harmful - "
                "many introduced species coexist without causing damage."
            ),
            "what_to_do": [
                "Log the sighting so its spread can be tracked over time",
                "Keep an eye on whether it is spreading in your area",
                "Avoid deliberately planting or releasing it",
                "Check your state's invasive species list for local guidance",
            ],
        }

    if status == "native":
        return {
            "summary": (
                f"{common_name} is native to {location}. It belongs in this ecosystem and "
                "supports the local wildlife that evolved alongside it."
            ),
            "what_to_do": [
                "Leave it in place - native species are part of a healthy ecosystem",
                "Consider planting more natives like it to support local wildlife",
                "Learn which local species depend on it",
            ],
        }

    return {
        "summary": (
            f"We could not confirm whether {common_name} is native to {location}. "
            "This can happen with uncommon species, or when the photo was not clear enough "
            "for a confident identification."
        ),
        "what_to_do": [
            "Try another photo with better light and a closer, sharper view",
            "Include distinctive features such as leaves, flowers or markings",
            "Check with your local extension office or a regional plant identification group",
        ],
    }


def category_of(curated: Optional[dict]) -> Optional[str]:
    return curated.get("category") if curated else None


def loaded_species_count() -> int:
    """How many curated species loaded. Used by the startup check."""
    entries, *_ = _load()
    return len(entries)
