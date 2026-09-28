"""Tests for app.price_freshness_from_catalog (#62).

A pure function over a list of dicts, so it needs no database - which keeps it in
the required CI tier per CONTRIBUTING §7. It is the logic behind the "these prices
are out of date" banner on /list/where-to-buy, i.e. the thing that stops the
household acting on six-week-old numbers with no indication.

Timestamps are built relative to the real current time rather than by mocking
`app.datetime`. Patching the datetime module wholesale is fragile (it is a C type,
and other code in app.py uses it), and it isn't necessary: the function only ever
compares a date against `date.today()`, so relative offsets are deterministic
whatever day the suite runs on.

The cases worth pinning are the ones where a plausible implementation gets it
subtly wrong: taking the *first* row per store instead of the newest, confusing
oldest with newest in the summary, and treating "scraped three hours ago" as one
day old.
"""

import datetime
import unittest

import app


def _row(store, when):
    return {"store": store, "product": "x", "datetime": when}


class PriceFreshnessTests(unittest.TestCase):
    def _dt(self, days_ago, hour=3):
        """A timestamp `days_ago` days before today, at a fixed hour.

        Scrapes run at 03:00, so hour=3 mirrors real data. Fixing the hour keeps
        the date part - which is all the function uses - unambiguous.
        """
        base = datetime.datetime.now() - datetime.timedelta(days=days_ago)
        return base.replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_empty_catalog_is_none_not_a_zero_age(self):
        # None is what lets the template say "no price data yet" rather than
        # "prices are 0 days old", which would be a confident lie.
        self.assertIsNone(app.price_freshness_from_catalog([]))

    def test_catalog_with_no_datetimes_is_none(self):
        # matching.load_catalog only started selecting `datetime` in #62, and
        # hand-built fixtures elsewhere don't include it. A catalog whose rows
        # carry no timestamp must degrade to None rather than crash.
        self.assertIsNone(app.price_freshness_from_catalog([{"store": "Tops", "product": "x"}]))

    def test_takes_the_newest_row_per_store_not_the_first(self):
        catalog = [
            _row("Tops", self._dt(9)),   # older, but first in the list
            _row("Tops", self._dt(1)),   # newest
            _row("Tops", self._dt(5)),
        ]
        result = app.price_freshness_from_catalog(catalog)
        self.assertEqual(len(result["stores"]), 1)
        self.assertEqual(result["stores"][0]["age_days"], 1)

    def test_age_of_a_scrape_from_earlier_today_is_zero(self):
        # (today - today).days == 0. Comparing datetimes rather than dates would
        # make a three-hour-old scrape look one day old.
        result = app.price_freshness_from_catalog([_row("Tops", self._dt(0))])
        self.assertEqual(result["stores"][0]["age_days"], 0)
        self.assertFalse(result["stores"][0]["stale"])

    def test_stale_flags_only_past_the_threshold(self):
        at_threshold = app.price_freshness_from_catalog(
            [_row("Tops", self._dt(app.PRICE_STALE_AFTER_DAYS))]
        )
        past_threshold = app.price_freshness_from_catalog(
            [_row("Tops", self._dt(app.PRICE_STALE_AFTER_DAYS + 1))]
        )
        self.assertFalse(at_threshold["any_stale"], "exactly at the threshold is not stale")
        self.assertTrue(past_threshold["any_stale"])
        self.assertTrue(past_threshold["stores"][0]["stale"])

    def test_one_stale_store_makes_the_whole_comparison_stale(self):
        # The reason this reports per store at all: collector.py scrapes each
        # store independently and one can fail while the others succeed (#24). A
        # fresh Tops must not disguise a six-week-old BJs, and a single global
        # "as of today" would do exactly that.
        catalog = [_row("Tops", self._dt(0)), _row("BJs", self._dt(41))]
        result = app.price_freshness_from_catalog(catalog)
        self.assertTrue(result["any_stale"])
        self.assertEqual(result["oldest_age_days"], 41)
        self.assertEqual(result["newest_age_days"], 0)
        by_store = {s["store"]: s for s in result["stores"]}
        self.assertFalse(by_store["Tops"]["stale"])
        self.assertTrue(by_store["BJs"]["stale"])

    def test_stores_are_sorted_for_a_stable_render(self):
        catalog = [_row("Walmart", self._dt(2)), _row("Aldi", self._dt(1)), _row("Tops", self._dt(3))]
        result = app.price_freshness_from_catalog(catalog)
        self.assertEqual([s["store"] for s in result["stores"]], ["Aldi", "Tops", "Walmart"])

    def test_rows_without_a_store_are_skipped_rather_than_crashing(self):
        catalog = [_row("Tops", self._dt(1)), {"product": "x", "datetime": self._dt(1)}]
        result = app.price_freshness_from_catalog(catalog)
        self.assertEqual([s["store"] for s in result["stores"]], ["Tops"])

    def test_accepts_a_bare_date_as_well_as_a_datetime(self):
        # The helper calls .date() when available and falls back to the value
        # itself, so a fixture or a future caller passing a date still works.
        result = app.price_freshness_from_catalog(
            [_row("Tops", datetime.date.today() - datetime.timedelta(days=3))]
        )
        self.assertEqual(result["stores"][0]["age_days"], 3)


if __name__ == "__main__":
    unittest.main()
