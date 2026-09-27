"""Tests for app.py's /healthz probe (#61, #71).

Small, but it's the first test coverage app.py has had - previously all 1,800+
lines of routes and domain logic were untested (see CONTRIBUTING §7, which
names that as the repo's coverage gap). Starting with /healthz is deliberate:
it is a pure request/response boundary with no domain math, so it can be tested
without a database, and it is the endpoint a container healthcheck and any
future reverse proxy will depend on for restart decisions. A probe that lies in
either direction is costly - a false 200 hides an outage, a false 503 makes
Docker restart a healthy container in a loop.

No database is contacted: get_connection is patched. That keeps this in the
required CI tier (`pytest -k "not Live"`), which must not need a database or
network to pass.
"""

import unittest
from unittest import mock

import psycopg2

import app


class _FakeCursor:
    """Stands in for a psycopg2 cursor on the `SELECT 1` round trip.

    Records *every* statement rather than just the last one, so a test can
    assert what the probe did and did not execute. Storing only the latest
    would silently pass even if the route grew a CREATE TABLE before its
    SELECT - which is the exact regression worth catching here.
    """

    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return (1,)


class _FakeConnection:
    def __init__(self):
        self.closed = False
        self.cursor_obj = _FakeCursor()

    def cursor(self, **kwargs):
        return self.cursor_obj

    def close(self):
        self.closed = True


class HealthzTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_returns_200_when_database_is_reachable(self):
        with mock.patch.object(app, "get_connection", return_value=_FakeConnection()):
            resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok", "database": "reachable"})

    def test_returns_503_when_database_is_unreachable(self):
        # OperationalError is what a refused connection / unresolvable DB_HOST
        # actually raises, and it is a psycopg2.Error subclass - the handler
        # catches the base class so every psycopg2 failure maps to 503 rather
        # than escaping as an unhandled 500.
        with mock.patch.object(
            app,
            "get_connection",
            side_effect=psycopg2.OperationalError("could not connect to server"),
        ):
            resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {"status": "unhealthy", "database": "unreachable"})

    def test_503_not_500_on_a_query_failure_after_connecting(self):
        # Connecting is not the same as being able to run a query - a database
        # that accepts the connection and then errors (starting up, shutting
        # down, out of connections) must still report unhealthy rather than
        # crashing the probe.
        conn = _FakeConnection()
        conn.cursor_obj.execute = mock.Mock(side_effect=psycopg2.Error("server closed"))
        with mock.patch.object(app, "get_connection", return_value=conn):
            resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 503)

    def test_closes_its_connection_even_when_the_query_fails(self):
        # A healthcheck runs every few seconds forever. Leaking one connection
        # per probe would exhaust Postgres' max_connections in minutes, so the
        # close has to be in a finally rather than on the happy path only.
        conn = _FakeConnection()
        conn.cursor_obj.execute = mock.Mock(side_effect=psycopg2.Error("boom"))
        with mock.patch.object(app, "get_connection", return_value=conn):
            self.client.get("/healthz")
        self.assertTrue(conn.closed)

    def test_opens_exactly_one_connection_and_runs_no_ddl(self):
        # Guards the two properties the route's docstring promises, which are
        # easy to break accidentally by "just rendering a template" or "just
        # calling ensure_profiles_table like the other routes do".
        #
        # Returning JSON means inject_profile_switcher never runs - it is a
        # template context processor, and it opens its own connection and
        # executes CREATE TABLE on every render. So exactly one connection here
        # proves the probe is not paying that cost on every healthcheck tick.
        #
        # The executed-statement list is what actually pins the "no DDL" half:
        # asserting the *whole* list equals ["SELECT 1"] fails if anything else
        # is run, before or after. Asserting only the last statement would not.
        conn = _FakeConnection()
        with mock.patch.object(app, "get_connection", return_value=conn) as gc:
            self.client.get("/healthz")
        self.assertEqual(gc.call_count, 1)
        self.assertEqual(conn.cursor_obj.executed, ["SELECT 1"])


if __name__ == "__main__":
    unittest.main()
