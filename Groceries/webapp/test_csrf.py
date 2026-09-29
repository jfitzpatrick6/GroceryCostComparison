"""CSRF protection (#69) - no database.

The uniformity test is the important one: #69 says a partial rollout is worse
than none, because it looks protected. So every <form method="post"> in every
template must carry the token field, checked by scanning the templates rather
than by remembering to.
"""

import glob
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import app

TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
_POST_FORM = re.compile(r'<form\b[^>]*method=["\']?post["\']?[^>]*>(.*?)</form>', re.I | re.S)


def _no_db():
    raise AssertionError("a rejected POST must not reach the database")


def post_with_token(client, url, data):
    """POST the way a browser does after loading a page: with the session's token."""
    with client.session_transaction() as sess:
        sess["_csrf"] = "test-token"
    return client.post(url, data={**data, "csrf_token": "test-token"})


class TemplateCoverageTests(unittest.TestCase):
    def test_every_post_form_carries_the_token(self):
        forms = 0
        for path in glob.glob(os.path.join(TEMPLATES, "*.html")):
            for body in _POST_FORM.findall(Path(path).read_text()):
                forms += 1
                self.assertIn('name="csrf_token" value="{{ csrf_token() }}"', body, path)
        # Counted independently of _POST_FORM, so a form that regex fails to parse
        # (odd spacing, a ">" inside the tag) shows up as a mismatch, not a pass.
        independent = sum(
            len(re.findall(r'method\s*=\s*["\']?post', Path(p).read_text(), re.I))
            for p in glob.glob(os.path.join(TEMPLATES, "*.html"))
        )
        self.assertEqual(forms, independent)
        self.assertGreaterEqual(forms, 28)


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        app.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.client = app.app.test_client()

    def test_post_without_token_is_a_400_page_and_touches_nothing(self):
        with mock.patch.object(app, "get_connection", _no_db):
            resp = self.client.post("/recipes/1/delete")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"That form had expired", resp.data)

    def test_post_with_wrong_token_is_rejected(self):
        with self.client.session_transaction() as sess:
            sess["_csrf"] = "right"
        with mock.patch.object(app, "get_connection", _no_db):
            resp = self.client.post("/recipes/1/delete", data={"csrf_token": "wrong"})
        self.assertEqual(resp.status_code, 400)

    def test_post_with_the_session_token_reaches_the_route(self):
        with mock.patch.object(app, "get_connection", _no_db):
            resp = post_with_token(self.client, "/planner/add_extra",
                                   {"week_start": "2026-09-28", "day_of_week": "1", "line": ""})
        self.assertEqual(resp.status_code, 302)

    def test_unknown_url_is_a_404_not_an_expired_form(self):
        self.assertEqual(self.client.post("/no-such-route").status_code, 404)

    def test_get_is_not_checked(self):
        with app.app.test_request_context("/", method="GET"):
            self.assertIsNone(app.check_csrf())

    def test_token_is_stable_within_a_session(self):
        with app.app.test_request_context("/"):
            self.assertEqual(app.csrf_token(), app.csrf_token())

    def test_no_constant_secret_key(self):
        # The old fallback was a constant published in app.py.
        self.assertNotEqual(app.app.secret_key, "grocery-cost-comparison-dev-key")
        self.assertFalse(re.search(r"secret_key\s*=\s*os\.getenv\([^)]*,", Path(app.__file__).read_text()))

    def test_session_cookie_is_samesite_lax(self):
        self.assertEqual(app.app.config["SESSION_COOKIE_SAMESITE"], "Lax")


if __name__ == "__main__":
    unittest.main()
