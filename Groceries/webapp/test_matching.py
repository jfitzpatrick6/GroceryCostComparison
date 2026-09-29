"""
Standalone tests for matching.py (#25) - no DB, no Flask, stdlib
`unittest` only (this repo has no existing test infra to match). Run with:

    python3 -m unittest Groceries/webapp/test_matching.py -v

The product-name fixtures below (SAMPLE_CATALOG) are real rows pulled from
a live scrape (`grocery_prices_latest` against this repo's own db container
- Tops/Aldi/BJs; Walmart's scraper doesn't currently produce data, see
README) rather than invented examples, specifically because real listings
are messier than anything I'd have made up by hand (see e.g. the BJs
weighted-item chicken breast comment in app.py's grocery_list()). A fuller
sanity pass against the complete ~23k-row live catalog (not just this
trimmed fixture) was also run manually while building this - see the PR/
commit description for that coverage summary; it isn't reproduced here
since it isn't something CI could run without a live scrape.
"""

import unittest

import matching


class NormalizeTokensTests(unittest.TestCase):
    def test_lowercases_and_strips_punctuation(self):
        self.assertEqual(matching.normalize_tokens("Chicken Breast!"), frozenset({"chicken", "breast"}))

    def test_drops_pure_size_numbers_and_units(self):
        # "4.5-6.5 lbs." -> both numbers and the unit word disappear,
        # leaving only the actual product identity.
        tokens = matching.normalize_tokens("Wellsley Farms Boneless Skinless Chicken Breasts, 4.5-6.5 lbs.")
        self.assertNotIn("4.5", tokens)
        self.assertNotIn("6.5", tokens)
        self.assertNotIn("lbs", tokens)
        self.assertNotIn("lb", tokens)
        self.assertIn("chicken", tokens)
        self.assertIn("breast", tokens)  # stemmed from "Breasts"
        self.assertIn("boneless", tokens)
        self.assertIn("skinless", tokens)

    def test_plural_stemming_collapses_to_singular(self):
        self.assertEqual(matching.normalize_tokens("tomato"), matching.normalize_tokens("tomatoes"))
        self.assertEqual(matching.normalize_tokens("carrot"), matching.normalize_tokens("carrots"))
        self.assertEqual(matching.normalize_tokens("box"), matching.normalize_tokens("boxes"))

    def test_percent_sign_survives_stripping(self):
        # "2%" is meaningful (fat content), not a size/count to discard -
        # only bare numeric tokens and known unit words get dropped.
        self.assertIn("2%", matching.normalize_tokens("2% Milk"))

    def test_phrase_synonym_normalizes_both_sides_the_same(self):
        self.assertEqual(
            matching.normalize_tokens("garbanzo beans"),
            matching.normalize_tokens("chick peas"),
        )
        self.assertEqual(
            matching.normalize_tokens("green onions"),
            matching.normalize_tokens("scallions"),
        )

    def test_empty_or_all_noise_input_returns_empty(self):
        self.assertEqual(matching.normalize_tokens(""), frozenset())
        self.assertEqual(matching.normalize_tokens("12 oz"), frozenset())

    def test_club_is_not_stripped(self):
        # Regression: "club" looks like packaging/marketing filler but is
        # also part of a real Aldi private-label line ("Cheese Club
        # Macaroni and Cheese") - stripping it collapsed that product's
        # tokens down to {cheese, macaroni}, which then out-scored genuine
        # cheese products for a plain "cheese" query. See the comment next
        # to _STRIP_WORDS in matching.py.
        self.assertIn("club", matching.normalize_tokens("Cheese Club Macaroni and Cheese"))


class ScoreMatchTests(unittest.TestCase):
    def test_query_tokens_must_be_a_subset(self):
        query = matching.normalize_tokens("ground beef")
        self.assertIsNone(matching.score_match(query, matching.normalize_tokens("Ground Turkey")))
        self.assertIsNotNone(matching.score_match(query, matching.normalize_tokens("Fresh Ground Beef Patties")))

    def test_low_overlap_ratio_is_pruned(self):
        # A one-word query matching deep inside an otherwise-unrelated,
        # long product name shouldn't count - see MIN_SCORE's rationale.
        query = matching.normalize_tokens("milk")
        noisy_product = matching.normalize_tokens("3 Musketeers Fun Size Milk Chocolate Candy Bars Sharing Pack")
        self.assertIsNone(matching.score_match(query, noisy_product))

    def test_disqualifying_modifier_blocks_category_confusion(self):
        # "butter" alone should not resolve to peanut butter or coffee
        # creamer just because the word is a substring - see
        # DISQUALIFYING_MODIFIERS.
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("butter"), matching.normalize_tokens("Jif Creamy Peanut Butter"),
        ))
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("coffee"), matching.normalize_tokens("Coffee-Mate Hazelnut Creamer"),
        ))
        # But a query that *asks* for peanut butter still works - the
        # disqualifier only applies to single-token queries.
        self.assertIsNotNone(matching.score_match(
            matching.normalize_tokens("peanut butter"), matching.normalize_tokens("Jif Creamy Peanut Butter"),
        ))

    def test_disqualifying_modifier_blocks_butter_pecan_ice_cream(self):
        # Regression for #56: a bare "butter" query normalized to
        # {butter}, a token-subset of "Butter Pecan Ice Cream" ->
        # {butter, pecan, ice, cream} (score 0.25, above MIN_SCORE), with
        # "pecan" missing from DISQUALIFYING_MODIFIERS["butter"] - nothing
        # blocked ice cream from showing as a "butter" match.
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("butter"), matching.normalize_tokens("Butter Pecan Ice Cream"),
        ))

    def test_disqualifying_modifier_blocks_butter_beans_and_lettuce(self):
        # Found auditing #56: same failure as butter pecan ice cream for
        # two other real product categories that happen to contain the
        # word "butter" - both score 0.5 against a bare "butter" query.
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("butter"), matching.normalize_tokens("Butter Beans"),
        ))
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("butter"), matching.normalize_tokens("Butter Lettuce"),
        ))

    def test_cookie_butter_and_butterscotch_already_correctly_blocked(self):
        # Verified (not assumed, per #56) while auditing DISQUALIFYING_MODIFIERS:
        # "cookie butter" is already blocked via the existing "cookie"
        # entry, and "butterscotch" never even reaches the disqualifier
        # check because it normalizes to a single token ("butterscotch"),
        # which isn't equal to the query token "butter" and so never
        # subset-matches in the first place. "Butter Scotch" written as two
        # words is still caught by the existing "scotch" entry.
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("butter"), matching.normalize_tokens("Cookie Butter"),
        ))
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("butter"), matching.normalize_tokens("Butterscotch Pudding"),
        ))
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("butter"), matching.normalize_tokens("Butter Scotch Candy"),
        ))

    def test_disqualifying_modifier_blocks_coffee_ice_cream(self):
        # Found auditing #56: the same "flavor word + ice cream" pattern as
        # butter/pecan recurs for "coffee" - "Coffee Ice Cream" scores 0.33
        # and "Vanilla Coffee Ice Cream Bar" scores exactly 0.2 (right at
        # MIN_SCORE) against a bare "coffee" query.
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("coffee"), matching.normalize_tokens("Coffee Ice Cream"),
        ))
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("coffee"), matching.normalize_tokens("Vanilla Coffee Ice Cream Bar"),
        ))

    def test_disqualifying_modifier_blocks_egg_nog(self):
        # Found auditing #56: "Egg Nog" (written as two words) scores 0.5
        # against a bare "egg" query - it's a drink, not the grocery item
        # "eggs". "Eggnog" as one word was already safe (single token,
        # never equals the query token "egg").
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("egg"), matching.normalize_tokens("Egg Nog"),
        ))
        self.assertIsNone(matching.score_match(
            matching.normalize_tokens("egg"), matching.normalize_tokens("Eggnog"),
        ))


# Real rows pulled from this repo's own live scrape (see the module
# docstring above) - deliberately messy brand/size/marketing text, the
# exact kind of thing the old exact/substring match in app.py couldn't
# handle (see #25).
SAMPLE_CATALOG_ROWS = [
    ("Wellsley Farms Boneless Skinless Chicken Breasts, 4.5-6.5 lbs.", "BJs", 19.99),
    ("Kirkwood Chicken Breasts", "Aldis", 8.49),
    ("Cold Packaged Italian Chicken Breast", "Tops", 6.99),
    ("93/7 Lean Ground Beef", "Aldis", 5.99),
    ("JBS Beef Patties, 80%/20%, Ground", "Tops", 7.49),
    ("a2 Milk Whole Milk", "Tops", 5.29),
    ("Friendly Farms 1% Milk", "Aldis", 1.95),
    ("Carnation Evaporated Milk, 8 ct./12 oz.", "BJs", 11.49),
    ("3 Musketeers Fun Size Milk Chocolate Candy Bars", "Tops", 4.29),
    ("Countryside Creamery Salted Butter Sticks", "Aldis", 3.19),
    ("Jif Creamy Peanut Butter", "Tops", 4.49),
    ("Yellow Onions 3lb", "Aldis", 2.29),
    ("Onion Rolls", "Tops", 3.99),
    ("Barissimo Adventure Ground Coffee", "Aldis", 6.49),
    ("Coffee-Mate Hazelnut Creamer", "BJs", 5.89),
    ("Melitta Coffee Filters", "Tops", 6.09),
]


def _build_catalog():
    catalog = []
    for product, store, price in SAMPLE_CATALOG_ROWS:
        # Built the way matching.load_catalog() builds it, including the ordered
        # sequences added in #99. Omitting them made every test here take
        # score_match's optional-parameter path, so the word-order penalties -
        # the whole point of #99 - were never exercised by this suite at all.
        # Review caught that; with the sequences present these fixtures test the
        # code that actually runs.
        seq, raw = matching.token_sequences(product)
        catalog.append({
            "product": product, "store": store, "price": price, "size": "",
            "unit_price": price, "unit": "each",
            "_tokens": frozenset(seq), "_token_seq": seq, "_raw_seq": raw,
        })
    return catalog


class MatchItemTests(unittest.TestCase):
    def setUp(self):
        self.catalog = _build_catalog()

    def test_chicken_breast_matches_across_all_three_stores_despite_brand_noise(self):
        matches = matching.match_item(self.catalog, "chicken breast")
        stores = {m["store"] for m in matches}
        self.assertEqual(stores, {"BJs", "Aldis", "Tops"})

    def test_ground_beef_does_not_match_unrelated_products(self):
        matches = matching.match_item(self.catalog, "ground beef")
        for m in matches:
            self.assertIn(m["product"], {"93/7 Lean Ground Beef", "JBS Beef Patties, 80%/20%, Ground"})

    def test_milk_does_not_resolve_to_candy_bar(self):
        by_store = matching.best_per_store(matching.match_item(self.catalog, "milk"))
        for m in by_store.values():
            self.assertNotIn("Candy", m["product"])

    def test_butter_does_not_resolve_to_peanut_butter(self):
        matches = matching.match_item(self.catalog, "butter")
        products = {m["product"] for m in matches}
        self.assertIn("Countryside Creamery Salted Butter Sticks", products)
        self.assertNotIn("Jif Creamy Peanut Butter", products)

    def test_coffee_does_not_resolve_to_creamer_or_filters(self):
        matches = matching.match_item(self.catalog, "coffee")
        products = {m["product"] for m in matches}
        self.assertIn("Barissimo Adventure Ground Coffee", products)
        self.assertNotIn("Coffee-Mate Hazelnut Creamer", products)
        self.assertNotIn("Melitta Coffee Filters", products)

    def test_yellow_onion_prefers_closer_match_over_bare_onion_roll(self):
        matches = matching.match_item(self.catalog, "yellow onion")
        products = {m["product"] for m in matches}
        self.assertIn("Yellow Onions 3lb", products)
        self.assertNotIn("Onion Rolls", products)  # "roll" isn't a query token, no match at all

    def test_unmatched_item_returns_empty_list_not_an_error(self):
        self.assertEqual(matching.match_item(self.catalog, "kombucha"), [])

    def test_manual_alias_overrides_normalization(self):
        # "tp" wouldn't normalize-match anything in this fixture catalog at
        # all - it only resolves via MANUAL_ALIASES's override phrase. Add
        # a throwaway toilet-paper row to prove the alias path is actually
        # exercised, not just falling through to zero matches either way.
        catalog = list(self.catalog)
        product = "Angel Soft Toilet Paper, 12 Mega Rolls"
        catalog.append({
            "product": product, "store": "Tops", "price": 12.99, "size": "",
            "unit_price": 12.99, "unit": "each", "_tokens": matching.normalize_tokens(product),
        })
        self.assertEqual(matching.match_item(catalog, "tp")[0]["product"], product)


class BestPerStoreTests(unittest.TestCase):
    def test_picks_highest_score_not_lowest_price(self):
        # Regression: the whole point of best_per_store is that a cheap,
        # loosely-related product can't beat a well-matched, pricier one
        # from the same store just because callers pick "cheapest".
        matches = [
            {"store": "Tops", "product": "loose match", "price": 1.00, "match_score": 0.2},
            {"store": "Tops", "product": "tight match", "price": 5.00, "match_score": 0.6},
        ]
        best = matching.best_per_store(matches)
        self.assertEqual(best["Tops"]["product"], "tight match")

    def test_price_only_breaks_exact_score_ties(self):
        matches = [
            {"store": "Tops", "product": "a", "price": 5.00, "match_score": 0.5},
            {"store": "Tops", "product": "b", "price": 3.00, "match_score": 0.5},
        ]
        best = matching.best_per_store(matches)
        self.assertEqual(best["Tops"]["product"], "b")

    def test_one_result_per_store_at_most(self):
        matches = matching.match_item(_build_catalog(), "chicken breast")
        by_store = matching.best_per_store(matches)
        self.assertEqual(len(by_store), len({m["store"] for m in matches}))


class SearchTermsForTests(unittest.TestCase):
    def test_unaliased_item_falls_back_to_itself(self):
        self.assertEqual(matching.search_terms_for("chicken breast"), ["chicken breast"])

    def test_aliased_item_is_case_and_whitespace_insensitive(self):
        self.assertEqual(matching.search_terms_for("  TP  "), matching.MANUAL_ALIASES["tp"])


if __name__ == "__main__":
    unittest.main()
