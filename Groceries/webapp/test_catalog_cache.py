"""matching.cached_catalog (#57) - fakes only, no database.

The cache is keyed on grocery_prices' insert/update/delete counters from
pg_stat_user_tables. What must hold: same counters -> no rebuild; any counter
change -> rebuild; no stats row -> never cached; and a hard age ceiling.
"""

import unittest
from unittest import mock

import matching


class _Cur:
    def __init__(self, counters):
        self.counters = counters

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self.counters


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        matching._catalog_cache.update(key=None, built=0.0, catalog=None)
        self.builds = []
        patcher = mock.patch.object(matching, "load_catalog",
                                    lambda cur: self.builds.append(1) or [f"build{len(self.builds)}"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(matching._catalog_cache.update, key=None, built=0.0, catalog=None)

    def test_unchanged_counters_reuse_the_catalog(self):
        cur = _Cur((100, 0, 0))
        first = matching.cached_catalog(cur)
        second = matching.cached_catalog(cur)
        self.assertIs(first, second)
        self.assertEqual(len(self.builds), 1)

    def test_a_scrape_or_prune_rebuilds(self):
        matching.cached_catalog(_Cur((100, 0, 0)))
        matching.cached_catalog(_Cur((23184, 0, 0)))   # scrape inserted rows
        matching.cached_catalog(_Cur((23184, 0, 500)))  # retention pruned
        self.assertEqual(len(self.builds), 3)

    def test_dict_rows_work_too(self):
        # Callers pass RealDictCursor cursors.
        cur = _Cur({"n_tup_ins": 1, "n_tup_upd": 0, "n_tup_del": 0})
        matching.cached_catalog(cur)
        matching.cached_catalog(cur)
        self.assertEqual(len(self.builds), 1)

    def test_no_stats_row_is_never_cached(self):
        cur = _Cur(None)
        matching.cached_catalog(cur)
        matching.cached_catalog(cur)
        self.assertEqual(len(self.builds), 2)

    def test_age_ceiling_forces_a_rebuild(self):
        cur = _Cur((1, 0, 0))
        with mock.patch.object(matching.time, "monotonic", return_value=1000.0):
            matching.cached_catalog(cur)
        with mock.patch.object(matching.time, "monotonic", return_value=1000.0 + matching.CATALOG_MAX_AGE + 1):
            matching.cached_catalog(cur)
        self.assertEqual(len(self.builds), 2)


if __name__ == "__main__":
    unittest.main()
