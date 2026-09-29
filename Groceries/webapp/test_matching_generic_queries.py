"""Regression tests for the generic-single-word matching failures in #99.

Every product name here is a REAL name captured from a live scrape, and every
query is one a household actually writes. That matters more than usual: the bug
was that the matcher could not tell a branded version of the right product from a
different product that merely mentions it, which is a property of how retailers
actually name things. Invented fixtures would encode the author's assumptions
about naming instead of the world's, and would have passed on the broken code
(CONTRIBUTING §7).

Measured before the fix, on a 21,574-row live catalog:

    bacon -> Tops "Breakfast Pizza With Bacon" (0.333)
             Tops "TOPS Bacon Chips"           (0.333)
    eggs  -> Aldis "Aldi Potato Salad with Egg" (0.250) in the top four
    milk  -> Tops "Goya Coconut Milk"           (0.333)
             BJs  "Carnation Evaporated Milk"   (0.333)  tied with real milk

Because where_to_buy keeps one match per store and then picks the CHEAPEST, a tie
means the wrong product wins whenever it costs less. "Cheapest milk" could
legitimately resolve to coconut milk.
"""

import unittest

import matching

# (product, store, price) - real scraped names.
CATALOG_ROWS = [
    ("Wellsley Farms Bacon", "BJs", 8.99),
    ("TOPS Bacon Chips", "Tops", 3.49),
    ("Breakfast Pizza With Bacon", "Tops", 6.99),
    ("Applegate Uncured Turkey Bacon", "Tops", 7.49),
    ("TOPS Large Eggs", "Tops", 4.29),
    ("Aldi Potato Salad with Egg", "Aldis", 3.99),
    ("Original Egg Beaters", "BJs", 4.99),
    ("Eggs 12 ct", "Aldis", 4.49),
    ("Goya Coconut Milk", "Tops", 2.79),
    ("Carnation Evaporated Milk", "BJs", 3.29),
    ("a2 Milk Whole Milk", "Tops", 4.99),
    ("Whole Milk", "Aldis", 3.49),
    ("Milk, Whole", "BJs", 3.79),
    ("Kirkwood Chicken Breasts", "Aldis", 5.99),
]


def build_catalog():
    """Catalog rows shaped exactly like matching.load_catalog() produces them,
    including the ordered sequences the word-order penalties need."""
    rows = []
    for product, store, price in CATALOG_ROWS:
        seq, raw = matching.token_sequences(product)
        rows.append({
            "product": product, "store": store, "price": price, "size": "1",
            "unit_price": price, "unit": "ea", "datetime": None,
            "_tokens": matching.normalize_tokens(product),
            "_token_seq": seq, "_raw_seq": raw,
        })
    return rows


def products(matches):
    return [m["product"] for m in matches]


class GenericQueryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog()

    def match(self, query):
        return matching.match_item(self.catalog, query)

    # --- bacon -----------------------------------------------------------

    def test_a_pizza_containing_bacon_is_demoted_below_real_bacon(self):
        # "with Bacon" makes bacon an ingredient of a composite, not the product.
        #
        # Demoted, NOT deleted. An earlier version of this change rejected
        # composites outright by applying MIN_SCORE after the penalty, which
        # silently dropped correct matches wholesale (241 of 395 live "cheese"
        # rows). Eligibility is decided on the raw ratio and the penalty only
        # affects ranking, so a composite can still surface - but last, and below
        # LOW_CONFIDENCE_SCORE so #97 flags it - which is the honest outcome when
        # it is the only thing a store stocks.
        matches = self.match("bacon")
        names = products(matches)
        self.assertIn("Breakfast Pizza With Bacon", names)
        self.assertEqual(names[-1], "Breakfast Pizza With Bacon", "composite must rank last")
        score = next(m["match_score"] for m in matches
                     if m["product"] == "Breakfast Pizza With Bacon")
        self.assertLess(score, matching.LOW_CONFIDENCE_SCORE if hasattr(matching, "LOW_CONFIDENCE_SCORE") else 0.45)

    def test_best_per_store_never_picks_the_pizza_when_real_bacon_exists(self):
        # What the user actually sees: one product per store, best score wins.
        # The demotion only matters if it changes the selection.
        by_store = matching.best_per_store(self.match("bacon"))
        self.assertNotIn("Breakfast Pizza With Bacon",
                         [v["product"] for v in by_store.values()])

    def test_bacon_ranks_real_bacon_above_bacon_chips(self):
        matches = self.match("bacon")
        names = products(matches)
        self.assertIn("Wellsley Farms Bacon", names)
        self.assertLess(
            names.index("Wellsley Farms Bacon"), names.index("TOPS Bacon Chips"),
            "branded real bacon must outrank a chip product that mentions bacon",
        )

    def test_bacon_chips_is_not_rejected_outright(self):
        # A penalty, not a ban: it may be the only bacon-ish thing a store stocks,
        # and #97 flags it as low-confidence rather than presenting it as certain.
        self.assertIn("TOPS Bacon Chips", products(self.match("bacon")))

    # --- eggs ------------------------------------------------------------

    def test_potato_salad_with_egg_is_demoted_and_never_selected(self):
        self.assertIn("Aldi Potato Salad with Egg", products(self.match("eggs")))
        by_store = matching.best_per_store(self.match("eggs"))
        self.assertNotIn("Aldi Potato Salad with Egg",
                         [v["product"] for v in by_store.values()])

    def test_eggs_prefers_a_product_that_is_just_eggs(self):
        # "Eggs 12 ct" normalizes to a single token, so it scores 1.0 - the
        # trailing count must not masquerade as the head noun.
        self.assertEqual(products(self.match("eggs"))[0], "Eggs 12 ct")

    # --- milk ------------------------------------------------------------

    def test_milk_does_not_match_coconut_or_evaporated_milk(self):
        # The one case the structural rules cannot reach: in both names "milk" IS
        # the head noun and there is no connective. These are genuinely
        # milk-shaped products that aren't what "milk" on a list means.
        names = products(self.match("milk"))
        self.assertNotIn("Goya Coconut Milk", names)
        self.assertNotIn("Carnation Evaporated Milk", names)

    def test_milk_still_matches_real_milk(self):
        names = products(self.match("milk"))
        self.assertIn("Whole Milk", names)
        self.assertEqual(names[0], "Whole Milk")

    def test_an_explicit_coconut_milk_request_still_finds_coconut_milk(self):
        # DISQUALIFYING_MODIFIERS only applies to single-token queries. Someone
        # who writes "coconut milk" means coconut milk, and rejecting it would be
        # a wrong answer of the same kind this issue is fixing.
        self.assertIn("Goya Coconut Milk", products(self.match("coconut milk")))

    def test_chocolate_milk_is_not_disqualified(self):
        # Deliberate exclusion from the milk lexicon: chocolate milk is drinking
        # milk to most people, so rejecting it would trade one wrong answer for
        # another. Asserted so the exclusion can't be "tidied up" later.
        self.assertNotIn("chocolate", matching.DISQUALIFYING_MODIFIERS["milk"])

    # --- things that must not regress ------------------------------------

    def test_a_specific_multiword_query_is_unchanged(self):
        matches = self.match("chicken breasts")
        self.assertEqual(products(matches), ["Kirkwood Chicken Breasts"])
        self.assertAlmostEqual(matches[0]["match_score"], 0.667, places=2)

    def test_inverted_product_names_still_match(self):
        # "Milk, Whole" has no query token last, so it takes the head-noun
        # penalty - but it must still match, just ranked below "Whole Milk".
        # A hard head-noun rule would have dropped a legitimate product.
        names = products(self.match("milk"))
        self.assertIn("Milk, Whole", names)
        self.assertLess(names.index("Whole Milk"), names.index("Milk, Whole"))


class ScoreMatchPenaltyTests(unittest.TestCase):
    """Unit-level checks on the two penalties, independent of the catalog."""

    def _score(self, product, query):
        seq, raw = matching.token_sequences(product)
        return matching.score_match(
            matching.normalize_tokens(query), matching.normalize_tokens(product), seq, raw
        )

    def test_composite_connective_penalty_applies(self):
        plain = self._score("Wellsley Farms Bacon", "bacon")
        composite = self._score("Breakfast Pizza With Bacon", "bacon")
        self.assertIsNotNone(plain)
        self.assertIsNotNone(composite, "demoted, not deleted - see the pizza test")
        self.assertLess(composite, plain)
        self.assertAlmostEqual(composite, plain * matching.COMPOSITE_PENALTY / 1.0, places=6)

    def test_a_long_product_name_still_matches_a_one_word_query(self):
        """The regression the review caught, and the reason penalties cannot gate
        eligibility. A 1-token query against a 5-token product has ratio 0.2,
        exactly MIN_SCORE; multiplying by NON_HEAD_PENALTY first pushed it to 0.14
        and the product stopped matching at all. On the live catalog that dropped
        241 of 395 "cheese" rows, 102 of 150 "cream", 68 of 96 "sugar".
        """
        score = self._score("Athenos Traditional Feta Cheese Chunk", "cheese")
        self.assertIsNotNone(score, "a 5-token product must still match a 1-word query")
        self.assertAlmostEqual(score, (1 / 5) * matching.NON_HEAD_PENALTY)

    def test_a_product_ending_in_the_query_word_is_not_penalized_at_all(self):
        self.assertAlmostEqual(self._score("Countryside Creamery Salted Butter", "butter"), 0.25)

    def test_multiword_queries_take_no_word_order_penalty(self):
        # _PHRASE_SYNONYMS rewrites "Half & Half" to "half and half", so the
        # query's own connective used to make the composite rule fire against its
        # exact match - "half and half" resolved to "Southern Grove Pecan Halves"
        # at all three stores. Penalties are single-token only, like
        # DISQUALIFYING_MODIFIERS.
        seq, raw = matching.token_sequences("Friendly Farms Half & Half")
        q = matching.normalize_tokens("half and half")
        p = matching.normalize_tokens("Friendly Farms Half & Half")
        self.assertEqual(matching.score_match(q, p, seq, raw), len(q) / len(p))

    def test_macaroni_and_cheese_keeps_its_exact_match(self):
        seq, raw = matching.token_sequences("Bella Vita Macaroni and Cheese")
        q = matching.normalize_tokens("macaroni and cheese")
        p = matching.normalize_tokens("Bella Vita Macaroni and Cheese")
        # "and" is a strip word, so the product is {bella, vita, macaroni, cheese}
        # and the query {macaroni, cheese}: ratio 2/4, and no penalty because the
        # query has two tokens.
        self.assertEqual(len(q), 2)
        self.assertAlmostEqual(matching.score_match(q, p, seq, raw), 2 / 4)

    def test_a_product_packed_in_its_own_juice_is_not_a_composite(self):
        # "Cento Minced Clams in Clam Juice" has clams on both sides of "in": it
        # IS clams. Treating that as a composite dropped 26 live canned-goods rows.
        score = self._score("Cento Minced Clams in Clam Juice", "clams")
        self.assertIsNotNone(score)
        # Ratio is 1/4 and the head noun is "juice", so the non-head penalty
        # applies - but the composite penalty must NOT, which is the point: the
        # 0.7 factor rather than 0.4 x 0.7 proves the query token preceding the
        # connective was noticed.
        self.assertAlmostEqual(score, (1 / 4) * matching.NON_HEAD_PENALTY, places=6)

    def test_an_ampersand_brand_is_not_mistaken_for_a_composite(self):
        # token_sequences rewrites "&" to " and ", so "A&W Cream Soda" contains a
        # literal standalone "w". With "w" in the connective set, "cream" looked
        # like an ingredient of a composite and A&W Cream Soda was lost - 24 live
        # rows carry a standalone "w", 12 of them created by that rewrite.
        self.assertNotIn("w", matching._INGREDIENT_CONNECTIVES)
        score = self._score("A&W Cream Soda", "cream")
        self.assertIsNotNone(score)
        self.assertAlmostEqual(score, (1 / 3) * matching.NON_HEAD_PENALTY)

    def test_a_real_ampersand_composite_is_still_caught(self):
        # Dropping "w" must not lose genuine composites: the "&" -> " and "
        # rewrite means "Crackers & Bacon Bits" still reads as "crackers and
        # bacon bits", and bacon follows a connective without preceding it.
        self.assertTrue(matching._is_composite(
            matching.normalize_tokens("bacon"),
            matching.token_sequences("Crackers & Bacon Bits")[1],
        ))

    def test_a_rate_suffix_is_not_treated_as_the_head_noun(self):
        # "per lb" left "per" as the head noun, which degraded
        # "boneless skinless chicken breast" from 0.800 to 0.560 and broke #99's
        # explicit done bar that specific multi-word queries must not regress.
        self.assertNotIn("per", matching.normalize_tokens("Kirkwood Chicken Breasts, per lb"))

    def test_head_noun_penalty_lowers_but_does_not_reject(self):
        head = self._score("Wellsley Farms Bacon", "bacon")
        non_head = self._score("TOPS Bacon Chips", "bacon")
        self.assertIsNotNone(non_head)
        self.assertLess(non_head, head)

    def test_omitting_the_sequences_reproduces_the_pre_99_score(self):
        # The sequences are optional so existing callers and hand-built fixtures
        # keep working. Without them the score must be exactly the old ratio -
        # this pins the backwards-compatibility claim rather than assuming it.
        q = matching.normalize_tokens("bacon")
        p = matching.normalize_tokens("TOPS Bacon Chips")
        self.assertAlmostEqual(matching.score_match(q, p), 1 / 3)
        seq, raw = matching.token_sequences("TOPS Bacon Chips")
        self.assertAlmostEqual(
            matching.score_match(q, p, seq, raw), (1 / 3) * matching.NON_HEAD_PENALTY
        )

    def test_ampersand_counts_as_a_connective(self):
        # "&" is rewritten to " and ", so "Crackers & Bacon Bits" is a composite
        # the same way "Crackers and Bacon Bits" is - demoted, not deleted.
        plain = self._score("Wellsley Farms Bacon", "bacon")
        composite = self._score("Crackers & Bacon Bits", "bacon")
        self.assertLess(composite, plain)


if __name__ == "__main__":
    unittest.main()
