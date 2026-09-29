"""Tests for run_status.py (#63) - stdlib only, no database, no scrapers.

The two failure modes worth guarding: a run verdict that cries wolf every night
(so Walmart's by-design failure, #12, must not make a run PARTIAL) or that hides
a real one; and a staleness check that either never fires on a fresh deploy or
fires while tonight's run is still due.
"""

import unittest
from datetime import datetime, timedelta

import run_status


class RunSummaryTests(unittest.TestCase):
    def test_three_stores_ok_and_walmart_failing_is_ok(self):
        verdict, line = run_status.run_summary([
            ("Aldis", 2173, None), ("Tops", 17408, None), ("BJs", 3121, None),
            ("Walmart", None, RuntimeError("bot wall")),
        ])
        self.assertEqual(verdict, "OK")
        self.assertIn("Walmart FAILED (expected, #12)", line)
        self.assertIn("Tops ok (17408 items)", line)

    def test_unexpected_store_failure_is_partial(self):
        verdict, line = run_status.run_summary([
            ("Aldis", 2173, None), ("Tops", None, TimeoutError("page 40")),
            ("BJs", 3121, None), ("Walmart", None, RuntimeError("bot wall")),
        ])
        self.assertEqual(verdict, "PARTIAL")
        self.assertIn("Tops FAILED: page 40", line)

    def test_nothing_succeeding_is_failed_even_if_only_expected_failures(self):
        verdict, _ = run_status.run_summary([("Walmart", None, RuntimeError("x"))])
        self.assertEqual(verdict, "FAILED")

    def test_long_error_is_truncated(self):
        _, line = run_status.run_summary([("Tops", None, RuntimeError("x" * 5000))])
        self.assertLess(len(line), 300)


class IsStaleTests(unittest.TestCase):
    NOW = datetime(2026, 9, 29, 3, 10)

    def test_empty_table_is_stale(self):
        self.assertTrue(run_status.is_stale(None, self.NOW))

    def test_last_nights_run_is_not_stale_before_tonights_finishes(self):
        # Yesterday's 03:00 run finished 03:25; it's 03:10 today, tonight's run
        # is in progress. Scraping again here would double up.
        self.assertFalse(run_status.is_stale(datetime(2026, 9, 28, 3, 25), self.NOW))

    def test_a_missed_night_is_stale(self):
        # Host was off at 03:00 yesterday: newest data is two days old.
        self.assertTrue(run_status.is_stale(self.NOW - timedelta(days=2), self.NOW))


if __name__ == "__main__":
    unittest.main()
