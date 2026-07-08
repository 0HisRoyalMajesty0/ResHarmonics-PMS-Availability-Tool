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

## Status — what's confirmed vs. what's still open

**Confirmed:**
- PMS: Res:Harmonics, Rerum API v3
- Base URL: `https://apiv3.rerumapp.uk`
- Auth: OAuth2 client-credentials grant against `https://auth.rerumapp.uk/oauth2/token`
  (implemented in `get_access_token()` — standard Cognito-style client-credentials
  exchange, should work as-is)

**Not yet confirmed — do this next:**
- The exact availability endpoint path (`AVAILABILITY_ENDPOINT` in
  `pms_availability.py` is currently a placeholder: `/availability`)
- The real response field names (`parse_availability_response()` currently
  guesses `openNightsCount`, `openDateRanges`, `lastSyncedAt`)
- None of this has been tested against the live API yet — the environment this
  was built in had outbound network access blocked to `auth.rerumapp.uk` /
  `apiv3.rerumapp.uk`. First live run will likely 404 on the availability call;
  the error response should tell you enough to fix the path.
- Suggested way to confirm: use the "Configuring OAuth2 on Postman" guide at
  https://apidocs.resharmonics.com/guides/oauth-postman to get a token and
  explore the API reference at https://apidocs.resharmonics.com/ interactively
  (it's a JS-rendered site, so it needs a real browser).

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
