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
ENDPOINT DISCOVERY — read this before debugging a 404
------------------------------------------------------------------------------
The units/availability/rates endpoints used elsewhere in this repo were
confirmed live (see CLAUDE.md). The *tenancy/booking* endpoint needed here was
NOT — the session that wrote this file had no PMS credentials, and CLAUDE.md
records that blind-guessing endpoint names has burned this project before.

So this script does not hard-code a guess. It resolves the endpoint at runtime
from the live OpenAPI spec at GET /v3/api-docs, and prints what it picked so the
choice is auditable. Two escape hatches:

    python departing_tenants_cleaning.py discover
        Dumps every GET path in the spec that looks tenancy/booking/client
        related, with its parameters. Run this first on a fresh credential set.

    python departing_tenants_cleaning.py report --endpoint /api/v3/bookings
        Pins the endpoint explicitly (or set RESHARMONICS_DEPARTURES_ENDPOINT),
        bypassing auto-resolution entirely.

Record the confirmed path in CLAUDE.md once you've seen it work, the same way
the availability endpoints were recorded.

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
from typing import Any, Iterable, Optional

import requests

API_BASE_URL = os.environ.get("RESHARMONICS_API_BASE", "https://apiv3.rerumapp.uk")
AUTH_URL = os.environ.get("RESHARMONICS_AUTH_URL", "https://auth.rerumapp.uk/oauth2/token")
CLIENT_ID = os.environ.get("RESHARMONICS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("RESHARMONICS_CLIENT_SECRET")

PIPEDRIVE_BASE_URL = "https://api.pipedrive.com"
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN")

DEFAULT_DAYS = 30

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


def resolve_departures_endpoint(spec: dict) -> tuple[str, dict]:
    """
    Pick the GET endpoint that lists tenancies over a date range.

    Prefers an operation that both reads as tenancy/booking-shaped AND accepts
    date-range parameters, since that's what we need to filter departures. The
    choice is printed so a wrong guess is obvious rather than silent.
    """
    override = os.environ.get("RESHARMONICS_DEPARTURES_ENDPOINT")
    if override:
        operation = (spec.get("paths") or {}).get(override, {}).get("get") or {}
        return override, operation

    best: Optional[tuple[int, str, dict]] = None
    for path, operation in _spec_get_operations(spec):
        haystack = f"{path} {operation.get('summary', '')} {operation.get('operationId', '')}".lower()
        score = sum(weight for word, weight in _DEPARTURE_KEYWORDS.items() if word in haystack)
        if not score:
            continue
        # Path parameters mean "one specific record", not a listing.
        if "{" in path:
            continue
        params = [p.lower() for p in _param_names(operation)]
        if any("date" in p or "from" in p or "to" in p for p in params):
            score += 10
        if any(p in ("size", "page", "limit") for p in params):
            score += 2
        if best is None or score > best[0]:
            best = (score, path, operation)

    if best is None:
        raise RuntimeError(
            "Could not resolve a departures endpoint from the OpenAPI spec.\n"
            "Run `python departing_tenants_cleaning.py discover` and pass the right "
            "path via --endpoint or RESHARMONICS_DEPARTURES_ENDPOINT."
        )
    return best[1], best[2]


def _pick_param(candidates: list[str], *wanted: str) -> Optional[str]:
    """Find the actual spelling of a parameter (dateFrom vs date_from vs from)."""
    lowered = {c.lower(): c for c in candidates}
    for want in wanted:
        if want.lower() in lowered:
            return lowered[want.lower()]
    for want in wanted:
        for low, original in lowered.items():
            if want.lower() in low:
                return original
    return None


# --- Tolerant field extraction ----------------------------------------------
#
# The tenancy record's schema is unconfirmed, so rather than assuming key names
# we search each record for the shapes we need. Every extracted value is echoed
# in the JSON output so a mis-pick is visible.

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _walk(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, sub in value.items():
            yield from _walk(sub, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            yield from _walk(sub, f"{prefix}[{index}]")
    else:
        yield prefix, value


def extract_email(record: dict) -> Optional[str]:
    for path, value in _walk(record):
        if isinstance(value, str) and _EMAIL_RE.match(value.strip()):
            # Prefer a field that actually calls itself an email.
            if "email" in path.lower():
                return value.strip()
    for _, value in _walk(record):
        if isinstance(value, str) and _EMAIL_RE.match(value.strip()):
            return value.strip()
    return None


def extract_name(record: dict) -> Optional[str]:
    first = last = full = None
    for path, value in _walk(record):
        if not isinstance(value, str) or not value.strip():
            continue
        low = path.lower()
        if "email" in low:
            continue
        if full is None and re.search(r"(^|\.)(fullname|full_name|name|displayname)$", low):
            full = value.strip()
        elif first is None and re.search(r"(firstname|first_name|forename|givenname)$", low):
            first = value.strip()
        elif last is None and re.search(r"(lastname|last_name|surname|familyname)$", low):
            last = value.strip()
    if first or last:
        return " ".join(p for p in (first, last) if p)
    return full


def extract_date(record: dict, *wanted: str) -> Optional[dt.date]:
    for path, value in _walk(record):
        low = path.lower()
        if not any(w.lower() in low for w in wanted):
            continue
        if isinstance(value, str) and len(value) >= 10:
            try:
                return dt.date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def extract_unit(record: dict) -> Optional[str]:
    for path, value in _walk(record):
        low = path.lower()
        if isinstance(value, str) and value.strip() and re.search(r"unit.*name|unitname|room|apartment", low):
            return value.strip()
    return None


def extract_building(record: dict) -> Optional[str]:
    for path, value in _walk(record):
        low = path.lower()
        if isinstance(value, str) and value.strip() and "building" in low:
            return value.strip()
    return None


# --- Departure fetch ---------------------------------------------------------

def fetch_departures(days: int, endpoint_override: Optional[str] = None) -> list[dict]:
    """Tenancies whose end date falls within the next `days` days."""
    spec = fetch_openapi_spec()
    if endpoint_override:
        path = endpoint_override
        operation = (spec.get("paths") or {}).get(path, {}).get("get") or {}
    else:
        path, operation = resolve_departures_endpoint(spec)

    params_available = _param_names(operation)
    print(f"[info] using departures endpoint: GET {path}", file=sys.stderr)
    if params_available:
        print(f"[info] endpoint params: {', '.join(params_available)}", file=sys.stderr)

    today = dt.date.today()
    horizon = today + dt.timedelta(days=days)

    query: dict[str, Any] = {}
    from_param = _pick_param(params_available, "dateFrom", "startDate", "from")
    to_param = _pick_param(params_available, "dateTo", "endDate", "to")
    size_param = _pick_param(params_available, "size", "limit", "pageSize")
    if from_param:
        query[from_param] = today.isoformat()
    if to_param:
        query[to_param] = horizon.isoformat()
    if size_param:
        query[size_param] = 500

    resp = requests.get(f"{API_BASE_URL}{path}", params=query, headers=_headers(), timeout=45)
    resp.raise_for_status()
    payload = resp.json()

    records = payload.get("content") if isinstance(payload, dict) else payload
    if records is None and isinstance(payload, dict):
        # Some endpoints wrap the list under a different key.
        for value in payload.values():
            if isinstance(value, list):
                records = value
                break
    if not isinstance(records, list):
        raise RuntimeError(
            f"GET {path} did not return a list of records (got keys: "
            f"{list(payload) if isinstance(payload, dict) else type(payload).__name__}).\n"
            "Run `discover` and pin the correct endpoint with --endpoint."
        )

    print(f"[info] {len(records)} records returned; filtering to departures", file=sys.stderr)

    departures = []
    for record in records:
        if not isinstance(record, dict):
            continue
        end_date = extract_date(record, "enddate", "departure", "checkout", "moveout", "to")
        if end_date is None or not (today <= end_date <= horizon):
            continue
        building = extract_building(record)
        if building in EXCLUDED_BUILDINGS:
            continue
        departures.append(
            {
                "name": extract_name(record) or "(name not found in record)",
                "email": extract_email(record),
                "unit": extract_unit(record),
                "building": building,
                "end_date": end_date.isoformat(),
            }
        )

    departures.sort(key=lambda d: (d["end_date"], d["name"]))
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

    by_unit: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for deal in deals:
        custom = deal.get("custom_fields") or {}
        unit_field = custom.get(PD_FIELD_UNIT)
        unit_label = unit_field.get("label") if isinstance(unit_field, dict) else unit_field
        if unit_label:
            by_unit.setdefault(_normalise_unit(unit_label), []).append(deal)
        # Deal titles lead with the tenant's name: "Dylan Moulder - WC 81 - ET".
        title_name = (deal.get("title") or "").split(" - ")[0]
        if title_name:
            by_name.setdefault(_normalise_name(title_name), []).append(deal)

    results = []
    for departure in departures:
        candidates = by_unit.get(_normalise_unit(departure.get("unit")), [])
        matched_on = "unit"
        if not candidates:
            candidates = by_name.get(_normalise_name(departure.get("name")), [])
            matched_on = "name"
        if not candidates:
            results.append({**departure, "deal": None, "matched_on": None,
                            "preference": PREF_NONE, "evidence": None})
            continue

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


def render_html(rows: list[dict], days: int) -> str:
    counts = summarise(rows)
    today = dt.date.today()

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
    <div style="padding:14px 26px;color:#999;font-size:11px;border-top:1px solid #eee;">
      Departures from Res:Harmonics · cleaning preference parsed from Pipedrive deal notes.
      Quoted text is the note the classification came from — verify anything marked
      “Asked, no answer yet” before booking cleaners.
    </div>
  </div>
</body></html>"""


# --- Entry points ------------------------------------------------------------

def report(args: argparse.Namespace) -> int:
    departures = fetch_departures(args.days, args.endpoint)
    print(f"[info] {len(departures)} tenants leaving within {args.days} days", file=sys.stderr)

    if not PIPEDRIVE_API_TOKEN:
        with open(OUTPUT_JSON_PATH, "w") as handle:
            json.dump(departures, handle, indent=2)
        print(
            f"\n[warn] PIPEDRIVE_API_TOKEN not set — wrote the departure list to "
            f"{OUTPUT_JSON_PATH} without cleaning preferences.\n"
            "       Set the token, or do the CRM half via the Pipedrive MCP tools.",
            file=sys.stderr,
        )
        for departure in departures:
            print(f"  {departure['end_date']}  {departure['name']:<32} "
                  f"{departure.get('unit') or '?':<20} {departure.get('email') or '(no email)'}")
        return 0

    rows = match_departures_to_pipedrive(departures)
    print_console_report(rows, args.days)

    with open(OUTPUT_JSON_PATH, "w") as handle:
        json.dump(rows, handle, indent=2)
    html_path = args.html or OUTPUT_HTML_PATH
    with open(html_path, "w") as handle:
        handle.write(render_html(rows, args.days))
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
    report_parser.add_argument("--endpoint", default=None,
                               help="pin the PMS departures endpoint instead of auto-resolving")
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
