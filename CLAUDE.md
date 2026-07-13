# Project context — PMS Availability Automation Tool for Partners

Read this before doing anything else. It's a handoff from a Cowork session
where the initial version of this tool was built without live network access
to the target API, so several things are verified-in-theory but not
verified-live.

## "Availability" — the definition to use (set by the user, 2026-07-13)

When the user asks for **"availability"** (or "the availability", "latest
availability", etc.), it means specifically this — produce it, don't ask:

- A unit counts as **available** only if it is vacant for the next **2 months**
  (2 consecutive calendar months from when it becomes vacant).
- **Two lists, in this order:**
  1. **Vacant now** — units vacant today (and, per the rule above, staying
     vacant for the next 2 months).
  2. **Becoming vacant in the next 3 months** — units that open up after today
     but on/before the horizon, then stay vacant 2 months.
- **Horizon: 92 days** (= "3 months"). Do NOT report anything becoming vacant
  beyond 92 days from whenever the automation is run.

This is exactly what `apartment_availability_report.py` implements
(`classify_availability()`, `VACANT_MONTHS_REQUIRED=2`, `HORIZON_DAYS=92`).
Default delivery is the branded output to Slack channel `C0B9NFGR0H1`
(canvas `F0BGT1VMQV6`); an `.xlsx` export is also available on request.

## Goal

Automate pulling live availability from Res:Harmonics (Gravity Coliving's
PMS) via API, classify it, and output a clean HTML digest for partners.
Originally captured as an idea in a Notion "Idea Bank" database:
https://app.notion.com/p/392345b06fe68190821cda37a6c58dd7 (Status: In Progress)

## Current state

- `pms_availability.py` — the original partner/property-level digest: OAuth2
  auth, availability fetch, four-state classification, HTML rendering. The
  file Cowork produced was actually truncated mid-function (missing HTML
  closing markup and the entire `main()`/CLI entry point) — fixed on
  2026-07-08. Its `AVAILABILITY_ENDPOINT`/`parse_availability_response()`
  guesses (`/availability`, `openNightsCount`, etc.) were never real — see
  "Confirmed against the live API" below for what the actual endpoints and
  fields are. This file has NOT been rewritten to use them, since its
  per-partner/property model doesn't map cleanly onto the real API (which is
  unit + rate + interval based, not a single "is this property available"
  call) — if this granularity is still wanted, it needs a product decision on
  what a "partner/property" maps to (a building? a unit type?) before
  rewriting `fetch_availability()`/`parse_availability_response()`.
- `apartment_availability_report.py` — new script, built 2026-07-08, tested
  live end-to-end against the real API. Produces a per-apartment digest
  (apartment number, apartment name/building, current monthly price,
  available-from date) as branded HTML. Read its module docstring for the
  confirmed endpoints/fields it relies on.
- `test_pms_availability.py` — passes, but only tests `pms_availability.py`'s
  classification logic against mocked API responses.
- `.env.example` — copy to `.env`, fill in credentials from the "Gravity
  Coliving v3 API" Word doc.

## Confirmed against the live API (2026-07-08)

The blocker described in the original handoff (sandbox network access to
`auth.rerumapp.uk` / `apiv3.rerumapp.uk` blocked) is NOT present in every
environment — it depended on that session's proxy allowlist. With real
`RESHARMONICS_CLIENT_ID`/`RESHARMONICS_CLIENT_SECRET` supplied by the user,
both the OAuth2 exchange and the API calls below were run live and confirmed
working:

- Auth: OAuth2 client-credentials against `https://auth.rerumapp.uk/oauth2/token`
  works exactly as `get_access_token()` implements it.
- The real OpenAPI spec is discoverable at `GET /v3/api-docs` on
  `https://apiv3.rerumapp.uk` (Bearer-authenticated) — this is how the
  endpoints below were found, since apidocs.resharmonics.com is JS-rendered
  and wasn't reachable/scrapable even when the network wasn't blocked.
- `GET /api/v3/units` (paginate with `size=500`) — full unit list:
  `id`, `unitName` (e.g. `"18 West Court"`), `buildingName`, `bookable`.
- `GET /api/v3/units/{id}` — per-unit detail incl. `unitType.id`/`unitType.name`.
- `GET /api/v3/availabilities/unit/{unitId}/intervals?dateFrom=&dateTo=` —
  ground-truth vacancy per unit: `{startDate, endDate, available}`. This is
  the right source for "available from."
- `GET /api/v3/rates?unitTypeId=&dateFrom=&dateTo=` — rate calendar per unit
  type (not per unit): `rateType` (`MONTHLY`/`DAILY`/`WEEKLY`), `rates: [{date, amount}]`.
- **Do NOT use `GET /api/v3/availabilities` (the "search" endpoint) as a
  vacancy check** — it applies real booking-engine rules (lead time, minimum
  stay, closed-to-arrival) on top of raw vacancy, so querying it with a
  unit's actual available-from date frequently returns zero results even
  though the unit is genuinely vacant per `/intervals`. Confirmed this the
  hard way — cost a lot of back-and-forth before switching to `/intervals`.
- Portfolio at time of testing: 306 total units, 212 bookable, spanning 10
  buildings (Gravity Hounslow Central, Gravity Camden Lock, Gravity Camden
  Town, Gravity Finsbury Park, Gravity Reading Town Centre, Gravity Notting
  Hill, Gravity West Hampstead, The Weymouth, Gravity Bayswater Hyde Park,
  plus a `Gravity Test` sandbox building with 1 demo unit — filter that out).
  All pricing is GBP.

## Design decisions worth knowing about (don't relitigate without reason)

- **Four-state classification** (Available / Partially Available /
  Unavailable / Awaiting Response), not a binary yes/no. This was an explicit
  requirement: partners who haven't given data shouldn't be silently dropped
  from the report — they get flagged as "Awaiting Response" instead.
- **Staleness check**: even if the PMS says "available," if the sync
  timestamp is older than `STALE_AFTER_HOURS` (default 24h), the partner gets
  downgraded to "Awaiting Response" rather than trusted blindly.
- **Sort order**: Awaiting Response and Partially Available rows float to the
  top of the report, since those are the ones that need a human to look at
  them.
- **HTML output**, not CSV/JSON — chosen because it's easy to scan, styleable,
  and pasteable/shareable, per the original idea capture.
- Credentials always via env vars, never hard-coded. `.env` is gitignored.

## Longer-term plan (not yet started)

Once the live endpoint is confirmed and the script works end-to-end against
real partner data, this is meant to become a recurring/scheduled pull (daily
or on-demand) rather than a manually-run script.
