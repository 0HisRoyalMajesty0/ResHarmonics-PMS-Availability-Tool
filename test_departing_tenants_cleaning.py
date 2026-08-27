#!/usr/bin/env python3
"""
Tests for departing_tenants_cleaning.py.

Every note string below is real text pulled from the Gravity Pipedrive account
on 2026-08-27, not invented fixtures — the classifier's whole job is coping with
how the ops team actually writes these notes, so testing it against tidied-up
prose would prove nothing.

Network-dependent code (PMS fetch, Pipedrive fetch) is not covered here; the
extraction helpers it depends on are tested against representative record
shapes so a schema surprise fails loudly rather than silently.
"""

import unittest

from departing_tenants_cleaning import (
    PREF_AMBIGUOUS,
    PREF_GRAVITY,
    PREF_NONE,
    PREF_SELF,
    _normalise_name,
    _normalise_unit,
    _strip_html,
    classify_cleaning_preference,
    extract_building,
    extract_date,
    extract_email,
    extract_name,
    extract_unit,
)


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


class TestFieldExtraction(unittest.TestCase):
    """The tenancy record schema is unconfirmed, so extraction must be tolerant."""

    def test_extracts_from_nested_client_object(self):
        record = {
            "id": 1,
            "endDate": "2026-09-12",
            "unit": {"unitName": "98 West Court", "buildingName": "Gravity Hounslow Central"},
            "client": {"firstName": "Sokaina", "lastName": "Alrihani",
                       "emailAddress": "sokaina@example.com"},
        }
        self.assertEqual(extract_name(record), "Sokaina Alrihani")
        self.assertEqual(extract_email(record), "sokaina@example.com")
        self.assertEqual(extract_unit(record), "98 West Court")
        self.assertEqual(extract_building(record), "Gravity Hounslow Central")
        self.assertEqual(extract_date(record, "enddate").isoformat(), "2026-09-12")

    def test_handles_flat_snake_case_schema(self):
        record = {
            "end_date": "2026-09-01T00:00:00Z",
            "customer_full_name": "Holly Summers",
            "customer_email": "holly@example.com",
        }
        self.assertEqual(extract_email(record), "holly@example.com")
        self.assertEqual(extract_date(record, "end_date").isoformat(), "2026-09-01")

    def test_prefers_a_field_named_email(self):
        record = {"reference": "not.an@email.ref", "contact": {"email": "real@example.com"}}
        self.assertEqual(extract_email(record), "real@example.com")

    def test_missing_fields_return_none(self):
        self.assertIsNone(extract_email({"id": 1}))
        self.assertIsNone(extract_name({"id": 1}))
        self.assertIsNone(extract_date({"id": 1}, "enddate"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
