#!/usr/bin/env python3
"""
PMS Availability Automation — Res:Harmonics -> HTML digest for partners.

WHAT THIS DOES
1. Pulls live availability from Res:Harmonics for each configured property/partner.
2. Classifies each partner into one of four states (see CLASSIFICATION RULES below).
3. Renders a clean, shareable HTML report.

AUTH — CONFIRMED
This is Rerum API v3, using OAuth2 client-credentials (Cognito-style):
  - Base URL:  https://apiv3.rerumapp.uk
  - Token URL: https://auth.rerumapp.uk/oauth2/token
The script exchanges CLIENT_ID + CLIENT_SECRET for a short-lived Bearer access
token, then calls the API with it (get_access_token() below, cached in-memory).

STILL TO CONFIRM
I couldn't reach a live login or render the JS-based API reference at
apidocs.resharmonics.com from this environment, so the exact availability
endpoint *path* and its response *field names* are still placeholders (clearly
marked below). Sandbox network access is also allowlisted and blocked
auth.rerumapp.uk / apiv3.rerumapp.uk outright, so none of this has been tested
against the live API yet — only against mocked responses. Run this from your
own machine (or Postman first, using the "Configuring OAuth2 on Postman" guide
on the docs site) to confirm the endpoint path and response shape, then update
AVAILABILITY_ENDPOINT and parse_availability_response() accordingly.

AUTH — how to set credentials
Set these as environment variables — never hard-code them in this file:
    export RESHARMONICS_CLIENT_ID="..."
    export RESHARMONICS_CLIENT_SECRET="..."
(Both are in the "Gravity Coliving v3 API" doc. Treat the client secret like a
password — don't paste it into Slack, Notion, or anywhere else it doesn't need
to be.)
"""

import os
import sys
import time
import base64
import datetime as dt
from dataclasses import dataclass
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# CONFIG — adjust AVAILABILITY_ENDPOINT once you've confirmed it against the
# live docs/Postman (see STILL TO CONFIRM above)
# ---------------------------------------------------------------------------

API_BASE_URL = os.environ.get("RESHARMONICS_API_BASE", "https://apiv3.rerumapp.uk")
AUTH_URL = os.environ.get("RESHARMONICS_AUTH_URL", "https://auth.rerumapp.uk/oauth2/token")
CLIENT_ID = os.environ.get("RESHARMONICS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("RESHARMONICS_CLIENT_SECRET")
AVAILABILITY_ENDPOINT = "/availability"  # CONFIRM against live docs/Postman

# How stale can a PMS record be before we flag it as unreliable, even if it
# says "available"? Tune this to how often the PMS actually syncs.
STALE_AFTER_HOURS = 24

# The partners/properties you want in the digest. In production this could be
# pulled from Res:Harmonics' /property or /companies endpoint instead of a
# static list.
PARTNERS = [
    # {"partner_name": "Example Serviced Apartments", "property_id": "12345"},
]

OUTPUT_HTML_PATH = "availability_report.html"


# ---------------------------------------------------------------------------
# CLASSIFICATION RULES
# ---------------------------------------------------------------------------
# AVAILABLE          - PMS returned open inventory for the full requested range,
#                       with a fresh sync timestamp.
# PARTIAL            - PMS returned open inventory for *some* but not all of the
#                       requested range (e.g. 5 of 7 nights free).
# UNAVAILABLE        - PMS explicitly returned zero open inventory for the range.
# AWAITING RESPONSE   - No usable data: partner/property has no PMS record yet,
#                       the API call failed for that property, or the record is
#                       older than STALE_AFTER_HOURS. This partner should NEVER
#                       be silently dropped from the report — they get flagged
#                       instead, so nobody falls through the cracks.
# ---------------------------------------------------------------------------

STATUS_AVAILABLE = "Available"
STATUS_PARTIAL = "Partially Available"
STATUS_UNAVAILABLE = "Unavailable"
STATUS_AWAITING = "Awaiting Response"

STATUS_STYLES = {
    STATUS_AVAILABLE: {"bg": "#e6f4ea", "fg": "#1e7e34", "label": "Available"},
    STATUS_PARTIAL: {"bg": "#fff8e1", "fg": "#8a6d00", "label": "Partially Available"},
    STATUS_UNAVAILABLE: {"bg": "#fdecea", "fg": "#b3261e", "label": "Unavailable"},
    STATUS_AWAITING: {"bg": "#eceff1", "fg": "#546e7a", "label": "Awaiting Response"},
}


@dataclass
class PartnerAvailability:
    partner_name: str
    property_id: str
    status: str
    available_dates: str = ""
    last_updated: Optional[dt.datetime] = None
    note: str = ""


# In-memory token cache: (access_token, expires_at_epoch_seconds)
_token_cache: dict = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    """
    Exchanges CLIENT_ID/CLIENT_SECRET for a Bearer access token via the
    OAuth2 client_credentials grant, caching it until shortly before expiry.

    This part is confirmed (Base URL + Auth URL come straight from the
    Gravity Coliving v3 API doc) — the OAuth2 dance itself is standard
    Cognito-style client-credentials, so this should work as-is. What's NOT
    yet confirmed is the availability endpoint this token is used against
    (see fetch_availability below).
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "RESHARMONICS_CLIENT_ID / RESHARMONICS_CLIENT_SECRET are not set in the environment."
        )

    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]

    resp = requests.post(
        AUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),  # HTTP Basic auth, standard for Cognito client-credentials
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()

    access_token = payload["access_token"]
    expires_in = payload.get("expires_in", 3600)
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + expires_in
    return access_token


def fetch_availability(property_id: str, start_date: str, end_date: str) -> Optional[dict]:
    """
    Calls the Res:Harmonics availability endpoint for one property.
    Returns the raw JSON response, or None if the call failed (network error,
    4xx/5xx, timeout) — a failure here should route the partner to
    AWAITING RESPONSE, not crash the whole run.

    CONFIRM: AVAILABILITY_ENDPOINT's exact path, query params, and response
    shape against the live docs/Postman before relying on this — the auth
    (get_access_token) is confirmed, the endpoint itself is not.
    """
    try:
        token = get_access_token()
    except (RuntimeError, requests.RequestException) as exc:
        print(f"[warn] could not get access token: {exc}", file=sys.stderr)
        return None

    url = f"{API_BASE_URL}{AVAILABILITY_ENDPOINT}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {
        "propertyId": property_id,
        "startDate": start_date,
        "endDate": end_date,
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        print(f"[warn] availability fetch failed for property {property_id}: {exc}", file=sys.stderr)
        return None


def parse_availability_response(raw: dict, requested_nights: int) -> tuple[str, str, Optional[dt.datetime]]:
    """
    Turns a raw PMS response into (status, available_dates_str, last_updated).
    PLACEHOLDER LOGIC — rewrite the field lookups once you know the real
    response shape (e.g. raw["units"][0]["availableRanges"]).
    """
    open_nights = raw.get("openNightsCount", 0)  # CONFIRM field name
    date_ranges = raw.get("openDateRanges", [])   # CONFIRM field name
    synced_at_raw = raw.get("lastSyncedAt")        # CONFIRM field name

    last_updated = None
    if synced_at_raw:
        try:
            last_updated = dt.datetime.fromisoformat(synced_at_raw.replace("Z", "+00:00"))
        except ValueError:
            last_updated = None

    if open_nights <= 0:
        status = STATUS_UNAVAILABLE
    elif open_nights < requested_nights:
        status = STATUS_PARTIAL
    else:
        status = STATUS_AVAILABLE

    dates_str = ", ".join(date_ranges) if date_ranges else ""
    return status, dates_str, last_updated


def is_stale(last_updated: Optional[dt.datetime]) -> bool:
    if last_updated is None:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=dt.timezone.utc)
    return (now - last_updated) > dt.timedelta(hours=STALE_AFTER_HOURS)


def build_report(partners: list[dict], start_date: str, end_date: str) -> list[PartnerAvailability]:
    requested_nights = (
        dt.date.fromisoformat(end_date) - dt.date.fromisoformat(start_date)
    ).days

    results: list[PartnerAvailability] = []
    for p in partners:
        raw = fetch_availability(p["property_id"], start_date, end_date)

        if raw is None:
            results.append(
                PartnerAvailability(
                    partner_name=p["partner_name"],
                    property_id=p["property_id"],
                    status=STATUS_AWAITING,
                    note="No response from PMS for this property/range.",
                )
            )
            continue

        status, dates_str, last_updated = parse_availability_response(raw, requested_nights)

        if is_stale(last_updated):
            status = STATUS_AWAITING
            note = "Data missing or older than the freshness window — needs a manual check."
        else:
            note = ""

        results.append(
            PartnerAvailability(
                partner_name=p["partner_name"],
                property_id=p["property_id"],
                status=status,
                available_dates=dates_str,
                last_updated=last_updated,
                note=note,
            )
        )

    # Float the states that need attention to the top so they're never missed.
    priority = {STATUS_AWAITING: 0, STATUS_PARTIAL: 1, STATUS_UNAVAILABLE: 2, STATUS_AVAILABLE: 3}
    results.sort(key=lambda r: priority.get(r.status, 99))
    return results


def render_html(results: list[PartnerAvailability], start_date: str, end_date: str) -> str:
    generated_at = dt.datetime.now().strftime("%d %b %Y, %H:%M")

    counts = {s: 0 for s in STATUS_STYLES}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    summary_chips = "".join(
        f'<span style="display:inline-block;margin-right:12px;padding:4px 10px;'
        f'border-radius:12px;background:{STATUS_STYLES[s]["bg"]};color:{STATUS_STYLES[s]["fg"]};'
        f'font-size:13px;font-weight:600;">{STATUS_STYLES[s]["label"]}: {counts.get(s, 0)}</span>'
        for s in STATUS_STYLES
    )

    rows = []
    for r in results:
        style = STATUS_STYLES[r.status]
        last_updated_str = r.last_updated.strftime("%d %b %Y, %H:%M") if r.last_updated else "—"
        rows.append(
            f"""
            <tr>
                <td style="padding:10px 14px;border-bottom:1px solid #eee;">{r.partner_name}</td>
                <td style="padding:10px 14px;border-bottom:1px solid #eee;">
                    <span style="display:inline-block;padding:3px 10px;border-radius:10px;
                        background:{style['bg']};color:{style['fg']};font-size:12px;font-weight:600;">
                        {style['label']}
                    </span>
                </td>
                <td style="padding:10px 14px;border-bottom:1px solid #eee;">{r.available_dates or "—"}</td>
                <td style="padding:10px 14px;border-bottom:1px solid #eee;">{last_updated_str}</td>
                <td style="padding:10px 14px;border-bottom:1px solid #eee;color:#666;font-size:13px;">{r.note}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Partner Availability</title>
</head>
<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#fafafa;margin:0;padding:24px;">
    <div style="max-width:900px;margin:0 auto;">
        <h1 style="font-size:20px;margin-bottom:4px;">Partner Availability — {start_date} to {end_date}</h1>
        <p style="color:#666;font-size:13px;margin-top:0;">Generated {generated_at}</p>
        <div style="margin:16px 0;">{summary_chips}</div>
        <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
            <thead>
                <tr style="background:#f5f5f5;text-align:left;">
                    <th style="padding:10px 14px;">Partner</th>
                    <th style="padding:10px 14px;">Status</th>
                    <th style="padding:10px 14px;">Available Dates</th>
                    <th style="padding:10px 14px;">Last Updated</th>
                    <th style="padding:10px 14px;">Note</th>
                </tr>
            </thead>
            <tbody>
                {"".join(rows)}
            </tbody>
        </table>
    </div>
</body>
</html>
"""


def main() -> None:
    today = dt.date.today()
    start_date = today.isoformat()
    end_date = (today + dt.timedelta(days=7)).isoformat()

    if not PARTNERS:
        print("[warn] PARTNERS list is empty — add partners in pms_availability.py before running.", file=sys.stderr)

    results = build_report(PARTNERS, start_date, end_date)
    html = render_html(results, start_date, end_date)

    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {OUTPUT_HTML_PATH} ({len(results)} partner(s))")


if __name__ == "__main__":
    main()
