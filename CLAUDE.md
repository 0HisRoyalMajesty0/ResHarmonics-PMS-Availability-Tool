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

## Confirmed against the live Pipedrive account (2026-08-27)

Explored via the Pipedrive MCP tools while answering "who's leaving in 30 days
and who wants Gravity to do their end-of-tenancy clean?". Used by
`departing_tenants_cleaning.py`.

- **Move-out conversations live in two pipelines**: id **5 = Extensions**
  (deals titled `"<Name> - <UNIT> (Ext.N)"`; `won` = tenant extended and is
  staying, `lost` = not extending) and id **7 = Early Termination** (titled
  `"<Name> - <UNIT> - ET"`; stages Formal Notice / Objection Handling /
  Termination Admin / Upfront Payment Refund).
- **Deal custom-field keys** (confirmed by cross-checking known deals):
  - `39a24443c32c1043f92a3a5641e016ba58bed353` — contract end / move-out date
  - `5ca4575f1ce5eac39e8c8a7f57db84f00fd8bc17` — unit, e.g. `"81 West Court"`
  - `f061944b5e04511943e57749e83376ccb6ebbb92` — building, e.g. `"West Court - Hounslow"`
  The unit label shares its format with the PMS's `unitName`, so it's a
  reliable join key between the two systems (normalise leading zeros: the CRM
  writes `"004 Royal Heights"`).
- **There is no structured field for cleaning preference.** It's free text in
  deal notes, written inconsistently by whoever handled the conversation
  ("EOT cleaning NOT requested. Tenant will take care of it.", "He opted for
  end of tenancy cleaning.", "she is happy to pay for the end-of-tenancy
  cleaning"). If this question keeps coming up, adding a single-option custom
  field to the Extensions/ET pipelines would remove the need to parse notes at
  all — worth raising with whoever owns the CRM.
- **Careful with note text that mentions cleaning but records no decision** —
  internal to-dos and unanswered questions ("to check if he wants gravity to
  arrange his end of tenancy cleaning") read as positive to a naive keyword
  match. The classifier buckets these separately as "asked, no answer yet";
  don't collapse that bucket into a yes/no, it under-books cleaners.
- **Pipedrive is NOT a valid source for the departure list.** It only contains
  tenants someone opened a deal for — anyone reaching their natural contract
  end without an extension conversation is invisible. Always take departures
  from the PMS and use Pipedrive only for the preference lookup.

## Departures / tenancies — confirmed live (2026-08-27)

Resolved via `discover` against the real spec, then run end-to-end.

- `GET /api/v3/roomStays?checkOutDateFrom=&checkOutDateTo=&size=500` — the
  departures query. A *room stay* is one booking of one unit by one contact:
  `{roomStayId, roomStayStatus, startDate, endDate, bookingContact:{firstName,
  lastName, emailAddress, id}, unit:{id, name, buildingName}}`. `unit.name`
  ("98 West Court") matches the Pipedrive unit label format exactly.
  Statuses seen live: `CHECKED_IN`, `CONFIRMED`, `CANCELLED`.
- `GET /api/v3/roomStays?bookingContactId=` — one contact's whole history.
- `GET /api/v3/guestStays?…&emailAddress=&firstName=&lastName=` also exists if
  per-guest (rather than per-booking-contact) detail is ever needed.
- Do NOT use `GET /api/v3/bookings` for this: its `dateFrom`/`dateTo` are stay
  overlap, not checkout, so it can't answer "who leaves in this window".

**A renewal is a NEW room stay, not an extended end date.** So "room stays
ending in the next 30 days" is NOT the departure list — on 2026-08-27 it
returned 54 stays of which 15 were renewals (tenants staying put). A contact is
only leaving if the LAST of their stays ends inside the window; back-to-back
stays must be stitched into one tenancy to get a true tenancy length. This is
implemented in `build_departures()` / `contiguous_tenancy_start()` and is the
single most important rule in that script — getting it wrong sends cleaners to
occupied flats.

**Never match a PMS tenant to a Pipedrive deal by unit.** Units are re-let, so
a unit-only match reliably finds the *previous* occupant's deal — live data had
"46 West Court" resolving to a moved-out tenant whose cleaning preference would
then have been attributed to the current one. Match on person (name/email); use
the unit only to disambiguate one person's several deals.

Live shape of the answer on 2026-08-27 (30-day window): 39 genuine departures =
26 residential tenants + 13 short-stay guests. Only 13 of the 26 had any deal in
pipelines 5/7 at all, and exactly ONE had a recorded cleaning preference.

## Network note

`api.pipedrive.com` is blocked by the Claude Code sandbox egress policy (403 on
CONNECT), so `departing_tenants_cleaning.py`'s direct REST path can't run there
— it degrades to writing the departure list and tells you to use the Pipedrive
MCP tools for the CRM half. The PMS hosts (`auth.rerumapp.uk`,
`apiv3.rerumapp.uk`) are reachable. Outside the sandbox, with
`PIPEDRIVE_API_TOKEN` set, the script runs end to end unaided.

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
