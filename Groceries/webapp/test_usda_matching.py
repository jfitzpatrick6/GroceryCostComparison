"""
Tests for #54's fix to the #36 USDA FoodData Central ingredient-matching
code in app.py (`_usda_grams_per_unit`, `_USDA_SEARCH_ALIASES`) - covers
the same-word-different-food ambiguity described in #54: a bare "pepper"
query top-matching "Pepper, banana, raw" instead of black pepper spice,
"butter" top-matching "Clarified butter (ghee)" instead of plain butter,
etc.

Two test classes:

- `MockedUsdaMatchingTests` - no network, `requests.get` is monkeypatched
  to return fixture data. The fixtures are trimmed real API responses
  (captured against the live FDC API while building this fix, same as
  test_matching.py's SAMPLE_CATALOG being real scraped rows rather than
  invented examples) so the exact same word-filter/starts-with/portion-
  fallback branches the live API exercises are exercised here too. These
  always run, regardless of API key/network availability.

- `LiveUsdaApiTests` - hits the real FDC API. Skipped automatically if
  USDA_API_KEY isn't set (checked via python-dotenv-free direct .env read,
  since app.py itself only reads it from the process environment). #54 is
  specifically about real-world search-relevance/disambiguation behavior a
  mock can't meaningfully stand in for, so this suite matters more here
  than it would for most features - if it's skipped in whatever
  environment runs this, that's a real gap in coverage, not just a nice-
  to-have.

Unlike test_matching.py (which tests matching.py, a zero-dependency
module), this imports app.py directly, so it needs the full
requirements.txt (Flask, psycopg2-binary, requests) installed - same as
running the webapp itself. Run with:

    python3 -m unittest Groceries/webapp/test_usda_matching.py -v
"""

import os
import unittest
from unittest import mock

# Loads USDA_API_KEY from .env into the environment (same as docker-compose
# would) *before* importing app, if it's not already set - app.py reads it
# via os.getenv() at call time, and the live tests need it present to not
# skip themselves.
if not os.getenv("USDA_API_KEY"):
    _env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if os.path.exists(_env_path):
        with open(_env_path) as f:
            for line in f:
                if line.startswith("USDA_API_KEY="):
                    os.environ["USDA_API_KEY"] = line.strip().split("=", 1)[1]

import app


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _food(fdc_id, description):
    return {"fdcId": fdc_id, "description": description}


def _portion(amount, modifier, gram_weight):
    return {"amount": amount, "modifier": modifier, "gramWeight": gram_weight}


# Trimmed real FDC responses, captured against the live API while building
# this fix (see the app.py comments next to _USDA_SEARCH_ALIASES for the
# specific fdcIds/descriptions this is based on).
_SEARCH_FIXTURES = {
    # #54's own example: "pepper" alone top-matches the vegetable before
    # the spice; the alias biases the query to "pepper black".
    "pepper black": [
        _food(170931, "Spices, pepper, black"),
        _food(169394, "Pepper, banana, raw"),
        _food(169396, "Peppers, ancho, dried"),
    ],
    # #54's own example: "butter" alone top-matches ghee (or worse, some
    # other flavored/derivative "butter"); the alias biases to "salted".
    "butter salted": [
        _food(173410, "Butter, salted"),
        _food(790508, "Butter, stick, salted"),
        _food(171314, "Butter, Clarified butter (ghee)"),
    ],
    # No alias entry for "onion" - real data showed the general fix
    # (plural-tolerant word boundary + starts-with preference) is enough
    # on its own; this fixture is what a real "onion" search returns.
    "onion": [
        _food(170000, "Onions, raw"),
        _food(169844, "DENNY'S, onion rings"),
        _food(170002, "Onions, dehydrated flakes"),
    ],
    # Real data: the top-ranked "Flour, wheat, all-purpose..." entries are
    # FDC's newer "Foundation" dataType, which only carries a RACC portion
    # (no "1 cup" data) - the correct food, but useless without the
    # multi-candidate portion fallback. The older "SR Legacy" entry for the
    # same food, a few slots down, has the cup data.
    "wheat flour all-purpose": [
        _food(789890, "Flour, wheat, all-purpose, enriched, bleached"),
        _food(789951, "Flour, wheat, all-purpose, enriched, unbleached"),
        _food(790018, "Flour, wheat, all-purpose, unenriched, unbleached"),
        _food(169761, "Wheat flour, white, all-purpose, unenriched"),
    ],
    # Regression fixture for the ANY -> ALL query-word-matching fix: none
    # of these descriptions contain "melted" (FDC's raw-commodity entries
    # never describe a cooking state), so a correct fix must reject all of
    # them rather than falling back to "butter" alone and picking ghee.
    "melted butter": [
        _food(173520, "Babyfood, snack, GERBER, GRADUATES, YOGURT MELTS"),
        _food(171314, "Butter, Clarified butter (ghee)"),
        _food(173410, "Butter, salted"),
        _food(174987, "Croissants, butter"),
    ],
}

_DETAIL_FIXTURES = {
    170931: [
        _portion(1.0, "tbsp, ground", 6.9),
        _portion(1.0, "dash", 0.1),
        _portion(1.0, "tsp, ground", 2.3),
        _portion(1.0, "tsp, whole", 2.9),
    ],
    169394: [],  # "Pepper, banana, raw" - no relevant portion data either way
    173410: [
        _portion(1.0, 'pat (1" sq, 1/3" high)', 5.0),
        _portion(1.0, "stick", 113.0),
        _portion(1.0, "cup", 227.0),
        _portion(1.0, "tbsp", 14.2),
    ],
    171314: [],  # ghee - real FDC data has no foodPortions at all
    170000: [
        _portion(1.0, "cup, chopped", 160.0),
        _portion(1.0, "medium (2-1/2\" dia)", 110.0),
    ],
    169844: [_portion(10.0, "rings", 60.0)],  # onion rings - no cup data
    789890: [_portion(1.0, None, 30.0)],  # Foundation flour - RACC only, no "cup"
    789951: [_portion(1.0, None, 30.0)],
    790018: [_portion(1.0, None, 30.0)],
    169761: [_portion(1.0, "cup", 125.0)],  # SR Legacy flour - has "cup"
}


def _fake_get(url, params=None, timeout=None):
    params = params or {}
    if url.endswith("/foods/search"):
        return _FakeResponse({"foods": _SEARCH_FIXTURES.get(params.get("query"), [])})
    # .../food/{fdcId}
    fdc_id = int(url.rsplit("/", 1)[-1])
    return _FakeResponse({"foodPortions": _DETAIL_FIXTURES.get(fdc_id, [])})


@mock.patch.dict(os.environ, {"USDA_API_KEY": "test-key"})
class MockedUsdaMatchingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("app.requests.get", side_effect=_fake_get)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_pepper_resolves_to_spice_not_vegetable(self):
        # The #54 headline bug: bare "pepper" used to top-match "Pepper,
        # banana, raw" (which has no tsp/tbsp portion data anyway, in real
        # FDC data) - it must resolve to the spice's real 2.3g/tsp instead.
        self.assertEqual(app._usda_grams_per_unit("pepper", ["tsp"]), 2.3)

    def test_butter_resolves_to_plain_butter_not_ghee(self):
        # The #54 headline bug: bare "butter" used to top-match "Butter,
        # Clarified butter (ghee)" - it must resolve to plain salted
        # butter's real 227g/cup instead.
        self.assertEqual(app._usda_grams_per_unit("butter", ["cup"]), 227.0)

    def test_onion_resolves_to_raw_onion_not_onion_rings(self):
        # No alias entry needed for "onion" (see _USDA_SEARCH_ALIASES's
        # comment) - the plural-tolerant whole-word filter plus
        # starts-with preference alone correctly prefer "Onions, raw" over
        # "DENNY'S, onion rings", which is the only match a strict
        # (non-plural-tolerant) \bonion\b boundary would have found.
        self.assertEqual(app._usda_grams_per_unit("onion", ["cup"]), 160.0)

    def test_flour_falls_back_past_candidates_with_no_portion_data(self):
        # The correct food ("wheat flour, all-purpose") is identified from
        # the first candidate, but the first three real-data candidates
        # (FDC's newer "Foundation" entries) don't carry cup portion data -
        # the fix must keep trying subsequent already-identity-confirmed
        # candidates rather than giving up after the very first one.
        self.assertEqual(app._usda_grams_per_unit("flour", ["cup"]), 125.0)

    def test_melted_butter_returns_none_rather_than_ghee(self):
        # Regression for the ANY -> ALL query-word fix: previously, only
        # ONE of a multi-word query's words had to appear anywhere in the
        # description, so "melted butter" fell back to matching on
        # "butter" alone and picked up ghee. Requiring every word be
        # present correctly finds no match at all (a missing estimate,
        # not a wrong one) since FDC's raw-ingredient data has no
        # "melted" entries.
        self.assertIsNone(app._usda_grams_per_unit("melted butter", ["cup"]))

    def test_unrelated_word_does_not_match(self):
        self.assertIsNone(app._usda_grams_per_unit("xyzzynotafood", ["cup"]))

    def test_no_api_key_returns_none(self):
        with mock.patch.dict(os.environ, {"USDA_API_KEY": ""}, clear=False):
            os.environ.pop("USDA_API_KEY", None)
            self.assertIsNone(app._usda_grams_per_unit("butter", ["cup"]))


@unittest.skipUnless(os.getenv("USDA_API_KEY"), "USDA_API_KEY not available - skipping live FDC API tests")
class LiveUsdaApiTests(unittest.TestCase):
    """Hits the real FDC API. #54 is fundamentally about live search-
    relevance behavior, so this is the coverage that actually matters -
    the mocked tests above are a fast regression net for the same logic,
    not a substitute for this. Asserts on the specific gram values FDC
    returns for the *correct* food (captured manually while building this
    fix) since that's the strongest available proxy for "matched the right
    fdcId" without adding a return-the-fdcId debug path to production
    code."""

    def test_pepper_is_the_spice(self):
        # "Spices, pepper, black", fdcId 170931 -> 1 tsp, ground = 2.3g.
        # The vegetable "Pepper, banana, raw" (fdcId 169394, the old wrong
        # top match) has no tsp/tbsp portion data in real FDC data, so a
        # regression back to matching it would show up as None here, not
        # as a different (wrong) number.
        self.assertEqual(app._usda_grams_per_unit("pepper", ["tsp"]), 2.3)

    def test_butter_is_plain_butter(self):
        # "Butter, salted", fdcId 173410 -> 1 cup = 227g.
        self.assertEqual(app._usda_grams_per_unit("butter", ["cup"]), 227.0)

    def test_onion_is_raw_onion(self):
        # "Onions, raw", fdcId 170000 -> 1 cup, chopped = 160g. Confirmed
        # against real data (not assumed, per #54) that a bare "onion"
        # query needs the general plural-tolerant/starts-with fix, not its
        # own alias table entry.
        self.assertEqual(app._usda_grams_per_unit("onion", ["cup"]), 160.0)

    def test_flour_is_wheat_all_purpose_flour(self):
        # "Wheat flour, white, all-purpose, unenriched" (SR Legacy),
        # fdcId 169761 -> 1 cup = 125g - not "Arrowroot flour" (fdcId
        # 170684, the old code's real top match, which returns 128g/cup:
        # a plausible-looking but wrong number for a different flour).
        self.assertEqual(app._usda_grams_per_unit("flour", ["cup"]), 125.0)

    def test_sugar_is_granulated_sugar(self):
        # "Sugars, granulated" (SR Legacy, fdcId 169655) -> 1 cup = 200g -
        # not "Sugar, turbinado" (fdcId 170674), the old code's real top
        # match for a bare "sugar" query.
        self.assertEqual(app._usda_grams_per_unit("sugar", ["cup"]), 200.0)

    def test_black_pepper_phrase_also_resolves_correctly(self):
        # A multi-word ingredient name close to how a recipe would
        # actually phrase it, not just the bare single word.
        self.assertEqual(app._usda_grams_per_unit("black pepper", ["tsp"]), 2.3)


if __name__ == "__main__":
    unittest.main()
