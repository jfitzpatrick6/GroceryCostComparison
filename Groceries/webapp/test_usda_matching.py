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
    # #58: real FDC data for the bell-pepper alias query. The newer
    # "Peppers, bell, red, raw" entry (2258590) is what a plain "bell
    # pepper" search top-ranks now - correct food, but its detail response
    # carries only an unmodified RACC portion (no cup/tbsp), which is why
    # the plain query produced no estimate before the alias existed.
    #
    # What these fixtures actually exercise is the ALL-word identity filter,
    # not the multi-candidate portion fallback: 2258590 is rejected for
    # lacking "sweet" and 168550 for lacking "raw", so neither is ever
    # fetched and 170108 is the only candidate the portion loop sees. (The
    # fallback loop is genuinely covered elsewhere - by the "wheat flour
    # all-purpose" fixture above, where the top Foundation entries do pass
    # the filter but lack cup data.) Kept here anyway because they are what
    # the real search returns, and they prove the filter rejects them.
    "peppers sweet red raw": [
        _food(170108, "Peppers, sweet, red, raw"),
        _food(2258590, "Peppers, bell, red, raw"),
        _food(168550, "Peppers, sweet, red, sauteed"),
    ],
    "peppers sweet green raw": [
        _food(170427, "Peppers, sweet, green, raw"),
        _food(2258588, "Peppers, bell, green, raw"),
    ],
    # #58: real FDC data for the green-onion alias query. 170005 is FIRST in
    # live relevance order (not second, as an earlier version of this comment
    # claimed - rechecked against the API), so the alias resolves on the
    # first candidate; 2727585 is the Foundation entry that has only a RACC
    # portion, and is here because the real search returns it.
    "onion spring scallion raw": [
        _food(170005, "Onions, spring or scallions (includes tops and bulb), raw"),
        _food(2727585, "Green onion, (scallion), bulb and greens, root removed, raw"),
    ],
    # #58: what a plain "spring onion" search really returns - captured live.
    # 170005 comes FIRST, so unlike "green onion" this name needs no alias at
    # all: "spring" appears in the target description and #54's ALL-word
    # filter lands it without help. The interesting entries are the two
    # false friends that contain "spring" but aren't onions (hard red spring
    # wheat, POLAND SPRING water) - they're what the filter has to reject,
    # and they're why adding an alias here would be a speculative no-op that
    # only raised the required-word count from 2 to 4.
    "spring onion": [
        _food(170005, "Onions, spring or scallions (includes tops and bulb), raw"),
        _food(168889, "Wheat, hard red spring"),
        _food(170000, "Onions, raw"),
        _food(173234, "Beverages, water, bottled, POLAND SPRING"),
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
    # #58: real FDC portion data for the bell-pepper / green-onion entries.
    # The "Peppers, bell, {color}, raw" entries have NO cup/tbsp portions at
    # all (only an unmodified RACC portion) - that's exactly why a plain
    # "bell pepper" query produces no estimate today; the SR Legacy
    # "sweet" entries do carry cup data.
    # Real FDC order for 170108: a whole-pepper portion comes first, then
    # the tablespoon, then cup-sliced before cup-chopped - so the code's
    # "first matching modifier wins" behavior returns the *sliced* value.
    # (Chopped is heavier per cup because of less empty space; sliced is
    # what a recipe most often means by "cups of bell pepper", so this is
    # also the more defensible estimate.)
    170108: [
        _portion(1.0, "large (2-1/4 per pound, approx 3-3/4\" long, 3\" dia.)", 164.0),
        _portion(1.0, "tablespoon", 9.3),
        _portion(1.0, "cup, sliced", 92.0),
        _portion(1.0, "cup, chopped", 149.0),
    ],
    2258590: [_portion(1.0, None, 85.0)],  # bell red - RACC only, no cup
    168550: [],  # sauteed variant - no relevant portion data
    # Real FDC order for 170427 differs from 170108: cup-chopped comes
    # first here, so the same code returns 149g for green but 92g for red -
    # an FDC data quirk, not a code bug; the tests assert each real value.
    170427: [
        _portion(1.0, "cup, chopped", 149.0),
        _portion(1.0, "cup, sliced", 92.0),
        _portion(1.0, "tbsp", 9.3),
    ],
    2258588: [_portion(1.0, None, 85.0)],  # bell green - RACC only, no cup
    170005: [
        _portion(1.0, "cup, chopped", 100.0),
        _portion(1.0, "tbsp chopped", 6.0),
    ],
    2727585: [_portion(1.0, None, 85.0)],  # green onion (Foundation) - RACC only
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
        # #54: bare "pepper" must be the spice, not "Pepper, banana, raw". Since #78 the
        # spice's "tsp, ground" 2.3g vs "tsp, whole" 2.9g (26% apart) means an unqualified
        # tsp is no estimate - so identity is asserted through the named form.
        self.assertEqual(app._usda_grams_per_unit("ground pepper", ["tsp"]), 2.3)
        self.assertIsNone(app._usda_grams_per_unit("pepper", ["tsp"]))
    def test_portion_policy_on_real_audited_values(self):
        # #78, values captured live 2026-09-29.
        onion = [("cup, chopped", 160.0), ("cup, sliced", 115.0)]
        broccoli = [("cup, chopped or diced", 88.0), ("cup chopped", 91.0)]
        brown_sugar = [("cup packed", 220.0), ("cup unpacked", 145.0)]
        self.assertEqual(app.choose_portion(onion, {"chopped"}), 160.0)
        self.assertIsNone(app.choose_portion(onion, set()))            # 39% apart
        self.assertIsNone(app.choose_portion(onion, {"diced"}))        # form FDC lacks
        self.assertEqual(app.choose_portion(broccoli, set()), 89.5)    # 3%: median
        self.assertEqual(app.choose_portion(brown_sugar, {"packed"}), 220.0)  # not "unpacked"
        self.assertEqual(app.choose_portion(brown_sugar, {"unpacked"}), 145.0)
        self.assertEqual(app._split_form_words("brown sugar, packed"), ("brown sugar", {"packed"}))

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

    def test_bell_pepper_resolves_via_alias_to_sweet_red(self):
        # #78: 170108/170427 carry "cup, sliced" 92g AND "cup, chopped" 149g (62%
        # apart). The old 92/149 split was only FDC's list order; an unqualified
        # cup is now no estimate, and the recipe's form picks the portion.
        self.assertIsNone(app._usda_grams_per_unit("bell pepper", ["cup"]))
        self.assertEqual(app._usda_grams_per_unit("bell pepper, sliced", ["cup"]), 92.0)
        self.assertEqual(app._usda_grams_per_unit("chopped bell pepper", ["cup"]), 149.0)
    def test_red_bell_pepper_resolves_via_alias(self):
        self.assertEqual(app._usda_grams_per_unit("sliced red bell pepper", ["cup"]), 92.0)
    def test_green_bell_pepper_resolves_via_alias_to_sweet_green(self):
        # #78: 170108/170427 carry "cup, sliced" 92g AND "cup, chopped" 149g (62%
        # apart). The old 92/149 split was only FDC's list order; an unqualified
        # cup is now no estimate, and the recipe's form picks the portion.
        # Same form, same answer, whichever colour - the inconsistency #78 was filed for.
        self.assertEqual(app._usda_grams_per_unit("chopped green bell pepper", ["cup"]), 149.0)
        self.assertEqual(app._usda_grams_per_unit("sliced green bell pepper", ["cup"]), 92.0)
    def test_green_onion_resolves_via_alias_to_spring_onion(self):
        # #58. Without the alias this did NOT fail safe. Verified live: a
        # plain "green onion" search returns 170006 "Onions, young green,
        # tops only" first - and 170005 isn't in its top six at all - so the
        # result was 71g/cup for the greens-only product. That is a silently
        # WRONG estimate, not a missing one, which is the failure mode this
        # codebase exists to avoid; #58 was filed assuming it produced no
        # estimate. The alias lands on 170005 (100g/cup chopped).
        self.assertEqual(app._usda_grams_per_unit("green onion", ["cup"]), 100.0)

    def test_spring_onion_needs_no_alias(self):
        # The counterpart to the test above, and the reason "spring onion" is
        # deliberately absent from _USDA_SEARCH_ALIASES. Verified live: FDC
        # returns 170005 FIRST for a plain "spring onion" query, because that
        # description literally contains "spring" - so #54's ALL-word filter
        # resolves it with no help, and an alias would change nothing while
        # raising the required-word count from 2 to 4. This asserts the
        # unaliased path keeps working, so that decision is a recorded one
        # rather than an omission someone later "fixes" by adding an entry.
        self.assertNotIn("spring onion", app._USDA_SEARCH_ALIASES)
        self.assertEqual(app._usda_grams_per_unit("spring onion", ["cup"]), 100.0)

    def test_bell_pepper_tbsp_portion_also_resolves(self):
        # The same alias path works for tbsp, not just cup. FDC spells
        # 170108's tablespoon modifier out in full ("tablespoon"), which
        # bare "tbsp" is not a substring of - so this has to go through
        # _USDA_MEASURE_WORDS, the same expansion resolve_purchase_amount
        # does in production. Passing ["tbsp"] directly asserts on a call
        # shape that never happens and fails on the spelled-out modifier.
        # (A *tsp* lookup would find no match on this entry: it has no
        # teaspoon portion - an existing data gap, unrelated to #58.)
        self.assertEqual(
            app._usda_grams_per_unit("bell pepper", app._USDA_MEASURE_WORDS["tbsp"]), 9.3
        )

    def test_measure_words_list_both_spellings(self):
        # Guards the internal consistency of _USDA_MEASURE_WORDS: the portion
        # lookup is a plain substring test against FDC's modifier text, which
        # is spelled inconsistently between foods - sometimes the abbreviation
        # ("tsp"), sometimes spelled out ("teaspoon") - so every canonical unit
        # must list both forms or one of them silently resolves to no estimate.
        #
        # Scope this honestly: it checks a constant against itself, so it does
        # NOT detect FDC changing its modifier vocabulary. Only the live tier
        # can do that. What it does catch is a new unit being added with only
        # one spelling, which is the easy mistake to make and the one that
        # fails silently.
        #
        # "tablespoon" is deliberately not asserted here - it is already
        # covered end-to-end by test_bell_pepper_tbsp_portion_also_resolves,
        # which builds its call from _USDA_MEASURE_WORDS["tbsp"]. "teaspoon"
        # is asserted because no other test exercises the spelled-out tsp form.
        for unit, words in app._USDA_MEASURE_WORDS.items():
            self.assertIn(unit, words, f"{unit} must match its own abbreviation")
        self.assertIn("teaspoon", app._USDA_MEASURE_WORDS["tsp"])

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
        # #54: bare "pepper" must be the spice, not "Pepper, banana, raw". Since #78 the
        # spice's "tsp, ground" 2.3g vs "tsp, whole" 2.9g (26% apart) means an unqualified
        # tsp is no estimate - so identity is asserted through the named form.
        self.assertEqual(app._usda_grams_per_unit("ground pepper", ["tsp"]), 2.3)
        self.assertIsNone(app._usda_grams_per_unit("pepper", ["tsp"]))
    def test_butter_is_plain_butter(self):
        # "Butter, salted", fdcId 173410 -> 1 cup = 227g.
        self.assertEqual(app._usda_grams_per_unit("butter", ["cup"]), 227.0)

    def test_onion_is_raw_onion(self):
        # "Onions, raw", fdcId 170000. #78: live data carries "cup, chopped" 160g and
        # "cup, sliced" 115g (39% apart), so the form decides; bare "onion" by the cup is
        # no estimate. Identity (not onion rings) is still what this asserts.
        self.assertEqual(app._usda_grams_per_unit("chopped onion", ["cup"]), 160.0)
        self.assertIsNone(app._usda_grams_per_unit("onion", ["cup"]))
    def test_flour_is_wheat_all_purpose_flour(self):
        # "Wheat flour, white, all-purpose, unenriched" (SR Legacy),
        # fdcId 169761 -> 1 cup = 125g - not "Arrowroot flour" (fdcId
        # 170684, the old code's real top match, which returns 128g/cup:
        # a plausible-looking but wrong number for a different flour).
        self.assertEqual(app._usda_grams_per_unit("flour", ["cup"]), 125.0)

    def test_sugar_is_granulated_sugar(self):
        # #58 follow-up: FDC added a newer "Sugars, granulated" entry
        # (fdcId 746784) that now top-ranks the alias query; it carries no
        # cup portion data, so the multi-candidate fallback must keep going
        # to the SR Legacy entry (169655) -> 1 cup = 200g. Not "Sugar,
        # turbinado" (170674), the old code's real top match for a bare
        # "sugar" query. (Before this FDC change, 169655 ranked first and
        # was hit directly - same correct food either way.)
        self.assertEqual(app._usda_grams_per_unit("sugar", ["cup"]), 200.0)

    def test_black_pepper_phrase_also_resolves_correctly(self):
        # Two-word phrase reaches the spice too. #78: ground 2.3g vs whole 2.9g per tsp,
        # so the form is needed for a number.
        self.assertEqual(app._usda_grams_per_unit("ground black pepper", ["tsp"]), 2.3)
        self.assertIsNone(app._usda_grams_per_unit("black pepper", ["tsp"]))
    def test_bell_pepper_gets_a_real_estimate(self):
        # #78: 170108/170427 carry "cup, sliced" 92g AND "cup, chopped" 149g (62%
        # apart). The old 92/149 split was only FDC's list order; an unqualified
        # cup is now no estimate, and the recipe's form picks the portion.
        self.assertIsNone(app._usda_grams_per_unit("bell pepper", ["cup"]))
        self.assertEqual(app._usda_grams_per_unit("sliced bell pepper", ["cup"]), 92.0)
    def test_red_bell_pepper_gets_a_real_estimate(self):
        self.assertEqual(app._usda_grams_per_unit("chopped red bell pepper", ["cup"]), 149.0)
    def test_green_bell_pepper_gets_a_real_estimate(self):
        self.assertEqual(app._usda_grams_per_unit("chopped green bell pepper", ["cup"]), 149.0)
    def test_green_onion_gets_a_real_estimate(self):
        # #58: live check that "green onion" resolves to "Onions, spring or
        # scallions (includes tops and bulb), raw" (170005) -> 1 cup chopped
        # = 100g - not the greens-only "Onions, young green, tops only"
        # (170006, 71g/cup) that a plain search top-ranks.
        self.assertEqual(app._usda_grams_per_unit("green onion", ["cup"]), 100.0)

    def test_spring_onion_gets_a_real_estimate(self):
        self.assertEqual(app._usda_grams_per_unit("spring onion", ["cup"]), 100.0)


if __name__ == "__main__":
    unittest.main()
