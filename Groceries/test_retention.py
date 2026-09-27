"""
Tests for retention.py's window policy (#65) - stdlib only, no database.

What is covered here is the part of retention whose failure mode is *deleting
price history nobody meant to delete*: how the configured window is
interpreted, and the guarantee that a window we can't interpret produces no
DELETE statement at all.

What is deliberately not covered here: the SQL semantics of the DELETE itself
(cutoff anchored to `max(datetime)`, NULL-safe on an empty table). Those need a
real Postgres, and CONTRIBUTING §7 keeps the required tier database-free, so
they were verified by hand against a throwaway `postgres:16` container instead
- the commands and their output are in the PR description. A mocked cursor
would only assert that we wrote the string we wrote the string.

Run with:

    pytest Groceries/test_retention.py -v
"""

import os
import unittest
from unittest import mock

import retention


class RecordingCursor:
    """Stand-in for a psycopg2 cursor that records what it was asked to run.

    This is a call recorder, not a data fixture - the thing under test is
    *whether* prune() reaches SQL at all, and with which window. `rowcount` stays
    0, which is what a real cursor reports for a DELETE that removed nothing.

    `fetchone` exists because prune() asks the database one question before
    deleting anything: is the newest row dated in the future? `future_dated`
    drives the answer so a test can exercise that guard without a database. It
    defaults to False - a sane clock - which is what every test not about the
    guard wants.
    """

    def __init__(self, future_dated=False):
        self.executed = []
        self.rowcount = 0
        self.future_dated = future_dated

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (self.future_dated,)


class RetentionDaysTests(unittest.TestCase):
    def test_unset_or_blank_falls_back_to_the_documented_default(self):
        for raw in (None, "", "   "):
            self.assertEqual(retention.retention_days(raw), retention.DEFAULT_RETENTION_DAYS)

    def test_parses_a_plain_day_count(self):
        self.assertEqual(retention.retention_days("30"), 30)
        self.assertEqual(retention.retention_days("1"), 1)

    def test_tolerates_the_whitespace_a_hand_edited_env_file_picks_up(self):
        # .env is edited by hand and quoted by shell habit; a value that is
        # really 14 days must not be treated as unparseable.
        self.assertEqual(retention.retention_days(" 14 "), 14)
        self.assertEqual(retention.retention_days("14\n"), 14)

    def test_non_positive_parses_as_a_value_rather_than_an_error(self):
        # 0 and negatives are the documented "keep everything" switch, so they
        # have to come back as numbers. Returning None here instead would print
        # a config-error warning at someone who deliberately disabled pruning.
        self.assertEqual(retention.retention_days("0"), 0)
        self.assertEqual(retention.retention_days("-1"), -1)

    def test_unparseable_returns_none_rather_than_the_default(self):
        # The regression this whole module exists to prevent: a typo falling
        # back to DEFAULT_RETENTION_DAYS would delete everything older than 90
        # days because someone typed `90days`. "No answer" has to mean "delete
        # nothing", never "delete on a guess".
        for raw in ("90days", "abc", "ninety", "1e3"):
            self.assertIsNone(retention.retention_days(raw), f"{raw!r} should not parse")

    def test_fractional_days_are_not_silently_truncated(self):
        # int(float("0.5")) would be 0 - and while 0 happens to mean "keep
        # everything" today, a truncating parser is one edit away from turning
        # "0.5" into a window nobody asked for. Refusing the value is cheaper
        # than reasoning about every future meaning of 0.
        self.assertIsNone(retention.retention_days("0.5"))
        self.assertIsNone(retention.retention_days("90.0"))


class PruneGuardsTests(unittest.TestCase):
    def prune_with_env(self, value, future_dated=False):
        """Call retention.prune() with PRICE_HISTORY_RETENTION_DAYS set to
        `value`, or removed from the environment entirely when value is None.
        `future_dated` is what the cursor reports for the clock guard."""
        env = {k: v for k, v in os.environ.items() if k != retention.RETENTION_DAYS_ENV}
        if value is not None:
            env[retention.RETENTION_DAYS_ENV] = value
        cursor = RecordingCursor(future_dated=future_dated)
        with mock.patch.dict(os.environ, env, clear=True):
            deleted = retention.prune(cursor)
        return deleted, cursor

    def test_unparseable_window_issues_no_sql_at_all(self):
        deleted, cursor = self.prune_with_env("90days")
        self.assertEqual(cursor.executed, [])
        self.assertEqual(deleted, 0)

    def test_disabled_window_issues_no_sql_at_all(self):
        for value in ("0", "-1"):
            deleted, cursor = self.prune_with_env(value)
            self.assertEqual(cursor.executed, [], f"{value} should disable pruning, not run it")
            self.assertEqual(deleted, 0)

    def test_unset_env_prunes_with_the_default_window(self):
        _, cursor = self.prune_with_env(None)
        # The default is asserted as a LITERAL, not against
        # retention.DEFAULT_RETENTION_DAYS. Comparing the constant to itself
        # passes no matter what the constant is set to, so it could never notice
        # the documented 30-day policy silently becoming 90 - and 30 is the
        # judgment call this whole module most needs pinned down, since raising
        # it later only keeps more data from that point forward.
        self.assertEqual(retention.DEFAULT_RETENTION_DAYS, 30)
        self.assertEqual(len(cursor.executed), 2, "expected the clock guard then the DELETE")
        self.assertEqual(cursor.executed[1][1], (30,))

    def test_configured_window_is_the_one_passed_to_the_delete(self):
        _, cursor = self.prune_with_env("45")
        self.assertEqual(cursor.executed[1][1], (45,))

    def test_cutoff_is_derived_from_the_table_rather_than_a_clock(self):
        # The invariant behind anchoring on max(datetime): the scraper writes
        # naive local timestamps while now()::timestamp is rendered in the
        # Postgres container's TimeZone, and those two clocks need not agree.
        # A wall-clock cutoff would quietly move the window by the offset.
        #
        # Asserted against the DELETE specifically (executed[1]), not the whole
        # statement list: the clock guard at executed[0] legitimately *does* use
        # now(), because it is asking "is the data ahead of the wall clock" -
        # which is a different question from "which rows are old enough to drop".
        delete_sql = self.prune_with_env("30")[1].executed[1][0].lower()
        self.assertIn("delete from grocery_prices", delete_sql)
        self.assertIn("max(datetime)", delete_sql)
        self.assertNotIn("now()", delete_sql)
        self.assertNotIn("current_timestamp", delete_sql)

    def test_prune_touches_grocery_prices_and_nothing_else(self):
        # #65's warning, kept as a net: prices are re-scrapable, recipes and
        # cooked-meal history are not, so this is the only DELETE the repo
        # contains and it must stay aimed at one table.
        sql = self.prune_with_env("30")[1].executed[1][0]
        self.assertEqual(sql.upper().count("DELETE"), 1)
        self.assertIn("grocery_prices", sql)

    def test_a_future_dated_row_stops_the_prune_entirely(self):
        """The guard against deleting the whole table.

        The cutoff is `max(datetime) - window` with no ceiling, so one
        forward-dated row drags the cutoff into the future and the DELETE matches
        everything. Measured against a real postgres:16 before this guard
        existed: 30 rows of good daily history plus one row dated +400 days went
        31 -> 1, logged as an ordinary success. Reachable without any bug here,
        because the scraper stamps rows with the *host* clock - WSL/Hyper-V skew
        after sleep, a VM snapshot restore, a dead CMOS battery.

        Asserted as "the DELETE is never issued at all" rather than "fewer rows
        deleted", because a guard that runs the DELETE with a safer cutoff would
        still be guessing; refusing is the behaviour this module promises
        everywhere else.
        """
        deleted, cursor = self.prune_with_env("30", future_dated=True)
        self.assertEqual(deleted, 0)
        self.assertEqual(len(cursor.executed), 1, "only the guard query should have run")
        self.assertNotIn("DELETE", cursor.executed[0][0].upper())

    def test_a_sane_clock_still_prunes(self):
        # The other half of the guard: it must not become a way for pruning to
        # silently stop happening forever, which would reintroduce the unbounded
        # growth #65 exists to fix.
        deleted, cursor = self.prune_with_env("30", future_dated=False)
        self.assertEqual(len(cursor.executed), 2)
        self.assertIn("DELETE", cursor.executed[1][0].upper())
        # prune() returns the cursor's rowcount, which this recorder leaves at 0;
        # asserting the call shape is the point, and the real row counts are
        # covered by the collector-level checks in the PR description.
        self.assertEqual(deleted, cursor.rowcount)


if __name__ == "__main__":
    unittest.main()
