"""Tests for #68's flash messages and error handler.

Written during review of the #68 PR, which found two routes whose flash was
outside the branch that did the work: /planner/add_extra raised
UnboundLocalError (a 500) on a blank line, and /planner/set said "Planned"
when the placeholder submitted no recipe. Neither touches the database on the
empty path, so neither needs one here; get_connection is patched to fail loudly
if it is reached.
"""

import unittest
from unittest import mock

import psycopg2

import app
from test_csrf import post_with_token


def _no_db():
    raise AssertionError("empty-input path must not touch the database")


class _NoProfilesConn:
    """Lets the nav's context processor run without a database."""

    def cursor(self, **kwargs):
        raise psycopg2.OperationalError("no database in tests")

    def close(self):
        pass


class PlannerEmptyInputTests(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()

    def _flashes(self):
        with self.client.session_transaction() as sess:
            return sess.get("_flashes", [])

    def test_blank_extra_line_is_an_error_not_a_500(self):
        with mock.patch.object(app, "get_connection", _no_db):
            resp = post_with_token(self.client, "/planner/add_extra", {
                "week_start": "2026-09-28", "day_of_week": "2", "line": "  "})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._flashes(), [("error", "Enter an ingredient line to add it.")])

    def test_placeholder_pick_does_not_claim_it_planned(self):
        with mock.patch.object(app, "get_connection", _no_db):
            resp = post_with_token(self.client, "/planner/set", {
                "week_start": "2026-09-28", "day_of_week": "2", "recipe_id": ""})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._flashes(), [("error", "Pick a recipe to plan it.")])


class ErrorHandlerTests(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        # TESTING propagates exceptions by default; the household never runs
        # with that, and the handler is what's under test.
        app.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.client = app.app.test_client()

    def test_404_stays_a_404_with_a_page(self):
        with mock.patch.object(app, "get_connection", _NoProfilesConn):
            resp = self.client.get("/no-such-page")
        self.assertEqual(resp.status_code, 404)
        self.assertIn(b"No such page", resp.data)

    def test_database_down_renders_a_503_page_not_a_traceback(self):
        def down():
            raise psycopg2.OperationalError("connection refused")
        with mock.patch.object(app, "get_connection", down), \
                self.assertLogs(app.app.logger, "ERROR"):
            resp = self.client.get("/recipes")
        self.assertEqual(resp.status_code, 503)
        self.assertIn(b"Postgres is not answering", resp.data)
        self.assertIn(b'href="/"', resp.data)

    def test_other_exceptions_are_a_500(self):
        with mock.patch.object(app, "get_connection", _NoProfilesConn), \
                mock.patch.object(app, "parse_pasted_recipe", side_effect=ValueError("boom")), \
                self.assertLogs(app.app.logger, "ERROR"):
            resp = post_with_token(self.client, "/recipes/parse_paste", {"pasted": "x"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn(b"Something went wrong on our side", resp.data)


if __name__ == "__main__":
    unittest.main()
