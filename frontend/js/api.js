/* Groundtruth API client.
 *
 * Every failure is normalised into a GTError with a `kind`, so the UI can
 * branch on the kind rather than sniffing status codes in three places.
 * The kinds map onto genuinely different screens:
 *
 *   ratelimit  -> the countdown screen (429, driven by Retry-After)
 *   network    -> "we cannot reach Groundtruth", auto-retrying
 *   upload     -> inline rejection on the screen you were already on
 *   server     -> a 5xx that is ours, not the user's
 */
(function () {
  "use strict";

  var CFG = window.GT_CONFIG || {};
  var BASE = (CFG.API_BASE || "").replace(/\/+$/, "");

  function GTError(kind, message, extra) {
    var err = new Error(message);
    err.kind = kind;
    if (extra) Object.keys(extra).forEach(function (k) { err[k] = extra[k]; });
    return err;
  }

  function url(path) { return BASE + path; }

  /* Pull the API's own message out of a FastAPI error body when there is one. */
  function detailOf(payload, fallback) {
    if (payload && typeof payload.detail === "string") return payload.detail;
    if (payload && Array.isArray(payload.detail) && payload.detail.length) {
      var first = payload.detail[0];
      if (first && first.msg) return first.msg;
    }
    return fallback;
  }

  async function readBody(response) {
    try { return await response.json(); } catch (e) { return null; }
  }

  async function handle(response) {
    if (response.ok) return readBody(response);

    var payload = await readBody(response);

    if (response.status === 429) {
      // Retry-After is seconds. Fall back to 60 if the header is missing or junk.
      var raw = parseInt(response.headers.get("Retry-After") || "", 10);
      var retryAfter = Number.isFinite(raw) && raw > 0 ? raw : 60;
      throw GTError(
        "ratelimit",
        detailOf(payload, "You have reached the request limit."),
        { retryAfter: retryAfter }
      );
    }

    if (response.status === 413 || response.status === 415 || response.status === 400) {
      throw GTError("upload", detailOf(payload, "That file could not be used."), {
        status: response.status
      });
    }

    if (response.status >= 500) {
      throw GTError("server", detailOf(payload, "The server had a problem."), {
        status: response.status
      });
    }

    throw GTError("http", detailOf(payload, "Request failed (" + response.status + ")."), {
      status: response.status
    });
  }

  /* A fetch that turns a dropped connection or a CORS failure into `network`
     rather than an opaque TypeError. */
  async function request(path, options) {
    var response;
    try {
      response = await fetch(url(path), options);
    } catch (e) {
      throw GTError("network", "Could not reach the server.");
    }
    return handle(response);
  }

  window.GT_API = {
    /* Fire-and-forget warm-up. Render's free tier sleeps, and /api/health is
       exempt from rate limiting precisely so this is free to call. */
    warmUp: function () {
      fetch(url("/api/health"), { method: "GET", cache: "no-store" }).catch(function () {});
    },

    identify: function (file, coords) {
      var form = new FormData();
      form.append("photo", file, file.name || "upload.jpg");
      // Only send coordinates when we actually have a pair. The backend
      // treats a lone value as no location anyway.
      if (coords && typeof coords.lat === "number" && typeof coords.lng === "number") {
        form.append("lat", String(coords.lat));
        form.append("lng", String(coords.lng));
      }
      return request("/api/identify", { method: "POST", body: form });
    },

    createSighting: function (sighting) {
      return request("/api/sightings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sighting)
      });
    },

    listSightings: function () {
      return request("/api/sightings", { method: "GET" });
    }
  };
})();
