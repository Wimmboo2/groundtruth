/* Groundtruth — the one value you edit after deploying the backend.
 *
 * Set API_BASE to your Render URL, with no trailing slash:
 *   https://groundtruth-api.onrender.com
 *
 * Leave it as "" only if the API is served from this same origin.
 */
window.GT_CONFIG = {
  API_BASE: "https://groundtruth-api-xwdw.onrender.com",

  // Client-side guards. Keep these in step with the backend, which enforces
  // the real limits — these exist so nobody waits out a 14 MB upload to be
  // told no.
  MAX_UPLOAD_BYTES: 10 * 1024 * 1024,
  ALLOWED_TYPES: ["image/jpeg", "image/jpg", "image/png", "image/webp"],

  // The cold-start notice appears at this mark, not at zero. Before then the
  // wait is normal, and saying otherwise would invent a problem.
  COLD_START_NOTICE_MS: 10000,

  // Map defaults when we have no sightings and no location: continental US.
  MAP_DEFAULT_CENTER: [39.5, -98.35],
  MAP_DEFAULT_ZOOM: 4
};
