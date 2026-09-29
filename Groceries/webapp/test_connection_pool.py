"""Tests for the pooled get_connection() (#67) - fakes only, no database.

The property that matters most is the one #67 calls subtle: a pooled
connection is reused, so an uncommitted transaction must be rolled back when
it is returned, or it leaks into the next request. The real-Postgres check
(transaction leak across requests, pool size under load, a db restart) is in
the #67 commit.
"""

import types
import unittest
from unittest import mock

import psycopg2
import psycopg2.extensions
import psycopg2.pool

import app


class _Conn:
    def __init__(self, dead=False):
        self.closed = 0
        self.dead = dead
        self.rollbacks = 0

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql):
                if conn.dead:
                    conn.closed = 2
                    raise psycopg2.OperationalError("server closed the connection")

        return Cur()

    def rollback(self):
        if self.closed:
            raise psycopg2.InterfaceError("connection already closed")
        self.rollbacks += 1


class _Pool:
    def __init__(self, conns, exhausted=False):
        self.conns, self.exhausted = list(conns), exhausted
        self.returned = []

    def getconn(self):
        if self.exhausted:
            raise psycopg2.pool.PoolError("connection pool exhausted")
        return self.conns.pop(0)

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))


class PooledConnectionTests(unittest.TestCase):
    def _with_pool(self, pool):
        return mock.patch.object(app, "_get_pool", lambda: pool)

    def test_close_rolls_back_and_returns_to_the_pool(self):
        raw = _Conn()
        pool = _Pool([raw])
        with self._with_pool(pool):
            conn = app.get_connection()
            rollbacks_after_probe = raw.rollbacks
            conn.close()
        self.assertEqual(raw.rollbacks, rollbacks_after_probe + 1)
        self.assertEqual(pool.returned, [(raw, False)])

    def test_close_twice_returns_once(self):
        pool = _Pool([_Conn()])
        with self._with_pool(pool):
            conn = app.get_connection()
            conn.close()
            conn.close()
        self.assertEqual(len(pool.returned), 1)

    def test_a_connection_broken_mid_request_is_discarded_not_reused(self):
        raw = _Conn()
        pool = _Pool([raw])
        with self._with_pool(pool):
            conn = app.get_connection()
            raw.closed = 2  # server went away during the request
            conn.close()
        self.assertEqual(pool.returned, [(raw, True)])

    def test_a_dead_pooled_connection_is_replaced_on_checkout(self):
        dead, fresh = _Conn(dead=True), _Conn()
        pool = _Pool([dead, fresh])
        with self._with_pool(pool):
            conn = app.get_connection()
        self.assertIs(conn._conn, fresh)
        self.assertEqual(pool.returned, [(dead, True)])

    def test_every_idle_connection_dead_falls_back_to_direct(self):
        direct = object()
        pool = _Pool([_Conn(dead=True) for _ in range(app.POOL_MAX + 1)])
        with self._with_pool(pool), mock.patch.object(psycopg2, "connect", return_value=direct):
            self.assertIs(app.get_connection(), direct)
        self.assertTrue(all(close for _, close in pool.returned))

    def test_real_pool_reuses_up_to_pool_min_connections(self):
        # Review of #67: psycopg2 keeps a returned connection only while fewer
        # than minconn are idle. Use the REAL pool class with connect mocked, so
        # this fails if POOL_MIN drops back to a value that forces reconnects.
        made = []

        def fake_connect(*a, **kw):
            c = _Conn()
            c.autocommit = False
            c.info = types.SimpleNamespace(transaction_status=psycopg2.extensions.TRANSACTION_STATUS_IDLE)
            made.append(c)
            return c

        with mock.patch("psycopg2.pool.psycopg2.connect", fake_connect):
            pool = psycopg2.pool.ThreadedConnectionPool(app.POOL_MIN, app.POOL_MAX)
            opened_at_start = len(made)
            for _ in range(20):  # a page render: route + nav, held at once
                a, b = pool.getconn(), pool.getconn()
                pool.putconn(b)
                pool.putconn(a)
        self.assertEqual(len(made), opened_at_start)
        self.assertGreaterEqual(app.POOL_MIN, 2)

    def test_exhausted_pool_falls_back_to_a_direct_connection(self):
        direct = object()
        with self._with_pool(_Pool([], exhausted=True)), \
                mock.patch.object(psycopg2, "connect", return_value=direct):
            self.assertIs(app.get_connection(), direct)

    def test_timeout_callers_bypass_the_pool(self):
        direct = object()
        with self._with_pool(_Pool([], exhausted=True)), \
                mock.patch.object(psycopg2, "connect", return_value=direct) as connect:
            self.assertIs(app.get_connection(connect_timeout=5), direct)
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 5)


if __name__ == "__main__":
    unittest.main()
