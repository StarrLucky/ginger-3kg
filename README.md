# ginger-3kg — food logging from a photo or a sentence

A personal nutrition tracker: FastAPI and SQLite on a Raspberry Pi, a build-free web app,
and a food recognition layer inside the backend.

Photograph a plate or type "180 g of buckwheat and a chicken breast" and you get a draft
with the macros filled in. You check it, then save it. Nothing is recorded without a human
confirming it.

The roadmap and the reasoning behind each decision live in [WEBAPP.md](WEBAPP.md) (in
Russian). This file is about running the thing and finding your way around it.

## How it works

A request goes through two steps, and that separation is the central design decision.

**Step 1 — `recognize.py`.** The model answers *what was eaten, how much, and what it
contains*. Nutrition comes back **per 100 g**, not per portion: that makes the number a
property of the food, so it can be cached under the English `lookup_query` and scaled by
arithmetic. The provider is configuration, not architecture — Anthropic, Gemini and a local
model via Ollama all sit behind one protocol.

**Step 2 — `nutrition.py`.** Assembles the final numbers in priority order:

1. `overrides.json` — a human correction, always wins;
2. the SQLite cache — keyed by brand+query for branded items, by `lookup_query` otherwise;
3. Open Food Facts — branded items only, where the label values for that exact product live;
4. `per_100g` from the model — the primary source for ordinary food;
5. `needs_manual` — anything unresolved is flagged honestly rather than filled with zeros.

Every item carries a `source_ref` (`model: buckwheat groats, cooked`,
`OFF: Ehrmann High Protein`, `override`) that is shown on the edit screen, so where a number
came from is never a mystery.

> **Why the model supplies the numbers rather than a database.** It used to be the other way
> round, as a defence against invented numbers. Measurement showed the matching error is the
> larger one: USDA resolved "coffee with milk" to *Candies, milk chocolate coated* — 1098 kcal
> instead of about 45. The write-up is in WEBAPP.md §1.3++. USDA is out of the serving path;
> `usda_lookup()` is kept as a baseline for a future evaluation.

### Layout

```
food_api/
  api.py              FastAPI: endpoints, auth, database schema
  recognize.py        step 1 — model providers behind one protocol
  recognize_prompt.md step 1 system prompt (shipped to the model verbatim)
  nutrition.py        step 2 — sources, cache, scaling
  overrides.json      local corrections for food the references get wrong
  webapp/             the app: vanilla HTML/CSS/ES modules, no build step
  tests/              pytest
scripts/
  check_secrets.sh    secret scanner (hook, make, CI)
  make_icons.py       PNG icons with no dependencies
```

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp food_api/.env.example food_api/.env    # fill in FOOD_API_KEY and a model key
make hooks                                 # pre-commit hook with the secret scanner
```

```bash
cd food_api
FOOD_API_KEY=... GEMINI_API_KEY=... RECOGNIZE_PROVIDER=gemini COOKIE_SECURE=0 \
  ../.venv/bin/python -m uvicorn api:app --port 8080
```

The app is served at `http://127.0.0.1:8080/app/`; sign in with `FOOD_API_KEY`.

`COOKIE_SECURE=0` is only for local http. Without it the browser silently refuses to store
the Secure cookie and the login appears to do nothing. Leave it alone in production.

## Configuration

The full list with explanations is in [`food_api/.env.example`](food_api/.env.example).
These are the ones that surprise people.

**`RECOGNIZE_PROVIDER`** — a comma-separated list; the order is the fallback order. An entry
may name a model after a colon:

```
RECOGNIZE_PROVIDER=gemini:gemini-3.5-flash-lite,gemini:gemini-3.6-flash
```

That is not decoration. The Gemini free tier allows **20 requests per day per model**, so
cycling through several models is the only way to get through a day without paying. Even
then it is tight for daily use: four meals plus corrections will exhaust it. Enabling
billing in Google AI Studio costs roughly $6 a year at that volume.

The `gemini-2.5-*` family has been withdrawn for new keys and answers 404, so values copied
from older documentation will not work on a fresh key.

**`USDA_API_KEY`** is not needed by the serving path — see the note above.

**`TRUST_CLIENT_IP_HEADER`** — set this only if a proxy that actually overwrites the header
sits in front of the service (Cloudflare: `CF-Connecting-IP`). Otherwise a client can supply
it themselves and walk around the login throttle. Leaving it empty is the safe default.

## Authentication

Two routes to the same key:

- **the `X-API-Key` header** — used by the Custom GPT, the Apple Shortcut and garmin-sync;
- **the `session` cookie** — obtained by the web app through `POST /auth/login`.

The cookie holds a random token rather than `FOOD_API_KEY` itself, so a leaked cookie can be
revoked through `/auth/logout` without rotating the key the integrations depend on. Sessions
last a year and are checked server-side, not merely by cookie `max-age`. Login is limited to
five attempts per address.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/recognize` | photo or text → a draft with macros, **saves nothing** |
| `POST` | `/logs` | record a meal; returns the recomputed day |
| `GET` | `/day`, `/days` | a day and a range, with progress against targets |
| `DELETE` | `/logs/{log_id}` | remove a whole entry |
| `POST` | `/targets` | calorie and macro targets |
| `POST` | `/activities`, `/activities/import` | activity, including from Garmin |
| `DELETE` | `/activities/{activity_id}` | remove an activity |
| `GET` | `/products` | Open Food Facts search, used by the Custom GPT |
| `GET` | `/export/pending`, `POST` `/export/ack` | Apple Health export queue |
| `POST` | `/auth/login`, `/auth/logout`, `GET` `/auth/status` | web app session |
| `GET` | `/health` | no authentication |

The draft from `/recognize` is accepted by `/logs` unchanged — the nutrient field lists match
one for one, and a test pins that.

## Tests

```bash
make check     # ruff + pytest
make secrets   # scan the whole history for secrets
```

214 Python tests and 21 for the front end. The front end runs under Node against a DOM stub
(`food_api/webapp/tests/run.mjs`). There is no build step, so what breaks is paths rather
than types, and that is covered separately: references in `index.html`, the cache list in
`sw.js`, ES module imports, and the edit screen's fields matching the `POST /logs` schema.

**What the tests do not cover:** behaviour in a real browser — layout, the camera,
`createImageBitmap`, service worker registration. Only a device can check those.

The habit here is to verify a new test by mutation: deliberately break the code it guards and
confirm the test fails. More than once that has revealed a green test which checked nothing.

## Deployment

The Docker build context is the `food_api/` directory:

```bash
cd food_api && docker compose up -d --build
```

The image copies `api.py nutrition.py recognize.py recognize_prompt.md overrides.json` and
the `webapp/` directory. The database is `food.db`, shared with the Custom GPT's backend,
which is why `journal_mode=WAL` and `busy_timeout` are set: two writing processes without
them produce "database is locked".

External access is not set up yet. The recommendation and a ready configuration for
Cloudflare Tunnel are in WEBAPP.md §2.6. ngrok's free tier shows an interstitial ahead of
HTML traffic, which ruins the feel of an app launched from the home screen.

## Secrets

Three layers: a pre-commit hook (`make hooks`), a CI job over the full history, and GitHub
push protection. The gitleaks configuration is in `.gitleaks.toml`, including the allowlist
for lines such as `usda_api_key=USDA_API_KEY` where the value is a variable name, not a key.

The hook fails closed: no gitleaks means no commit, rather than a commit that passed silently.
