# Groundtruth

NextStep Hacks 2026 · Earth Forward

Identify a species from a photo, then answer the question plant-ID apps skip:
**is this actually a problem where you are standing, and what should you do about it?**

FastAPI backend on Render, static frontend on Vercel, Postgres on Supabase.
The interface is built to the Groundtruth design spec: warm ground, one clay
action colour, and four status colours that belong to the species rather than
to the app.

---

## How identification works

```
photo ──> Pl@ntNet ──(match)──────────────────────────┐
             │                                        │
             └─(404 "not a plant" / low score)        │
                        │                             │
                        └──> Gemini vision ───────────┤
                                                      │
                                                      v
                        iNaturalist: native here?  ───┤
                        Curated invasive list      ───┤
                                                      v
                                             combined status
                                                      │
                                       Gemini explainer (grounded
                                       on curated facts when we
                                       have them)
```

Pl@ntNet returns **HTTP 404** when it decides an image is not a plant. That
rejection is used deliberately as a free plant/not-plant router — which is why
`no-reject` is never sent. Insects, animals and fish fall through to Gemini.

**Status is resolved as:**

| Condition | Status |
|---|---|
| On the curated list, and iNaturalist does not call it native here | `invasive` |
| Non-native per iNaturalist, not on the curated list | `introduced` |
| Native or endemic per iNaturalist | `native` |
| Nothing conclusive | `unknown` |

The native guard matters: a curated species photographed inside its own native
range is reported as native, not invasive.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | Liveness + which env vars are missing. **Not rate limited** — use it to warm up a sleeping Render instance. |
| `POST` | `/api/identify` | Multipart: `photo` (required), `lat`, `lng` (optional). |
| `POST` | `/api/sightings` | JSON body; returns the saved row with a server-generated `created_at`. |
| `GET` | `/api/sightings` | Most recent first, capped at 500. |

Interactive docs at `/docs`.

### `POST /api/identify` response

```json
{
  "common_name": "Kudzu",
  "scientific_name": "Pueraria montana",
  "id_source": "plantnet",
  "confidence": 0.907,
  "status": "invasive",
  "explainer": {
    "summary": "...",
    "what_to_do": ["...", "..."]
  },
  "establishment_means": "introduced",
  "place_name": "Georgia",
  "category": "plant",
  "curated_match": true
}
```

`id_source` is `plantnet`, `gemini`, or `none`. A photo with no identifiable
organism returns **HTTP 200** with `status: "unknown"` and `id_source: "none"`,
not an error — so the UI can render a helpful card instead of catching an
exception.

---

## Rate limits

Applied to all three API routes; `/api/health` is exempt.

| Limit | Default | Env var |
|---|---|---|
| Per IP, per minute | 5 | `RATE_LIMIT_PER_MIN` |
| Per IP, per day | 20 | `RATE_LIMIT_PER_DAY` |
| Global identifies per day | 300 | `GLOBAL_IDENTIFY_PER_DAY` |

Rejections return **429** with a `Retry-After` header.

Two things to know before demo day:

- **The 20/day cap applies to `GET /api/sightings` too.** Anyone sharing an IP
  (conference wifi, several judges on one network) stops being able to load
  sightings after 20 requests. Raise `RATE_LIMIT_PER_DAY` in the Render
  dashboard if that bites — it takes effect on restart, no code push.
- **It will throttle you while testing.** Six uploads in a minute returns 429.

Counters are in-process and reset when Render spins the service down.

---


## Frontend

Plain HTML/CSS/JS. Tailwind via CDN, Leaflet for the map, no build step.

```
frontend/
  index.html      identify: upload, the wait, result card, failure states
  map.html        community map
  css/styles.css  icon masks, Leaflet theming, motion
  js/config.js    API_BASE - the one value you edit after deploying
  js/api.js       fetch wrappers; normalises errors into kinds
  js/identify.js  identify page logic and result rendering
  js/map.js       Leaflet, pins, legend-as-filter, empty vs failed
  vercel.json     static config
```

**Design rules the code enforces:**

- **Status is the hero.** The verdict band is the headline and always names the
  place: "Invasive in Georgia", "Native to Georgia".
- **Colour is never alone.** Every status carries its own glyph, its own word,
  and its own pin silhouette, so it survives colourblindness and sunlight.
- **Clay is the app talking; status colours are the species talking.** Errors
  and system messages never borrow a status colour, or red stops meaning
  invasive. Upload rejections are deliberately grey.
- **Native is not a green warning.** No report button, a sage panel instead of
  a numbered task list, and a closing line. It reads as an answer.
- **Log this sighting** is solid for invasive, outlined for introduced, and
  absent for native and unknown - an unverified point would pollute the map.
- **`curated_match: true` earns the "Vetted, not generated" chip**, so people
  can tell vetted guidance from a model improvising about herbicides.

**The wait** names the three backend steps in the order they actually run, then
fills them with real returned values. The cold-start notice appears at ten
seconds, not at zero - before then the wait is normal and saying otherwise
would invent a problem. Both pages call `/api/health` on load to warm the
server, so most people never see it.

**Three failures that look nothing like each other:** a 429 is an 82px
countdown driven by `Retry-After`; an unreachable server has no countdown and
retries itself; an unidentifiable photo is a normal 200 outcome and says so.
The empty map is fully drawn and working, while the failed map shows no map at
all - at a glance they are unmistakably different.

---

## Deploying

### 1. Supabase

Run in the SQL editor:

```sql
create table public.sightings (
  id              bigint generated always as identity primary key,
  common_name     text not null,
  scientific_name text not null,
  status          text not null check (status in ('invasive','introduced','native','unknown')),
  latitude        double precision not null,
  longitude       double precision not null,
  created_at      timestamptz not null default now()
);

alter table public.sightings enable row level security;
```

RLS on with **no policies** locks out the anon key entirely. The backend uses
the `service_role` key, which bypasses RLS by design.

Copy the **Project URL** and the **`service_role`** key from Settings → API.
`service_role` is a full-access admin credential: Render env vars only, never
in frontend code, never committed.

### 2. Render

New **Web Service** → connect this repo.

| Setting | Value |
|---|---|
| Root Directory | `backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |

**Required env vars:** `GEMINI_API_KEY`, `PLANTNET_API_KEY`, `SUPABASE_URL`,
`SUPABASE_KEY`, `ALLOWED_ORIGIN`

**Optional:** `GEMINI_MODEL`, `PLANTNET_PROJECT`, `PLANTNET_MIN_SCORE`,
`HTTP_TIMEOUT_SECONDS`, `MAX_UPLOAD_BYTES`, and the three rate-limit vars.

See [.env.example](.env.example) for the full list with descriptions.

`ALLOWED_ORIGIN` is comma-separated, so production and a preview URL can both
be allowed. No trailing slashes. Until it is set, browsers cannot call this API
— that is the safe default, and the startup log says so.


### 3. Vercel

New Project -> same repo -> **Root Directory `frontend`**, Framework Preset
**Other**, no build command.

Then edit `frontend/js/config.js` and set `API_BASE` to your Render URL, with
no trailing slash, and push. This is the one manual wire-up between the two
services.

### 4. Verify

```bash
curl https://YOUR-SERVICE.onrender.com/api/health
```

`missing_config` should be empty. Then upload a leaf photo (expect
`id_source: "plantnet"`) and an insect photo (expect `id_source: "gemini"`).
That pair exercises the entire fallback chain.

---

## Design notes

- **Supabase over HTTPS, not a Postgres connection string.** Render's free tier
  has no IPv6 outbound and Supabase's free-tier direct connection is IPv6-only,
  so a connection string fails there in a way that is annoying to diagnose.
- **Gemini model is pinned to `gemini-3.5-flash-lite`.** Flash-Lite carries
  roughly double the free-tier RPM of standard Flash, and no
  `gemini-flash-lite-latest` alias is published. Override with `GEMINI_MODEL`
  if your key lacks access — check your limits at
  [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit).
- **The explainer is grounded on curated facts.** For species on the list,
  the vetted `why_it_matters` and `what_to_do` go into the prompt, so the model
  does not improvise advice about herbicides or plants that burn skin.
- **Every upstream failure degrades rather than crashing.** Pl@ntNet, Gemini
  and iNaturalist can each be down, slow or rate-limited and `/api/identify`
  still returns a valid, renderable result.
- **`supabase-py` is synchronous**, so its calls run in a worker thread via
  `asyncio.to_thread` rather than blocking the event loop.

## Curated list

[`backend/data/invasive_species.json`](backend/data/invasive_species.json) —
38 species: 15 plants, 10 insects, 6 animals, 7 aquatic. Each has
`why_it_matters` and concrete `what_to_do` actions, plus `aliases` covering
taxonomic synonyms so matching survives whichever name a service returns.

Matching is forgiving in one direction only. Subspecies entries such as
*Apis mellifera scutellata* (Africanized honey bee) never match on their
binomial alone — telling someone their ordinary garden honey bee is an
aggressive invasive would be a real failure.
