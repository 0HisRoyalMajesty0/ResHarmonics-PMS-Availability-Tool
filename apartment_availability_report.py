#!/usr/bin/env python3
"""
Apartment-level availability + pricing report — Res:Harmonics (Rerum API v3) -> HTML.

Confirmed live against the real API on 2026-07-08 (see CLAUDE.md for how the
endpoints were discovered — the OpenAPI spec at /v3/api-docs on the Rerum app
host, since apidocs.resharmonics.com is JS-rendered and couldn't be scraped
directly).

CONFIRMED ENDPOINTS (all under API_BASE_URL, Bearer auth):
  GET /api/v3/units
      -> {"content": [{"id", "unitName", "buildingName", "bookable", ...}], "page": {...}}
      Paginated list of units. Use size=500 to get all in one page (portfolio
      currently has ~300 units).

  GET /api/v3/units/{id}
      -> {"id", "name", "unitType": {"id", "name"}, "publish", "bookable", ...}
      Per-unit detail. Needed for unitType.id (used to look up rates).

  GET /api/v3/availabilities/unit/{unitId}/intervals?dateFrom=&dateTo=
      -> {"content": [{"startDate", "endDate", "available": bool}], "page": {...}}
      Ground truth for when a specific unit is/isn't vacant. This is the
      right source for "available from" — NOT the /api/v3/availabilities
      search endpoint (see note below).

  GET /api/v3/rates?unitTypeId=&dateFrom=&dateTo=
      -> {"content": [{"name", "rateType" ("MONTHLY"/"DAILY"/"WEEKLY"),
                        "occupancyCount", "rates": [{"date","amount",...}]}]}
      Rate calendar per unit type (not per individual unit). Used for price.

NOTE on /api/v3/availabilities (the "search" endpoint): it looks like the
obvious choice for combined availability+price, but it implements full
booking-engine search semantics (lead time, minimum-stay, closed-to-arrival
rules) rather than raw vacancy — querying it with a unit's actual vacancy
date frequently returns zero results even though the unit IS vacant per
/intervals. Don't use it for "is this unit available" — use it only if you
specifically want date ranges that satisfy real booking rules.

AUTH: same OAuth2 client-credentials flow as pms_availability.py — see that
file's get_access_token() docstring. Requires RESHARMONICS_CLIENT_ID /
RESHARMONICS_CLIENT_SECRET in the environment.
"""

import os
import re
import sys
import time
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

API_BASE_URL = os.environ.get("RESHARMONICS_API_BASE", "https://apiv3.rerumapp.uk")
AUTH_URL = os.environ.get("RESHARMONICS_AUTH_URL", "https://auth.rerumapp.uk/oauth2/token")
CLIENT_ID = os.environ.get("RESHARMONICS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("RESHARMONICS_CLIENT_SECRET")

# How far ahead to look for the next vacancy / rate calendar entries.
LOOKAHEAD_DAYS = 400
MAX_WORKERS = 8

OUTPUT_HTML_PATH = "apartment_availability_report.html"

_BAD_RATE_NAME_RE = re.compile(r"override|sales over ?ride|system", re.I)

_token_cache: dict = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    """OAuth2 client-credentials exchange. See pms_availability.py for details."""
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
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + payload.get("expires_in", 3600)
    return _token_cache["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"}


def fetch_bookable_units() -> list[dict]:
    resp = requests.get(f"{API_BASE_URL}/api/v3/units", params={"size": 500}, headers=_headers(), timeout=30)
    resp.raise_for_status()
    units = resp.json().get("content", [])
    return [u for u in units if u.get("bookable")]


def fetch_unit_type(unit_id: int) -> tuple[int, Optional[dict]]:
    try:
        resp = requests.get(f"{API_BASE_URL}/api/v3/units/{unit_id}", headers=_headers(), timeout=20)
        resp.raise_for_status()
        data = resp.json()
        unit_type = data.get("unitType") or {}
        return unit_id, {"id": unit_type.get("id"), "name": unit_type.get("name")}
    except requests.RequestException as exc:
        print(f"[warn] unit type lookup failed for unit {unit_id}: {exc}", file=sys.stderr)
        return unit_id, None


def fetch_intervals(unit_id: int, date_from: str, date_to: str) -> tuple[int, Optional[list]]:
    try:
        resp = requests.get(
            f"{API_BASE_URL}/api/v3/availabilities/unit/{unit_id}/intervals",
            params={"dateFrom": date_from, "dateTo": date_to, "size": 50},
            headers=_headers(),
            timeout=20,
        )
        resp.raise_for_status()
        return unit_id, resp.json().get("content", [])
    except requests.RequestException as exc:
        print(f"[warn] intervals lookup failed for unit {unit_id}: {exc}", file=sys.stderr)
        return unit_id, None


def fetch_rates_for_type(unit_type_id: int, date_from: str, date_to: str) -> tuple[int, Optional[list]]:
    try:
        resp = requests.get(
            f"{API_BASE_URL}/api/v3/rates",
            params={"dateFrom": date_from, "dateTo": date_to, "unitTypeId": unit_type_id, "size": 100},
            headers=_headers(),
            timeout=25,
        )
        resp.raise_for_status()
        return unit_type_id, resp.json().get("content", [])
    except requests.RequestException as exc:
        print(f"[warn] rate lookup failed for unit type {unit_type_id}: {exc}", file=sys.stderr)
        return unit_type_id, None


def next_available_from(intervals: list, today: dt.date) -> Optional[dt.date]:
    for interval in sorted(intervals, key=lambda x: x["startDate"]):
        start = dt.date.fromisoformat(interval["startDate"])
        end = dt.date.fromisoformat(interval["endDate"])
        if interval["available"]:
            if start <= today <= end:
                return today
            if start >= today:
                return start
    return None


def best_monthly_rate(rate_entries: list, target_date: dt.date) -> Optional[tuple[str, float]]:
    """Picks a representative single-occupancy monthly rate for the target date."""
    candidates = []
    for entry in rate_entries:
        if entry.get("rateType") != "MONTHLY":
            continue
        if _BAD_RATE_NAME_RE.search(entry.get("name") or ""):
            continue
        if entry.get("occupancyCount") not in (1, None):
            continue
        for r in entry.get("rates", []):
            if r.get("amount"):
                candidates.append((r["date"], r["amount"]))

    if not candidates:
        # Relax the single-occupancy filter if that's all this unit type has.
        for entry in rate_entries:
            if entry.get("rateType") != "MONTHLY":
                continue
            if _BAD_RATE_NAME_RE.search(entry.get("name") or ""):
                continue
            for r in entry.get("rates", []):
                if r.get("amount"):
                    candidates.append((r["date"], r["amount"]))

    if not candidates:
        return None

    target = target_date.isoformat()
    on_or_after = sorted([c for c in candidates if c[0] >= target], key=lambda c: c[0])
    pool = on_or_after if on_or_after else sorted(candidates, key=lambda c: c[0], reverse=True)
    chosen_date = pool[0][0]
    cheapest_on_date = min((c for c in pool if c[0] == chosen_date), key=lambda c: c[1])
    return cheapest_on_date


def parse_apartment_number(unit_name: str) -> str:
    match = re.match(r"^([\w.\-]+)\s+.+$", unit_name.strip())
    return match.group(1) if match else unit_name.strip()


def build_report_rows() -> list[dict]:
    today = dt.date.today()
    date_from = today.isoformat()
    date_to = (today + dt.timedelta(days=LOOKAHEAD_DAYS)).isoformat()

    units = [u for u in fetch_bookable_units() if u.get("buildingName") != "Gravity Test"]
    print(f"[info] {len(units)} bookable units", file=sys.stderr)

    unit_types: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(fetch_unit_type, u["id"]) for u in units]
        for fut in as_completed(futures):
            uid, ut = fut.result()
            if ut:
                unit_types[uid] = ut

    intervals_by_unit: dict[int, list] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(fetch_intervals, u["id"], date_from, date_to) for u in units]
        for fut in as_completed(futures):
            uid, ivals = fut.result()
            if ivals is not None:
                intervals_by_unit[uid] = ivals

    distinct_type_ids = {ut["id"] for ut in unit_types.values() if ut.get("id")}
    rates_by_type: dict[int, list] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(fetch_rates_for_type, tid, date_from, date_to) for tid in distinct_type_ids]
        for fut in as_completed(futures):
            tid, rates = fut.result()
            if rates is not None:
                rates_by_type[tid] = rates

    rows = []
    for u in units:
        uid = u["id"]
        ut = unit_types.get(uid)
        ivals = intervals_by_unit.get(uid)
        if not ut or not ivals:
            continue

        available_from = next_available_from(ivals, today)
        if available_from is None:
            continue

        rate_entries = rates_by_type.get(ut["id"])
        if not rate_entries:
            continue
        rate = best_monthly_rate(rate_entries, available_from)
        if rate is None:
            continue
        _, amount = rate

        rows.append(
            {
                "apartment_number": parse_apartment_number(u["unitName"]),
                "apartment_name": u["buildingName"],
                "unit_type": ut.get("name") or "",
                "price_monthly_gbp": amount,
                "available_from": available_from,
            }
        )

    rows.sort(key=lambda r: (r["available_from"], r["apartment_name"], r["apartment_number"]))
    return rows


def render_html(rows: list[dict]) -> str:
    generated_at = dt.datetime.now().strftime("%d %b %Y, %H:%M")
    today = dt.date.today()

    row_html = []
    for r in rows:
        is_now = r["available_from"] == today
        badge = (
            '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            'background:#e6f4ea;color:#1e7e34;font-size:11px;font-weight:700;margin-left:8px;">NOW</span>'
            if is_now
            else ""
        )
        row_html.append(
            f"""
            <tr>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;font-weight:600;">{r['apartment_number']}</td>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;">
                    {r['apartment_name']}<br>
                    <span style="color:#888;font-size:12px;">{r['unit_type']}</span>
                </td>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;">£{r['price_monthly_gbp']:,.0f} / month</td>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;">{r['available_from'].strftime('%d %b %Y')}{badge}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GravityCo — Apartment Availability</title>
</head>
<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#fafafa;margin:0;padding:24px;">
    <div style="max-width:900px;margin:0 auto;">
        <div style="font-size:26px;font-weight:800;letter-spacing:-0.5px;color:#111;margin-bottom:4px;">GravityCo</div>
        <h1 style="font-size:19px;margin:0 0 4px 0;color:#111;">Live Apartment Availability</h1>
        <p style="color:#666;font-size:13px;margin-top:0;">
            Generated {generated_at} · {len(rows)} apartments with confirmed availability &amp; pricing
        </p>
        <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);margin-top:12px;">
            <thead>
                <tr style="background:#111;color:#fff;text-align:left;">
                    <th style="padding:10px 14px;">Apt #</th>
                    <th style="padding:10px 14px;">Apartment</th>
                    <th style="padding:10px 14px;">Current Price</th>
                    <th style="padding:10px 14px;">Available From</th>
                </tr>
            </thead>
            <tbody>
                {"".join(row_html)}
            </tbody>
        </table>
    </div>
</body>
</html>
"""


def main() -> None:
    rows = build_report_rows()
    html = render_html(rows)
    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_HTML_PATH} ({len(rows)} apartments)")


if __name__ == "__main__":
    main()
