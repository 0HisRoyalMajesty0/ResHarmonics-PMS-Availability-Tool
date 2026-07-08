# Project context — PMS Availability Automation Tool for Partners

Read this before doing anything else. It's a handoff from a Cowork session
where the initial version of this tool was built without live network access
to the target API, so several things are verified-in-theory but not
verified-live.

## Goal

Automate pulling live availability from Res:Harmonics (Gravity Coliving's
PMS) via API, classify it, and output a clean HTML digest for partners.
Originally captured as an idea in a Notion "Idea Bank" database:
https://app.notion.com/p/392345b06fe68190821cda37a6c58dd7 (Status: In Progress)

## Current state

- `pms_availability.py` — the full script: OAuth2 auth, availability fetch,
  classification logic, HTML rendering. Read the module docstring at the top
  first — it documents exactly what's confirmed vs. still a placeholder.
- `test_pms_availability.py` — passes, but only tests logic against mocked
  API responses. Does not touch the real API.
- `.env.example` — copy to `.env`, fill in credentials from the "Gravity
  Coliving v3 API" Word doc the user has (Client ID + Client Secret).

## What's confirmed

- PMS is Res:Harmonics, specifically the Rerum API v3.
- Base URL: `https://apiv3.rerumapp.uk`
- Auth URL: `https://auth.rerumapp.uk/oauth2/token`
- Auth type: OAuth2 client-credentials grant (Cognito-style — HTTP Basic auth
  with client_id/client_secret, `grant_type=client_credentials`). Implemented
  in `get_access_token()`. This follows a standard, well-documented pattern,
  so it's likely correct, but has never actually been run against the live
  auth server (see below).

## What's NOT confirmed — this is the immediate next task

1. **The availability endpoint path.** `AVAILABILITY_ENDPOINT = "/availability"`
   in `pms_availability.py` is a guess, not a confirmed path. The Res:Harmonics
   API reference (https://apidocs.resharmonics.com/) is a JS-rendered docs
   site — plain HTTP fetches return an empty shell, so it needs a real browser
   session to read.
2. **Response field names.** `parse_availability_response()` guesses
   `openNightsCount`, `openDateRanges`, `lastSyncedAt` as field names in the
   JSON response. These are placeholders, not confirmed.
3. **Nothing has been run against the live API.** The environment this was
   built in had its outbound network access restricted (an allowlisted proxy
   blocked `auth.rerumapp.uk` and `apiv3.rerumapp.uk` entirely, HTTP 403
   `blocked-by-allowlist`). So even the OAuth2 token exchange itself is
   untested live — only the classification/rendering logic has been verified,
   via mocks.

**Recommended next step:** use the "Configuring OAuth2 on Postman" guide
(https://apidocs.resharmonics.com/guides/oauth-postman) to get a token in
Postman, then browse https://apidocs.resharmonics.com/ to find the real
availability endpoint and its response shape. Update
`AVAILABILITY_ENDPOINT` and `parse_availability_response()` accordingly, then
re-run `test_pms_availability.py` (add a new test using a real captured
response as a fixture) before pointing it at production partner data.

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
