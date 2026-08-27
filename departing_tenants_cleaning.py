#!/usr/bin/env python3
"""
Departing tenants + end-of-tenancy cleaning preference — Res:Harmonics x Pipedrive.

Answers the recurring ops question: "who is leaving in the next N days, and of
those, who has told us they'll do the end-of-tenancy (EOT) clean themselves vs.
who wants Gravity to arrange it?"

Two data sources, each authoritative for a different half:

  Res:Harmonics (PMS)  -> WHO is leaving, and when. Plus tenant name + email.
                          This is the source of truth for departures: a tenant
                          reaching their natural contract end never generates a
                          Pipedrive deal, so a Pipedrive-only list silently
                          under-reports departures.
  Pipedrive (CRM)      -> WHETHER they've told us their cleaning preference.
                          There is no structured field for this; it lives in
                          free-text notes on the Extensions / Early Termination
                          deals (see CLEANING_* patterns below).

------------------------------------------------------------------------------
CONFIRMED ENDPOINTS (live, 2026-08-27)
------------------------------------------------------------------------------
  GET /api/v3/roomStays?checkOutDateFrom=&checkOutDateTo=&size=500
      -> {"content": [{roomStayId, roomStayStatus, startDate, endDate,
                       bookingContact: {firstName, lastName, emailAddress, id},
                       unit: {id, name, buildingName}, ...}], "page": {...}}
      A room stay is one booking of one unit by one contact. `unit.name` is
      formatted exactly like the Pipedrive unit field ("98 West Court"), which
      makes it a reliable join key between the two systems.
      Statuses seen in practice: CHECKED_IN (in residence), CONFIRMED (booked,
      not yet arrived), CANCELLED (ignored).

  GET /api/v3/roomStays?bookingContactId=&size=200
      -> every stay belonging to one contact. Used for renewal detection.

`discover` remains available for re-checking the spec if the API changes:
    python departing_tenants_cleaning.py discover

------------------------------------------------------------------------------
WHY THIS ISN'T JUST "ROOM STAYS ENDING SOON"
------------------------------------------------------------------------------
A tenant who renews gets a *new* room stay starting the day the old one ends,
rather than an extended end date on the existing one. So the naive query — room
stays with a checkout date in the window — reports renewing tenants as
departures. On the 2026-08-27 run, 15 of 54 matching room stays were renewals;
reporting them would have sent cleaners to 15 occupied flats.

So a contact is only leaving if the LAST of their stays ends inside the window.
Back-to-back bookings are stitched into one tenancy (see contiguous_tenancy_start)
so the reported tenancy length is the real one, not just the final segment.

Departures are split into residential tenants and short-stay guests by
TENANCY_MIN_NIGHTS. End-of-tenancy cleaning is a residential concept — a 2-night
stay gets ordinary turnover cleaning and never appears in the CRM — so mixing
them would inflate the "nothing recorded" bucket with rows that will never have
a record. Both groups are reported; only tenants are cross-checked against
Pipedrive.

------------------------------------------------------------------------------
CREDENTIALS (all via environment, never hard-coded)
------------------------------------------------------------------------------
    RESHARMONICS_CLIENT_ID       required
    RESHARMONICS_CLIENT_SECRET   required
    PIPEDRIVE_API_TOKEN          optional — without it the script still produces
                                 the departure list and writes departures.json,
                                 and the Pipedrive half can be done via the
                                 Pipedrive MCP tools instead.

Usage:
    python departing_tenants_cleaning.py discover
    python departing_tenants_cleaning.py report [--days 30] [--html out.html]
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

import requests

API_BASE_URL = os.environ.get("RESHARMONICS_API_BASE", "https://apiv3.rerumapp.uk")
AUTH_URL = os.environ.get("RESHARMONICS_AUTH_URL", "https://auth.rerumapp.uk/oauth2/token")
CLIENT_ID = os.environ.get("RESHARMONICS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("RESHARMONICS_CLIENT_SECRET")

PIPEDRIVE_BASE_URL = "https://api.pipedrive.com"
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN")

DEFAULT_DAYS = 30

# Confirmed live 2026-08-27. Overridable if the API changes — run `discover`.
DEPARTURES_ENDPOINT = os.environ.get("RESHARMONICS_DEPARTURES_ENDPOINT", "/api/v3/roomStays")

# A room stay in this state isn't a real occupancy and must never be treated as
# a departure or as evidence of a renewal.
IGNORED_STAY_STATUSES = {"CANCELLED", "NO_SHOW"}

# Below this, a stay is a short-let guest rather than a residential tenant.
# The live data has a clean gap here: stays cluster at <=14 nights (short-lets,
# mostly The Weymouth and OTA bookings) or >=31 nights (monthly tenancies).
TENANCY_MIN_NIGHTS = 28

MAX_WORKERS = 8

# Pipedrive pipelines that carry move-out conversations. Confirmed live
# 2026-08-27 via the Pipedrive MCP: pipeline 5 is "Extensions" (deals titled
# "<Name> - <UNIT> (Ext.N)"), pipeline 7 is "Early Termination" ("... - ET",
# stages Formal Notice / Objection Handling / Termination Admin / Refund).
PIPEDRIVE_MOVE_OUT_PIPELINES = (5, 7)

# Pipedrive deal custom-field keys, confirmed live 2026-08-27 by cross-checking
# known deals (e.g. deal 17564 "Dylan Moulder - WC 81 - ET" -> unit "81 West
# Court", contract end 2026-09-30).
PD_FIELD_CONTRACT_END = "39a24443c32c1043f92a3a5641e016ba58bed353"
PD_FIELD_UNIT = "5ca4575f1ce5eac39e8c8a7f57db84f00fd8bc17"
PD_FIELD_BUILDING = "f061944b5e04511943e57749e83376ccb6ebbb92"

# The PMS has a sandbox building with demo units — never report on it.
EXCLUDED_BUILDINGS = {"Gravity Test"}

OUTPUT_HTML_PATH = "departing_tenants_cleaning.html"
OUTPUT_JSON_PATH = "departures.json"

_token_cache: dict = {"access_token": None, "expires_at": 0}

# --- Cleaning-preference classification -------------------------------------
#
# Preference is recorded as free text by whoever handled the conversation, with
# no house style. Real examples pulled from the CRM on 2026-08-27:
#
#   Gravity does it : "End of tenancy cleaning scheduled."
#                     "NOT EXTENDING. REQUESTED EOT CLEANING."
#                     "He opted for end of tenancy cleaning."
#                     "she is happy to pay for the end-of-tenancy cleaning"
#   Tenant does it  : "EOT cleaning NOT requested. Tenant will take care of it."
#                     "will be doing the end of tenancy cleaning herself."
#   Neither (a task
#   someone set,
#   not an answer)  : "to check if he wants gravity to arrange his end of
#                      tenancy cleaning, and if not to advise him accordingly."
#                     "could you please let us know how you plan on arranging
#                      your end of tenancy cleaning?"
#
# That last bucket is the dangerous one: it mentions cleaning and reads positive
# on a naive keyword match, but records no tenant decision at all. Counting it
# as an answer would overstate coverage and under-book cleaners, so it gets its
# own AMBIGUOUS bucket that the report surfaces verbatim for a human to settle.

_CLEANING_MENTION_RE = re.compile(r"clean|\beot\b|end[\s\-]of[\s\-]tenancy", re.I)

# Checked first — an explicit "no"/"themselves" beats any positive keyword in
# the same breath ("EOT cleaning NOT requested. Tenant will take care of it.").
_CLEANING_SELF_PATTERNS = (
    r"\bnot\s+request",
    r"\bno\s+(eot\s+)?clean",
    r"\b(him|her|it)self\b",
    r"\bthemselves\b",
    r"\bown\s+(end[\s\-]of[\s\-]tenancy\s+)?clean",
    r"\btake\s+care\s+of\s+it\b",
    r"\bwill\s+(be\s+)?(doing|do|handle|sort)\b",
    r"\bdeclin",
    r"\bcleaning:\s*no\b",
    r"\barrang(e|ing)\s+(his|her|their)\s+own",
)

# Checked second — a recorded "yes".
_CLEANING_GRAVITY_PATTERNS = (
    r"\brequest(ed|s|ing)?\b",
    r"\bopted\s+for\b",
    r"\bscheduled\b",
    r"\bbooked\b",
    r"\barranged\b",
    r"\bconfirmed\b",
    r"\bhappy\s+to\s+pay\b",
    r"\bcleaning:\s*yes\b",
    r"\bwants?\s+gravity\s+to\b",
    r"\bcharge\b",
)

# Checked before either of the above — markers that the sentence is a question
# or an internal to-do rather than a tenant's answer.
_CLEANING_AMBIGUOUS_PATTERNS = (
    r"\?",
    r"\bto\s+check\b",
    r"\bneed\s+to\s+(ask|confirm|check)\b",
    r"\bplease\s+let\s+us\s+know\b",
    r"\bchase\b",
    r"\bfollow[\s\-]up\b",
    r"\bawait",
    r"\bno\s+(response|reply|answer)\b",
    r"\btbc\b",
    r"\bif\s+(he|she|they)\s+wants?\b",
)

PREF_GRAVITY = "gravity"
PREF_SELF = "self"
PREF_AMBIGUOUS = "ambiguous"
PREF_NONE = "not_recorded"


# --- Res:Harmonics auth ------------------------------------------------------

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


def fetch_openapi_spec() -> dict:
    resp = requests.get(f"{API_BASE_URL}/v3/api-docs", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


# --- Endpoint discovery ------------------------------------------------------

# Words that suggest an endpoint listing tenancies/stays, ranked by how strongly
# they imply "a tenant occupying a unit over a date range".
_DEPARTURE_KEYWORDS = {
    "tenancy": 10,
    "tenancies": 10,
    "booking": 8,
    "bookings": 8,
    "reservation": 7,
    "reservations": 7,
    "contract": 6,
    "stay": 5,
    "stays": 5,
    "lease": 5,
    "occupancy": 4,
}
_CLIENT_KEYWORDS = ("client", "customer", "guest", "tenant", "resident", "person")


def _spec_get_operations(spec: dict) -> Iterable[tuple[str, dict]]:
    for path, methods in (spec.get("paths") or {}).items():
        operation = (methods or {}).get("get")
        if operation:
            yield path, operation


def _param_names(operation: dict) -> list[str]:
    return [p.get("name", "") for p in (operation.get("parameters") or [])]


def discover(args: argparse.Namespace) -> int:
    """Print the tenancy/booking/client-shaped GET endpoints the API actually has."""
    spec = fetch_openapi_spec()
    operations = list(_spec_get_operations(spec))
    print(f"[info] spec exposes {len(operations)} GET operations\n", file=sys.stderr)

    interesting = []
    for path, operation in operations:
        haystack = f"{path} {operation.get('summary', '')} {operation.get('operationId', '')}".lower()
        score = sum(weight for word, weight in _DEPARTURE_KEYWORDS.items() if word in haystack)
        score += sum(2 for word in _CLIENT_KEYWORDS if word in haystack)
        if score:
            interesting.append((score, path, operation))

    if not interesting:
        print("No tenancy/booking/client-shaped endpoints matched. Dumping all GET paths:")
        for path, _ in sorted(operations):
            print(f"  {path}")
        return 0

    for score, path, operation in sorted(interesting, key=lambda x: (-x[0], x[1])):
        params = _param_names(operation)
        print(f"[{score:>3}] GET {path}")
        if operation.get("summary"):
            print(f"       {operation['summary']}")
        if params:
            print(f"       params: {', '.join(params)}")
        print()

    print(
        "Pick the one that lists tenancies with a date range, then re-run:\n"
        "  python departing_tenants_cleaning.py report --endpoint <path>\n"
        "and record it in CLAUDE.md.",
        file=sys.stderr,
    )
    return 0


# --- Departure fetch ---------------------------------------------------------

def _fetch_room_stays(params: dict) -> list[dict]:
    query = {"size": 500, **params}
    resp = requests.get(
        f"{API_BASE_URL}{DEPARTURES_ENDPOINT}", params=query, headers=_headers(), timeout=60
    )
    resp.raise_for_status()
    payload = resp.json()
    stays = payload.get("content")
    if stays is None:
        raise RuntimeError(
            f"GET {DEPARTURES_ENDPOINT} returned no 'content' key (got {list(payload)}). "
            "Run `discover` — the API shape may have changed."
        )
    return [s for s in stays if s.get("roomStayStatus") not in IGNORED_STAY_STATUSES]


def _fetch_stays_for_contact(contact_id: int) -> tuple[int, list[dict]]:
    try:
        return contact_id, _fetch_room_stays({"bookingContactId": contact_id, "size": 200})
    except requests.RequestException as exc:
        print(f"[warn] stay lookup failed for contact {contact_id}: {exc}", file=sys.stderr)
        return contact_id, []


def contiguous_tenancy_start(stays: list[dict], end_date: dt.date) -> dt.date:
    """
    Walk backwards through back-to-back bookings to find when the tenancy began.

    A tenant who renews annually has several room stays chained end-to-start
    (…2025-08-31, 2025-08-31…2026-08-31, …). Measuring only the final stay would
    call a four-year resident a one-year one, so consecutive stays are stitched
    into a single tenancy.
    """
    start = end_date
    changed = True
    while changed:
        changed = False
        for stay in stays:
            stay_start = dt.date.fromisoformat(stay["startDate"])
            stay_end = dt.date.fromisoformat(stay["endDate"])
            if stay_start < start <= stay_end:
                start = stay_start
                changed = True
    return start


def build_departures(
    stays_by_contact: dict[int, list[dict]],
    contacts: dict[int, dict],
    today: dt.date,
    horizon: dt.date,
) -> list[dict]:
    """
    Reduce every contact's stay history to at most one departure.

    A contact is leaving only if the LAST of their stays ends within the window.
    Anyone with a stay ending in the window but a later stay after it has renewed
    and is staying put — see the module docstring. Kept pure (no I/O) so the rule
    that decides whether a cleaner gets booked is directly testable.
    """
    departures = []
    for contact_id, stays in stays_by_contact.items():
        if not stays:
            continue
        final_end = max(dt.date.fromisoformat(s["endDate"]) for s in stays)
        if not (today <= final_end <= horizon):
            continue

        last_stay = next(s for s in stays if dt.date.fromisoformat(s["endDate"]) == final_end)
        unit = last_stay.get("unit") or {}
        if unit.get("buildingName") in EXCLUDED_BUILDINGS:
            continue

        contact = contacts.get(contact_id) or last_stay.get("bookingContact") or {}
        name = " ".join(
            part for part in (contact.get("firstName"), contact.get("lastName")) if part
        ).strip()
        tenancy_start = contiguous_tenancy_start(stays, final_end)
        nights = (final_end - tenancy_start).days

        departures.append(
            {
                "name": name or f"(contact {contact_id})",
                "email": contact.get("emailAddress"),
                "contact_id": contact_id,
                "unit": unit.get("name"),
                "building": unit.get("buildingName"),
                "end_date": final_end.isoformat(),
                "tenancy_start": tenancy_start.isoformat(),
                "nights": nights,
                "is_tenant": nights >= TENANCY_MIN_NIGHTS,
                "status": last_stay.get("roomStayStatus"),
            }
        )

    departures.sort(key=lambda d: (d["end_date"], d["name"]))
    return departures


def fetch_departures(days: int) -> list[dict]:
    """Everyone whose stay genuinely ends within the next `days` days."""
    today = dt.date.today()
    horizon = today + dt.timedelta(days=days)

    window_stays = _fetch_room_stays(
        {"checkOutDateFrom": today.isoformat(), "checkOutDateTo": horizon.isoformat()}
    )
    print(f"[info] {len(window_stays)} room stays end within {days} days", file=sys.stderr)

    contacts: dict[int, dict] = {}
    for stay in window_stays:
        contact = stay.get("bookingContact") or {}
        if contact.get("id") is not None:
            contacts[contact["id"]] = contact

    # Each contact's full history, so a renewal booked after the window is still
    # seen. Without this pass, renewing tenants read as departures.
    stays_by_contact: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for contact_id, stays in executor.map(_fetch_stays_for_contact, contacts):
            stays_by_contact[contact_id] = stays

    departures = build_departures(stays_by_contact, contacts, today, horizon)
    renewals = len(contacts) - len(departures)
    print(
        f"[info] {len(departures)} genuine departures "
        f"({renewals} contact(s) excluded as renewals or later departures)",
        file=sys.stderr,
    )
    return departures
# --- Pipedrive ---------------------------------------------------------------

def _pipedrive_get(path: str, params: dict) -> dict:
    if not PIPEDRIVE_API_TOKEN:
        raise RuntimeError("PIPEDRIVE_API_TOKEN is not set in the environment.")
    resp = requests.get(
        f"{PIPEDRIVE_BASE_URL}{path}",
        params=params,
        headers={"x-api-token": PIPEDRIVE_API_TOKEN, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_pipedrive_move_out_deals() -> list[dict]:
    """Deals from the Extensions and Early Termination pipelines."""
    deals: list[dict] = []
    for pipeline_id in PIPEDRIVE_MOVE_OUT_PIPELINES:
        cursor = None
        while True:
            params = {
                "pipeline_id": pipeline_id,
                "limit": 500,
                "custom_fields": ",".join(
                    (PD_FIELD_CONTRACT_END, PD_FIELD_UNIT, PD_FIELD_BUILDING)
                ),
                "include_option_labels": "true",
            }
            if cursor:
                params["cursor"] = cursor
            payload = _pipedrive_get("/api/v2/deals", params)
            deals.extend(payload.get("data") or [])
            cursor = (payload.get("additional_data") or {}).get("next_cursor")
            if not cursor:
                break
    return deals


def fetch_pipedrive_notes(deal_id: int) -> list[dict]:
    payload = _pipedrive_get("/api/v1/notes", {"deal_id": deal_id, "limit": 50})
    return payload.get("data") or []


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def classify_cleaning_preference(notes_text: Iterable[str]) -> tuple[str, Optional[str]]:
    """
    Classify a tenant's EOT cleaning preference from their deal notes.

    Returns (preference, evidence) where preference is one of PREF_SELF,
    PREF_GRAVITY, PREF_AMBIGUOUS or PREF_NONE, and evidence is the sentence the
    call was made from (so every classification can be audited against the CRM).

    Only sentences that actually mention cleaning are considered, and the most
    recent note wins — a tenant who first said "I'll do it" and later asked
    Gravity to handle it should read as Gravity.
    """
    best: tuple[str, Optional[str]] = (PREF_NONE, None)

    for text in notes_text:
        clean = _strip_html(text)
        if not _CLEANING_MENTION_RE.search(clean):
            continue
        for sentence in _split_sentences(clean):
            if not _CLEANING_MENTION_RE.search(sentence):
                continue
            low = sentence.lower()
            if any(re.search(p, low) for p in _CLEANING_AMBIGUOUS_PATTERNS):
                best = (PREF_AMBIGUOUS, sentence)
                continue
            if any(re.search(p, low) for p in _CLEANING_SELF_PATTERNS):
                best = (PREF_SELF, sentence)
                continue
            if any(re.search(p, low) for p in _CLEANING_GRAVITY_PATTERNS):
                best = (PREF_GRAVITY, sentence)
                continue
            if best[0] == PREF_NONE:
                best = (PREF_AMBIGUOUS, sentence)

    return best


def _normalise_name(name: Optional[str]) -> str:
    if not name:
        return ""
    return re.sub(r"[^a-z]", "", name.lower())


def _normalise_unit(unit: Optional[str]) -> str:
    """'81 West Court' / 'WC 81' -> a comparable key. Leading zeros dropped."""
    if not unit:
        return ""
    match = re.match(r"^\s*(\w+)\s+(.*)$", unit.strip())
    if not match:
        return re.sub(r"[^a-z0-9]", "", unit.lower())
    number, rest = match.groups()
    return re.sub(r"[^a-z0-9]", "", f"{number.lstrip('0')}{rest}".lower())


def match_departures_to_pipedrive(departures: list[dict]) -> list[dict]:
    """Attach each departure's Pipedrive deal and cleaning preference."""
    deals = fetch_pipedrive_move_out_deals()
    print(f"[info] {len(deals)} Pipedrive move-out deals fetched", file=sys.stderr)

    by_name: dict[str, list[dict]] = {}
    for deal in deals:
        custom = deal.get("custom_fields") or {}
        unit_field = custom.get(PD_FIELD_UNIT)
        unit_label = unit_field.get("label") if isinstance(unit_field, dict) else unit_field
        deal["_unit_key"] = _normalise_unit(unit_label) if unit_label else None
        # Deal titles lead with the tenant's name: "Dylan Moulder - WC 81 - ET".
        title_name = (deal.get("title") or "").split(" - ")[0]
        if title_name:
            by_name.setdefault(_normalise_name(title_name), []).append(deal)

    results = []
    for departure in departures:
        # Identity must come from the PERSON, never the unit. Units are re-let,
        # so a unit-only match reliably finds the PREVIOUS occupant's deal — on
        # the 2026-08-27 data, matching "46 West Court" returned the deal of a
        # tenant who had already moved out, whose cleaning preference would then
        # have been attributed to the current one. Unit is corroboration only.
        candidates = by_name.get(_normalise_name(departure.get("name")), [])
        matched_on = "name"
        if not candidates:
            results.append({**departure, "deal": None, "matched_on": None,
                            "preference": PREF_NONE, "evidence": None})
            continue

        unit_key = _normalise_unit(departure.get("unit"))
        same_unit = [d for d in candidates if unit_key and d.get("_unit_key") == unit_key]
        if same_unit:
            candidates, matched_on = same_unit, "name+unit"

        # Most recently updated deal is the live conversation.
        deal = max(candidates, key=lambda d: d.get("update_time") or "")
        notes = fetch_pipedrive_notes(deal["id"])
        notes_sorted = sorted(notes, key=lambda n: n.get("update_time") or n.get("add_time") or "")
        preference, evidence = classify_cleaning_preference(n.get("content", "") for n in notes_sorted)

        results.append(
            {
                **departure,
                "deal": {"id": deal["id"], "title": deal.get("title"), "status": deal.get("status")},
                "matched_on": matched_on,
                "preference": preference,
                "evidence": evidence,
            }
        )
    return results


# --- Output ------------------------------------------------------------------

_PREF_LABELS = {
    PREF_GRAVITY: "Gravity arranges",
    PREF_SELF: "Tenant does it",
    PREF_AMBIGUOUS: "Asked, no answer yet",
    PREF_NONE: "Not recorded",
}
_PREF_COLOURS = {
    PREF_GRAVITY: ("#e6f4ea", "#1e7e34"),
    PREF_SELF: ("#e8f0fe", "#1a56b0"),
    PREF_AMBIGUOUS: ("#fff4e5", "#a35a00"),
    PREF_NONE: ("#f1f1f1", "#666666"),
}


def summarise(rows: list[dict]) -> dict[str, int]:
    counts = {key: 0 for key in _PREF_LABELS}
    for row in rows:
        counts[row["preference"]] += 1
    return counts


def print_console_report(rows: list[dict], days: int) -> None:
    counts = summarise(rows)
    print()
    print(f"Tenants leaving in the next {days} days: {len(rows)}")
    print(f"  Gravity arranges the EOT clean : {counts[PREF_GRAVITY]}")
    print(f"  Tenant doing it themselves     : {counts[PREF_SELF]}")
    print(f"  Asked, no answer yet           : {counts[PREF_AMBIGUOUS]}")
    print(f"  Nothing recorded               : {counts[PREF_NONE]}")
    print()
    for row in rows:
        unit = row.get("unit") or "?"
        email = row.get("email") or "(no email)"
        print(f"  {row['end_date']}  {row['name']:<32} {unit:<20} {email:<34} "
              f"{_PREF_LABELS[row['preference']]}")
    print()


def _render_short_stays(short_stays: list[dict]) -> str:
    """Short-let checkouts, listed for completeness — no EOT cleaning applies."""
    if not short_stays:
        return ""
    items = "".join(
        f'<li style="margin:2px 0;">{s["end_date"]} — {s["name"]} · '
        f'{s.get("unit") or "?"} · {s["nights"]} night{"s" if s["nights"] != 1 else ""}</li>'
        for s in short_stays
    )
    return f"""
    <div style="padding:16px 26px;border-top:1px solid #eee;font-size:12px;color:#666;">
      <strong style="color:#444;">Short-stay guests also checking out ({len(short_stays)})</strong>
      <p style="margin:4px 0 8px;color:#999;">Turnover cleans, not end-of-tenancy — these
         never appear in the CRM.</p>
      <ul style="margin:0;padding-left:18px;">{items}</ul>
    </div>"""


def render_html(rows: list[dict], days: int, short_stays: Optional[list[dict]] = None) -> str:
    counts = summarise(rows)
    today = dt.date.today()
    short_stays = short_stays or []

    body_rows = []
    for row in rows:
        background, colour = _PREF_COLOURS[row["preference"]]
        evidence = row.get("evidence") or ""
        evidence_html = (
            f'<br><span style="color:#888;font-size:11px;font-style:italic;">“{evidence}”</span>'
            if evidence else ""
        )
        email = row.get("email")
        email_html = f'<a href="mailto:{email}" style="color:#1a56b0;">{email}</a>' if email else "—"
        body_rows.append(
            f"""
            <tr>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;font-weight:600;">{row['name']}</td>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;">{row.get('unit') or '—'}<br>
                    <span style="color:#888;font-size:12px;">{row.get('building') or ''}</span></td>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;">{email_html}</td>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;">{row['end_date']}</td>
                <td style="padding:9px 14px;border-bottom:1px solid #eee;">
                    <span style="display:inline-block;padding:2px 8px;border-radius:10px;
                        background:{background};color:{colour};font-size:11px;font-weight:700;">
                        {_PREF_LABELS[row['preference']]}</span>{evidence_html}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Departing tenants — EOT cleaning</title></head>
<body style="margin:0;padding:24px;background:#fafafa;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#222;">
  <div style="max-width:1000px;margin:0 auto;background:#fff;border-radius:10px;
              box-shadow:0 1px 3px rgba(0,0,0,0.08);overflow:hidden;">
    <div style="padding:22px 26px;border-bottom:1px solid #eee;">
      <h1 style="margin:0;font-size:20px;">Departing tenants — end-of-tenancy cleaning</h1>
      <p style="margin:6px 0 0;color:#888;font-size:13px;">
        Leaving within {days} days of {today.strftime('%d %b %Y')} ·
        {len(rows)} tenant{'s' if len(rows) != 1 else ''}</p>
    </div>
    <div style="padding:16px 26px;border-bottom:1px solid #eee;font-size:13px;">
      <strong>{counts[PREF_GRAVITY]}</strong> want Gravity to clean ·
      <strong>{counts[PREF_SELF]}</strong> doing it themselves ·
      <strong>{counts[PREF_AMBIGUOUS]}</strong> asked but no answer ·
      <strong>{counts[PREF_NONE]}</strong> nothing recorded
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="background:#fbfbfb;text-align:left;color:#666;font-size:11px;
                        text-transform:uppercase;letter-spacing:0.4px;">
        <th style="padding:10px 14px;">Tenant</th><th style="padding:10px 14px;">Unit</th>
        <th style="padding:10px 14px;">Email</th><th style="padding:10px 14px;">Leaves</th>
        <th style="padding:10px 14px;">EOT cleaning</th>
      </tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
    {_render_short_stays(short_stays)}
    <div style="padding:14px 26px;color:#999;font-size:11px;border-top:1px solid #eee;">
      Departures from Res:Harmonics · cleaning preference parsed from Pipedrive deal notes.
      Renewing tenants are excluded: a renewal is a new room stay, so anyone whose
      final stay ends after this window is still in residence.
      Quoted text is the note the classification came from — verify anything marked
      “Asked, no answer yet” before booking cleaners.
    </div>
  </div>
</body></html>"""


# --- Entry points ------------------------------------------------------------

def report(args: argparse.Namespace) -> int:
    departures = fetch_departures(args.days)
    tenants = [d for d in departures if d["is_tenant"]]
    short_stays = [d for d in departures if not d["is_tenant"]]
    print(
        f"[info] {len(tenants)} residential tenants and {len(short_stays)} short-stay "
        f"guests leaving within {args.days} days",
        file=sys.stderr,
    )

    def without_crm(reason: str) -> int:
        with open(OUTPUT_JSON_PATH, "w") as handle:
            json.dump({"tenants": tenants, "short_stays": short_stays}, handle, indent=2)
        print(
            f"\n[warn] {reason}\n"
            f"       Wrote the departure list to {OUTPUT_JSON_PATH} without cleaning\n"
            "       preferences. Do the CRM half via the Pipedrive MCP tools instead.",
            file=sys.stderr,
        )
        for departure in tenants:
            print(f"  {departure['end_date']}  {departure['name']:<32} "
                  f"{departure.get('unit') or '?':<20} {departure.get('email') or '(no email)'}")
        return 0

    if not PIPEDRIVE_API_TOKEN:
        return without_crm("PIPEDRIVE_API_TOKEN not set.")

    # Only tenants get the CRM lookup — a short-let guest has no EOT conversation.
    try:
        rows = match_departures_to_pipedrive(tenants)
    except requests.exceptions.ProxyError as exc:
        # Some networks (including Claude Code's sandbox) allow the PMS host but
        # deny api.pipedrive.com by egress policy. That's a policy decision, not
        # a bug — degrade to the departure list rather than losing the whole run.
        return without_crm(f"Pipedrive is unreachable from this network: {exc}")
    print_console_report(rows, args.days)
    if short_stays:
        print(f"Short-stay guests also checking out (no EOT cleaning applies): {len(short_stays)}")
        for stay in short_stays:
            print(f"  {stay['end_date']}  {stay['name']:<32} {stay.get('unit') or '?':<20} "
                  f"{stay['nights']}n")
        print()

    with open(OUTPUT_JSON_PATH, "w") as handle:
        json.dump({"tenants": rows, "short_stays": short_stays}, handle, indent=2)
    html_path = args.html or OUTPUT_HTML_PATH
    with open(html_path, "w") as handle:
        handle.write(render_html(rows, args.days, short_stays))
    print(f"[info] wrote {html_path} and {OUTPUT_JSON_PATH}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser(
        "discover", help="list tenancy/booking-shaped endpoints from the live OpenAPI spec"
    )
    discover_parser.set_defaults(func=discover)

    report_parser = subparsers.add_parser("report", help="build the departing-tenants report")
    report_parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                               help=f"departure horizon in days (default {DEFAULT_DAYS})")
    report_parser.add_argument("--html", default=None, help=f"HTML output path (default {OUTPUT_HTML_PATH})")
    report_parser.set_defaults(func=report)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
