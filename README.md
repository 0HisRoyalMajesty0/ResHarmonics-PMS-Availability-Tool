# PMS Availability Automation Tool for Partners

Pulls live availability from Res:Harmonics (Gravity Coliving's PMS) and renders it
as a clean, shareable HTML digest for partners — with an explicit "Awaiting
Response" state so partners with missing/stale data are flagged instead of
silently dropped.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and fill in RESHARMONICS_CLIENT_ID / RESHARMONICS_CLIENT_SECRET
# (values are in the "Gravity Coliving v3 API" doc)
export $(cat .env | xargs)   # or use python-dotenv / direnv if you prefer
```

## Run the tests first (no credentials needed)

```bash
python3 test_pms_availability.py
```

These confirm the classification logic and HTML rendering work correctly. They
run entirely against mocked API responses — no network calls.

## Run for real

1. Open `pms_availability.py` and add real partners to the `PARTNERS` list
   (partner name + Res:Harmonics property ID).
2. `python3 pms_availability.py`
3. Open the generated `availability_report.html`.

## Apartment-level report (`apartment_availability_report.py`)

Produces a per-apartment digest — apartment number, apartment name/building,
current monthly price, available-from date — as branded HTML, pulling
directly from the confirmed live endpoints (see `CLAUDE.md`). This has been
run successfully against the live API.

```bash
python3 apartment_availability_report.py
# writes apartment_availability_report.html
```

Takes about a minute — it walks every bookable unit's vacancy calendar and
every unit type's rate calendar.

## Departing tenants + cleaning preference (`departing_tenants_cleaning.py`)

Answers "who is leaving in the next N days, and which of them want Gravity to
handle the end-of-tenancy clean?" — joining Res:Harmonics (who's leaving, with
names and emails) to Pipedrive (what they've told us about cleaning).

```bash
python3 departing_tenants_cleaning.py discover          # confirm the PMS endpoint
python3 departing_tenants_cleaning.py report --days 30  # writes HTML + JSON
```

Run `discover` first on a fresh credential set. The tenancy endpoint is the one
part of the PMS API this repo has **not** confirmed live, so the script resolves
it from the OpenAPI spec at runtime and prints its choice rather than shipping a
hard-coded guess. Pin it with `--endpoint` (or `RESHARMONICS_DEPARTURES_ENDPOINT`)
once you know the real path, and record it in `CLAUDE.md`.

`PIPEDRIVE_API_TOKEN` is optional — without it you still get the departure list,
written to `departures.json`, and the CRM half can be done via the Pipedrive MCP.

Cleaning preference is classified into four buckets: **Gravity arranges**,
**Tenant does it**, **Asked, no answer yet**, and **Not recorded**. The third
bucket matters — plenty of notes mention cleaning while recording no tenant
decision at all ("to check if he wants gravity to arrange his end of tenancy
cleaning"), and counting those as answers would under-book cleaners. Every
classification carries the sentence it came from so it can be checked against
the CRM.

```bash
python3 -m unittest test_departing_tenants_cleaning -v   # no credentials needed
```

## Status — what's confirmed vs. what's still open

**Confirmed (including a live run against the real API on 2026-07-08):**
- PMS: Res:Harmonics, Rerum API v3
- Base URL: `https://apiv3.rerumapp.uk`
- Auth: OAuth2 client-credentials grant against `https://auth.rerumapp.uk/oauth2/token`
  — confirmed working live, not just in theory.
- Real endpoints and field names for units, per-unit vacancy, and per-unit-type
  rates — see the module docstring in `apartment_availability_report.py` and
  the "Confirmed against the live API" section in `CLAUDE.md`.

**Still open:**
- `pms_availability.py`'s partner/property-level model (`AVAILABILITY_ENDPOINT`,
  `parse_availability_response()`) has NOT been updated to the real API — its
  granularity (one partner/property → one availability status) doesn't map
  directly onto the real API's unit/rate/interval model. Needs a product
  decision on what a "partner" maps to before rewriting it; see `CLAUDE.md`.

See `CLAUDE.md` for full background/context if you're picking this up with
Claude Code.

## Security

- `.env` is gitignored — never commit real credentials.
- Treat the client secret like a password. Don't paste it into Slack, Notion,
  or anywhere else it doesn't need to be.

## Origin

Built from the "PMS Availability Automation Tool for Partners" idea in the
Idea Bank Notion database:
https://app.notion.com/p/392345b06fe68190821cda37a6c58dd7
