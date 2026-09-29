"""Tests for moving USDA lookups off /planner/ingredients' request path (#64).

No database and no network: the cache read is a fake cursor, and
_usda_grams_per_unit is patched. What is guarded here is the latency contract
(a cache miss never calls USDA on the request thread), that the background
worker banks each result with its own commit, and that a failed lookup settles
to "no estimate" rather than "estimating" forever.
"""

import threading
import time
import unittest
from unittest import mock

import app


class _MissCursor:
    """ingredient_conversions has nothing cached."""

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return None


class _HitCursor(_MissCursor):
    def fetchone(self):
        return {"grams_per_unit": 125.0}


class _RecordingConn:
    def __init__(self):
        self.executed, self.commits = [], 0

    def cursor(self, **kwargs):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.executed.append((sql, params))

        return Cur()

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _reset_state():
    with app._usda_lock:
        app._usda_pending.clear()
        app._usda_misses.clear()
    while not app._usda_queue.empty():
        app._usda_queue.get_nowait()


def _wait_until_settled(timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app._usda_lock:
            if not app._usda_pending:
                return
        time.sleep(0.01)
    raise AssertionError("background lookup never settled")


class BackgroundLookupTests(unittest.TestCase):
    def setUp(self):
        _reset_state()
        env = mock.patch.dict("os.environ", {"USDA_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(_reset_state)

    def test_cache_miss_returns_immediately_even_when_usda_is_slow(self):
        release = threading.Event()

        def slow_usda(name, words):
            release.wait(5)
            return None

        conn = _RecordingConn()
        with mock.patch.object(app, "_usda_grams_per_unit", slow_usda), \
                mock.patch.object(app, "get_connection", lambda: conn):
            start = time.monotonic()
            result = app.resolve_purchase_amount(_MissCursor(), "flour", "2", "cup")
            elapsed = time.monotonic() - start
            release.set()
            _wait_until_settled()
        self.assertEqual(result, "pending")
        self.assertLess(elapsed, 0.5)

    def test_worker_commits_the_result_on_its_own_connection(self):
        conn = _RecordingConn()
        with mock.patch.object(app, "_usda_grams_per_unit", lambda n, w: 125.0), \
                mock.patch.object(app, "get_connection", lambda: conn):
            app.resolve_purchase_amount(_MissCursor(), "flour", "2", "cup")
            _wait_until_settled()
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.executed[0][1], ("flour", "cup", 125.0))

    def test_failed_lookup_settles_to_no_estimate_and_is_not_requeued(self):
        calls = []

        def no_match(name, words):
            calls.append(name)
            return None

        with mock.patch.object(app, "_usda_grams_per_unit", no_match):
            self.assertEqual(app.resolve_purchase_amount(_MissCursor(), "saffron", "1", "tsp"), "pending")
            _wait_until_settled()
            self.assertIsNone(app.resolve_purchase_amount(_MissCursor(), "saffron", "1", "tsp"))
            self.assertIsNone(app.resolve_purchase_amount(_MissCursor(), "saffron", "1", "tsp"))
        self.assertEqual(calls, ["saffron"])

    def test_worker_survives_an_exception(self):
        def boom(name, words):
            raise RuntimeError("unexpected")

        with mock.patch.object(app, "_usda_grams_per_unit", boom), \
                self.assertLogs(app.app.logger, "ERROR"):
            app.resolve_purchase_amount(_MissCursor(), "flour", "2", "cup")
            _wait_until_settled()
        # The same thread must still serve the next lookup.
        with mock.patch.object(app, "_usda_grams_per_unit", lambda n, w: None):
            self.assertEqual(app.resolve_purchase_amount(_MissCursor(), "sugar", "1", "cup"), "pending")
            _wait_until_settled()

    def test_no_api_key_is_no_estimate_not_pending(self):
        with mock.patch.dict("os.environ", {"USDA_API_KEY": ""}):
            self.assertIsNone(app.resolve_purchase_amount(_MissCursor(), "flour", "2", "cup"))
        self.assertTrue(app._usda_queue.empty())

    def test_cache_hit_is_unchanged(self):
        self.assertEqual(app.resolve_purchase_amount(_HitCursor(), "flour", "2", "cup"), (0.55, "lb"))

    def test_apply_counts_pending(self):
        combined = [
            {"name": "flour", "amount": "2", "unit": "cup"},
            {"name": "salt", "amount": "1", "unit": "lb"},
        ]
        with mock.patch.object(app, "_usda_grams_per_unit", lambda n, w: None):
            pending = app.apply_purchase_estimates(_MissCursor(), combined)
            _wait_until_settled()
        self.assertEqual(pending, 1)
        self.assertTrue(combined[0]["purchase_pending"])
        self.assertEqual((combined[1]["purchase_amount"], combined[1]["purchase_unit"]), (1.0, "lb"))


if __name__ == "__main__":
    unittest.main()
