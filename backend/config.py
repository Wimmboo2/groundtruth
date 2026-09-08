"""Environment-driven configuration.

Every secret comes from the environment. Nothing is hardcoded, so the same
image runs locally, on Render, or anywhere else without code changes.

Missing keys are logged loudly at startup but never crash the process: a
half-configured deploy should still boot and serve /api/health so you can
see *why* it is unhappy, instead of Render showing a dead service with no
explanation.
"""

import logging
import os

logger = logging.getLogger("invasive-id.config")


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to the default if it is unset or junk."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using default %s", name, raw, default)
        return default


# --- Secrets (set these on Render; never commit real values) ---------------

GEMINI_API_KEY = _env_str("GEMINI_API_KEY")
PLANTNET_API_KEY = _env_str("PLANTNET_API_KEY")
SUPABASE_URL = _env_str("SUPABASE_URL")
SUPABASE_KEY = _env_str("SUPABASE_KEY")

# --- CORS ------------------------------------------------------------------

# Comma-separated so you can allow the production Vercel domain *and* a
# preview URL at once. Discovering you need a second origin mid-demo is a
# bad time to find out this only accepted one value.
ALLOWED_ORIGIN = _env_str("ALLOWED_ORIGIN")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGIN.split(",") if o.strip()]

# --- Upstream service tunables --------------------------------------------

# Pinned rather than a floating alias. Flash-Lite carries roughly double the
# free-tier RPM of standard Flash, and there is no `gemini-flash-lite-latest`
# alias published, so we name the concrete model.
GEMINI_MODEL = _env_str("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# "all" is Pl@ntNet's worldwide flora project.
PLANTNET_PROJECT = _env_str("PLANTNET_PROJECT", "all")
PLANTNET_API_BASE = "https://my-api.plantnet.org/v2"

# Below this score we distrust Pl@ntNet and fall through to Gemini.
PLANTNET_MIN_SCORE = _env_float("PLANTNET_MIN_SCORE", 0.15)

INATURALIST_API_BASE = "https://api.inaturalist.org/v1"
# iNaturalist place id 1 == United States. Used when we have no coordinates
# or when the nearby-place lookup fails.
INATURALIST_FALLBACK_PLACE_ID = 1

# Upstream calls are wrapped in these timeouts so one slow provider cannot
# hang a request forever. Render's free tier is already slow enough.
HTTP_TIMEOUT_SECONDS = _env_float("HTTP_TIMEOUT_SECONDS", 25.0)

# --- Uploads ---------------------------------------------------------------

MAX_UPLOAD_BYTES = _env_int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)  # 10 MB
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}

# --- Rate limiting ---------------------------------------------------------

# Per-IP limits applied to every API route. Env-configurable so they can be
# tuned live from the Render dashboard without a redeploy.
RATE_LIMIT_PER_MIN = _env_int("RATE_LIMIT_PER_MIN", 5)
RATE_LIMIT_PER_DAY = _env_int("RATE_LIMIT_PER_DAY", 20)

# Global ceiling across *all* callers. This is what actually protects the
# Pl@ntNet and Gemini free quotas from distributed traffic, which a per-IP
# limit cannot do.
GLOBAL_IDENTIFY_PER_DAY = _env_int("GLOBAL_IDENTIFY_PER_DAY", 300)

# --- Startup diagnostics ---------------------------------------------------

def missing_required() -> list[str]:
    """Names of required settings that are absent."""
    required = {
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "PLANTNET_API_KEY": PLANTNET_API_KEY,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
        "ALLOWED_ORIGIN": ALLOWED_ORIGIN,
    }
    return [name for name, value in required.items() if not value]


def log_startup_summary() -> None:
    """Log what is configured. Never logs a secret value, only whether it is set."""
    missing = missing_required()
    if missing:
        logger.warning(
            "Missing required environment variables: %s. "
            "The service will start, but features depending on them will fail.",
            ", ".join(missing),
        )
    else:
        logger.info("All required environment variables are set.")

    logger.info("Gemini model: %s", GEMINI_MODEL)
    logger.info("Pl@ntNet project: %s (min score %.2f)", PLANTNET_PROJECT, PLANTNET_MIN_SCORE)
    logger.info("Allowed CORS origins: %s", ALLOWED_ORIGINS or "(none set - browser calls will be blocked)")
    logger.info(
        "Rate limits: %d/min, %d/day per IP; global %d identifies/day",
        RATE_LIMIT_PER_MIN,
        RATE_LIMIT_PER_DAY,
        GLOBAL_IDENTIFY_PER_DAY,
    )
