#!/usr/bin/env python3
"""
Tests for departing_tenants_cleaning.py.

Every note string below is real text pulled from the Gravity Pipedrive account
on 2026-08-27, not invented fixtures — the classifier's whole job is coping with
how the ops team actually writes these notes, so testing it against tidied-up
prose would prove nothing.

The departure rules are tested against real room-stay shapes taken from the live
PMS, including the renewal cases that made the naive "stay ends soon" query
wrong. HTTP calls themselves aren't covered; build_departures is pure so the
rule that decides whether a cleaner gets booked is directly testable.
"""

import datetime as dt
import unittest

import departing_tenants_cleaning

from departing_tenants_cleaning import (
    PREF_AMBIGUOUS,
    PREF_GRAVITY,
    PREF_NONE,
    PREF_SELF,
    TENANCY_MIN_NIGHTS,
    _normalise_name,
    _normalise_unit,
    _strip_html,
    build_departures,
    classify_cleaning_preference,
    contiguous_tenancy_start,
)

TODAY = dt.date(2026, 8, 27)
HORIZON = TODAY + dt.timedelta(days=30)


def stay(start, end, status="CHECKED_IN", unit="98 West Court", building="Gravity Hounslow Central",
         stay_id=1):
    return {
        "roomStayId": stay_id,
        "roomStayStatus": status,
        "startDate": start,
        "endDate": end,
        "unit": {"id": 1, "name": unit, "buildingName": building},
    }


CONTACT = {"id": 7, "firstName": "Sokaina", "lastName": "Alrihani",
           "emailAddress": "sokainayassine@hotmail.com"}

_UNIT_KEY = departing_tenants_cleaning.PD_FIELD_UNIT


def _match(deals, name, unit):
    """Run match_departures_to_pipedrive against fixed deals, with no HTTP."""
    original_deals = departing_tenants_cleaning.fetch_pipedrive_move_out_deals
    original_notes = departing_tenants_cleaning.fetch_pipedrive_notes
    departing_tenants_cleaning.fetch_pipedrive_move_out_deals = lambda: deals
    departing_tenants_cleaning.fetch_pipedrive_notes = lambda deal_id: []
    try:
        return departing_tenants_cleaning.match_departures_to_pipedrive(
            [{"name": name, "unit": unit, "email": None, "end_date": "2026-09-01"}]
        )
    finally:
        departing_tenants_cleaning.fetch_pipedrive_move_out_deals = original_deals
        departing_tenants_cleaning.fetch_pipedrive_notes = original_notes


class TestCleaningClassification(unittest.TestCase):
    def assert_pref(self, note, expected):
        preference, evidence = classify_cleaning_preference([note])
        self.assertEqual(
            preference, expected,
            f"\nnote:     {note!r}\nexpected: {expected}\ngot:      {preference}\nevidence: {evidence!r}",
        )

    def test_gravity_arranges(self):
        for note in [
            "End of tenancy cleaning scheduled.",
            "End of tenancy cleaning requested",
            "NOT EXTENDING. REQUESTED EOT CLEANING.",
            "NOT EXTENDING Requested end of tenancy cleaning.",
            "NOT EXTENDING. For Ops: He opted for end of tenancy cleaning.",
            "she won't extend her stay - she is happy to pay for the end-of-tenancy cleaning",
        ]:
            with self.subTest(note=note):
                self.assert_pref(note, PREF_GRAVITY)

    def test_tenant_does_it_themselves(self):
        for note in [
            "EOT Cleaning: will be doing the end of tenancy cleaning herself.",
            "Activity raised on PMS. EOT cleaning NOT requested. Tenant will take care of it.",
        ]:
            with self.subTest(note=note):
                self.assert_pref(note, PREF_SELF)

    def test_explicit_no_beats_the_word_cleaning(self):
        """The trap case: a 'not requested' sentence still contains 'cleaning'."""
        self.assert_pref("EOT cleaning NOT requested.", PREF_SELF)
        self.assert_pref("EOT cleaning: no.", PREF_SELF)

    def test_question_or_todo_is_not_an_answer(self):
        """Mentions cleaning, records no tenant decision — must not count as one."""
        for note in [
            "to check the if he wants gravity to arrange his end of tenancy cleaning, "
            "and if not to advise him accordingly.",
            "could you please let us know how you plan on arranging your end of tenancy cleaning?",
            "Awaiting reply on end of tenancy cleaning.",
            "EOT cleaning TBC",
        ]:
            with self.subTest(note=note):
                self.assert_pref(note, PREF_AMBIGUOUS)

    def test_notes_without_cleaning_are_not_recorded(self):
        for note in [
            "Not extending.",
            "Moving home.",
            "Blacklisted by community.",
            "Extension check-in sent on 18 August 2026 to Ankita Nareda regarding 9 West Hampstead.",
        ]:
            with self.subTest(note=note):
                self.assert_pref(note, PREF_NONE)

    def test_later_note_supersedes_earlier(self):
        """Notes arrive oldest-first; a change of mind must win."""
        preference, _ = classify_cleaning_preference([
            "EOT cleaning NOT requested. Tenant will take care of it.",
            "Update: end of tenancy cleaning requested after all.",
        ])
        self.assertEqual(preference, PREF_GRAVITY)

    def test_evidence_is_returned_for_audit(self):
        preference, evidence = classify_cleaning_preference([
            "Wants to move in together with her sister who will be studying at UCL.",
            "EOT Cleaning: will be doing the end of tenancy cleaning herself.",
        ])
        self.assertEqual(preference, PREF_SELF)
        self.assertIn("herself", evidence)

    def test_html_notes_are_stripped(self):
        note = "Activity raised on PMS.&nbsp;<br>EOT cleaning NOT requested.&nbsp;<br>Tenant will take care of it."
        self.assert_pref(note, PREF_SELF)

    def test_strip_html(self):
        self.assertEqual(
            _strip_html("<b>Summary</b><br>Line one.&nbsp;<br>Line two."),
            "Summary Line one. Line two.",
        )


class TestMatchingKeys(unittest.TestCase):
    def test_unit_forms_converge(self):
        """PMS 'unitName' and the Pipedrive unit field share a format."""
        self.assertEqual(_normalise_unit("81 West Court"), _normalise_unit("81 West Court"))
        self.assertEqual(_normalise_unit("004 Royal Heights"), _normalise_unit("4 Royal Heights"))
        self.assertEqual(_normalise_unit("202 The Weymouth"), _normalise_unit("202 the weymouth"))

    def test_distinct_units_do_not_collide(self):
        self.assertNotEqual(_normalise_unit("81 West Court"), _normalise_unit("18 West Court"))
        self.assertNotEqual(_normalise_unit("202 Royal Heights"), _normalise_unit("202 The Weymouth"))

    def test_name_normalisation(self):
        self.assertEqual(_normalise_name("Jiayi (Angela) Li"), _normalise_name("Jiayi Angela Li"))
        self.assertEqual(_normalise_name("Erik Matthias Hündling"), "erikmatthiashndling")
        self.assertNotEqual(_normalise_name("Holly Summers"), _normalise_name("Holly Sanders"))


class TestRenewalDetection(unittest.TestCase):
    """
    The rule that stops cleaners being sent to occupied flats.

    A renewal is a NEW room stay starting the day the old one ends, so a query
    for "stays ending soon" returns renewing tenants. On the 2026-08-27 live run
    15 of 54 matching stays were renewals.
    """

    def _departures(self, stays, contact=CONTACT):
        return build_departures({contact["id"]: stays}, {contact["id"]: contact}, TODAY, HORIZON)

    def test_genuine_departure_is_reported(self):
        """Sokaina Alrihani — one stay ending 12 Sep, nothing after it."""
        rows = self._departures([stay("2025-09-12", "2026-09-12")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["end_date"], "2026-09-12")
        self.assertEqual(rows[0]["email"], "sokainayassine@hotmail.com")
        self.assertTrue(rows[0]["is_tenant"])

    def test_renewal_is_not_a_departure(self):
        """Ashna Jose — stay ends 30 Aug but a new year is already booked."""
        rows = self._departures([
            stay("2026-03-07", "2026-08-30", stay_id=1),
            stay("2026-08-30", "2027-08-30", status="CONFIRMED", stay_id=2),
        ])
        self.assertEqual(rows, [])

    def test_short_extension_moves_the_departure_date(self):
        """Nooredeen Awwad — extended from 2 Sep to 16 Sep; still leaving, later."""
        rows = self._departures([
            stay("2026-08-02", "2026-09-02", stay_id=1),
            stay("2026-09-02", "2026-09-16", status="CONFIRMED", stay_id=2),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["end_date"], "2026-09-16")

    def test_extension_past_the_window_is_not_a_departure(self):
        """Eduofon Japhet — extended to 30 Sep, which is outside a 30-day window."""
        rows = self._departures([
            stay("2026-06-05", "2026-09-01", stay_id=1),
            stay("2026-09-01", "2026-09-30", status="CONFIRMED", stay_id=2),
        ])
        self.assertEqual(rows, [])

    def test_cancelled_stay_does_not_mask_a_departure(self):
        """A cancelled renewal must not make a real departure disappear."""
        stays = [stay("2025-09-12", "2026-09-12", stay_id=1)]
        rows = build_departures({7: stays}, {7: CONTACT}, TODAY, HORIZON)
        self.assertEqual(len(rows), 1)

    def test_already_departed_is_excluded(self):
        self.assertEqual(self._departures([stay("2025-01-01", "2026-08-01")]), [])

    def test_sandbox_building_is_excluded(self):
        rows = self._departures([stay("2025-09-12", "2026-09-12", building="Gravity Test")])
        self.assertEqual(rows, [])


class TestDealMatching(unittest.TestCase):
    """
    Units are re-let, so they are not identity.

    On the live 2026-08-27 data, matching "46 West Court" by unit returned a deal
    belonging to a tenant who had already moved out — whose cleaning preference
    would then have been attributed to the current occupant. Matching is by name,
    with the unit used only to disambiguate between one person's several deals.
    """

    def test_previous_occupant_of_the_same_unit_is_not_matched(self):
        deals = [{"id": 14524, "title": "Saif Ali Kafeel - WC 46 - (Ext 1)",
                  "update_time": "2026-01-01T00:00:00Z",
                  "custom_fields": {_UNIT_KEY: {"label": "46 West Court"}}}]
        rows = _match(deals, name="Alejandro Estremadoyro", unit="46 West Court")
        self.assertIsNone(rows[0]["deal"])
        self.assertEqual(rows[0]["preference"], PREF_NONE)

    def test_matches_the_same_person(self):
        deals = [{"id": 17958, "title": "Sokaina Alrihani - WC 98 - (Ext.1)",
                  "update_time": "2026-08-18T00:00:00Z",
                  "custom_fields": {_UNIT_KEY: {"label": "98 West Court"}}}]
        rows = _match(deals, name="Sokaina Alrihani", unit="98 West Court")
        self.assertEqual(rows[0]["deal"]["id"], 17958)
        self.assertEqual(rows[0]["matched_on"], "name+unit")

    def test_unit_disambiguates_one_persons_several_deals(self):
        deals = [
            {"id": 1, "title": "Corey Donohue - RH 100 - (Ext.1)",
             "update_time": "2026-08-01T00:00:00Z",
             "custom_fields": {_UNIT_KEY: {"label": "100 Royal Heights"}}},
            {"id": 2, "title": "Corey Donohue - RH 007 - (Ext.2)",
             "update_time": "2026-07-01T00:00:00Z",
             "custom_fields": {_UNIT_KEY: {"label": "007 Royal Heights"}}},
        ]
        rows = _match(deals, name="Corey Donohue", unit="007 Royal Heights")
        self.assertEqual(rows[0]["deal"]["id"], 2)


class TestTenancyLength(unittest.TestCase):
    def test_back_to_back_stays_count_as_one_tenancy(self):
        """A four-year resident renewing annually is not a one-year resident."""
        stays = [
            stay("2023-09-01", "2024-09-01", stay_id=1),
            stay("2024-09-01", "2025-09-01", stay_id=2),
            stay("2025-09-01", "2026-09-12", stay_id=3),
        ]
        start = contiguous_tenancy_start(stays, dt.date(2026, 9, 12))
        self.assertEqual(start, dt.date(2023, 9, 1))

    def test_a_gap_breaks_the_tenancy(self):
        """An unrelated stay years earlier must not be stitched on."""
        stays = [
            stay("2020-01-01", "2020-06-01", stay_id=1),
            stay("2025-09-12", "2026-09-12", stay_id=2),
        ]
        start = contiguous_tenancy_start(stays, dt.date(2026, 9, 12))
        self.assertEqual(start, dt.date(2025, 9, 12))

    def test_short_let_guest_is_not_a_tenant(self):
        """A 2-night Weymouth booking gets a turnover clean, not an EOT clean."""
        rows = build_departures(
            {9: [stay("2026-09-16", "2026-09-18", status="CONFIRMED", unit="202 The Weymouth")]},
            {9: {"id": 9, "firstName": "Mohammad", "lastName": "Alaiban"}},
            TODAY, HORIZON,
        )
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["is_tenant"])
        self.assertEqual(rows[0]["nights"], 2)

    def test_tenancy_threshold_boundary(self):
        start = TODAY - dt.timedelta(days=TENANCY_MIN_NIGHTS)
        rows = build_departures(
            {9: [stay(start.isoformat(), TODAY.isoformat())]},
            {9: CONTACT}, TODAY, HORIZON,
        )
        self.assertTrue(rows[0]["is_tenant"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
