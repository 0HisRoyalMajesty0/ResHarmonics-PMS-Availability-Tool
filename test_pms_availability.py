"""
Mock-based tests for pms_availability.py — no network calls, no real credentials.

These verify the classification logic and HTML rendering work correctly given
known inputs. They do NOT verify the live API integration (endpoint path,
field names) — that still needs to be checked against the real Res:Harmonics
API (see "STILL TO CONFIRM" in pms_availability.py's docstring).

Run with: python3 test_pms_availability.py
"""

import datetime as dt
import unittest
import pms_availability as m


class TestClassificationLogic(unittest.TestCase):
    def setUp(self):
        # Patch fetch_availability per-test so no real HTTP calls happen.
        self._original_fetch = m.fetch_availability

    def tearDown(self):
        m.fetch_availability = self._original_fetch

    def _run(self, fake_fetch, partners):
        m.fetch_availability = fake_fetch
        return m.build_report(partners, "2026-08-01", "2026-08-08")

    def test_fully_available(self):
        def fake_fetch(property_id, start_date, end_date):
            return {
                "openNightsCount": 7,
                "openDateRanges": ["01-07 Aug"],
                "lastSyncedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        results = self._run(fake_fetch, [{"partner_name": "A", "property_id": "1"}])
        self.assertEqual(results[0].status, m.STATUS_AVAILABLE)

    def test_partially_available(self):
        def fake_fetch(property_id, start_date, end_date):
            return {
                "openNightsCount": 3,
                "openDateRanges": ["01-03 Aug"],
                "lastSyncedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        results = self._run(fake_fetch, [{"partner_name": "B", "property_id": "2"}])
        self.assertEqual(results[0].status, m.STATUS_PARTIAL)

    def test_unavailable(self):
        def fake_fetch(property_id, start_date, end_date):
            return {
                "openNightsCount": 0,
                "openDateRanges": [],
                "lastSyncedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        results = self._run(fake_fetch, [{"partner_name": "C", "property_id": "3"}])
        self.assertEqual(results[0].status, m.STATUS_UNAVAILABLE)

    def test_no_response_from_api(self):
        def fake_fetch(property_id, start_date, end_date):
            return None

        results = self._run(fake_fetch, [{"partner_name": "D", "property_id": "4"}])
        self.assertEqual(results[0].status, m.STATUS_AWAITING)

    def test_stale_data_flagged_as_awaiting(self):
        def fake_fetch(property_id, start_date, end_date):
            return {
                "openNightsCount": 7,
                "openDateRanges": ["01-07 Aug"],
                "lastSyncedAt": (
                    dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
                ).isoformat(),
            }

        results = self._run(fake_fetch, [{"partner_name": "E", "property_id": "5"}])
        self.assertEqual(results[0].status, m.STATUS_AWAITING)

    def test_awaiting_and_partial_sort_first(self):
        def fake_fetch(property_id, start_date, end_date):
            responses = {
                "avail": {
                    "openNightsCount": 7,
                    "openDateRanges": ["01-07 Aug"],
                    "lastSyncedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
                "none": None,
            }
            return responses.get(property_id, responses["avail"])

        partners = [
            {"partner_name": "Available Partner", "property_id": "avail"},
            {"partner_name": "No Response Partner", "property_id": "none"},
        ]
        results = self._run(fake_fetch, partners)
        self.assertEqual(results[0].status, m.STATUS_AWAITING)

    def test_html_renders_without_error(self):
        def fake_fetch(property_id, start_date, end_date):
            return {
                "openNightsCount": 7,
                "openDateRanges": ["01-07 Aug"],
                "lastSyncedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

        results = self._run(fake_fetch, [{"partner_name": "A", "property_id": "1"}])
        html = m.render_html(results, "2026-08-01", "2026-08-08")
        self.assertIn("Partner Availability", html)
        self.assertIn("A", html)


if __name__ == "__main__":
    unittest.main()
