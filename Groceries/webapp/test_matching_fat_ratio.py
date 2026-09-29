"""Lean/fat ratio queries (#104).

Every product name here is copied verbatim from the live catalogue export of
2026-09-29 (23,089 rows) - CONTRIBUTING §7: the point is to encode how the three
retailers actually spell a fat ratio, which is what the old normalizer got wrong.
Before #104, "80/20" was dropped as a bare number and every one of these queries
degraded to plain "ground beef".
"""

import unittest

import matching

# (store, product) pairs, verbatim.
_REAL_GROUND_MEAT = [
    ("Aldis", "93/7 Lean Ground Beef"),
    ("Aldis", "Simply Nature Organic Grass Fed 93/07 Ground Beef"),
    ("Aldis", "Simply Nature Organic Grass Fed 85% Lean Ground Beef"),
    ("Aldis", "USDA 80% Lean 20% Fat Ground Beef Patties - 10 Count"),
    ("Aldis", "Specially Selected 75% Lean/25% Fat Wagyu Ground Beef"),
    ("BJs", "Swift & Company 73% Lean Ground Beef, 5 lbs."),
    ("BJs", "Wellsley Farms 80% Lean Ground Beef, 9.75-10.5 lbs."),
    ("BJs", "Wellsley Farms 90% Lean Ground Beef, 3.75-4.5 lbs."),
    ("BJs", "Farm to Family by Butterball 93% Lean/7% Fat Ground Turkey, 2.5 lbs."),
    ("Tops", "TOPS 80%/20% Ground Beef Burgers"),
    ("Tops", "Grass Run Farms Beef, Ground, 85%/15%"),
    ("Tops", "Grass Run Farms Beef, Ground, 92/8"),
    ("Tops", "TOPS 90% Lean Ground Beef"),
    ("Tops", "TOPS Ground Beef Burgers"),
]


def _catalog(rows):
    catalog = []
    for store, product in rows:
        seq, raw = matching.token_sequences(product)
        catalog.append({
            "store": store, "product": product, "price": 1.0,
            "_tokens": frozenset(seq), "_token_seq": seq, "_raw_seq": raw,
        })
    return catalog


class FatRatioTests(unittest.TestCase):
    def setUp(self):
        self.catalog = _catalog(_REAL_GROUND_MEAT)

    def _products(self, query):
        return {m["product"] for m in matching.match_item(self.catalog, query)}

    def test_ratio_is_kept_as_a_token(self):
        self.assertEqual(matching.normalize_tokens("80/20 ground beef"), {"80%", "ground", "beef"})

    def test_80_20_matches_only_80_percent_products_in_every_spelling(self):
        self.assertEqual(self._products("80/20 ground beef"), {
            "USDA 80% Lean 20% Fat Ground Beef Patties - 10 Count",
            "Wellsley Farms 80% Lean Ground Beef, 9.75-10.5 lbs.",
            "TOPS 80%/20% Ground Beef Burgers",
        })

    def test_hyphenated_query_is_the_same_ratio(self):
        self.assertEqual(self._products("80-20 ground beef"), self._products("80/20 ground beef"))

    def test_leading_zero_and_bare_ratio_forms(self):
        self.assertEqual(self._products("93/7 ground beef"), {
            "93/7 Lean Ground Beef",
            "Simply Nature Organic Grass Fed 93/07 Ground Beef",
        })
        self.assertEqual(self._products("92/8 ground beef"), {"Grass Run Farms Beef, Ground, 92/8"})

    def test_percent_slash_percent_form(self):
        self.assertIn("Grass Run Farms Beef, Ground, 85%/15%", self._products("85/15 ground beef"))

    def test_a_ratio_nobody_stocks_matches_nothing_rather_than_another_fat_level(self):
        # "No answer beats a wrong answer": this used to return every ground beef.
        self.assertEqual(self._products("70/30 ground beef"), set())

    def test_lean_word_survives_the_rewrite(self):
        self.assertIn(
            "Specially Selected 75% Lean/25% Fat Wagyu Ground Beef",
            self._products("75% lean ground beef"),
        )

    def test_plain_ground_beef_still_matches_every_fat_level(self):
        beef = {product for _, product in _REAL_GROUND_MEAT if "Turkey" not in product}
        self.assertEqual(self._products("ground beef"), beef)

    def test_non_ratio_fractions_are_still_dropped(self):
        # Real names: sizes, shrimp counts and a deodorant, none summing to 100.
        for name, expected in [
            ("Doritos Tortilla Chips Cool Ranch Flavored 14 1/2 Oz",
             {"dorito", "tortilla", "chip", "cool", "ranch", "flavored"}),
            ("Jubilee Brand Headless Shrimp, 16/20, Chem Free, USA Wild Caught, 2 lbs.",
             {"jubilee", "brand", "headless", "shrimp", "chem", "free", "usa", "wild", "caught"}),
            ("Old Spice Bearglove 24/7 Freshness Aluminum Free Deodorant",
             {"old", "spice", "bearglove", "freshness", "aluminum", "free", "deodorant"}),
        ]:
            self.assertEqual(matching.normalize_tokens(name), expected, name)


if __name__ == "__main__":
    unittest.main()
