"""Tests for /prices' per-store grouping (#107) - no database.

Product names are verbatim from the 2026-09-29 catalogue export (CONTRIBUTING
§7); scores are whatever matching.match_item gives them, not hand-set, so these
tests break if the grouping and the matcher stop agreeing.
"""

import unittest

import app
import matching


def _catalog(rows):
    out = []
    for store, product, unit_price in rows:
        seq, raw = matching.token_sequences(product)
        out.append({
            "store": store, "product": product, "price": 9.99, "unit_price": unit_price,
            "unit": "lb", "_tokens": frozenset(seq), "_token_seq": seq, "_raw_seq": raw,
        })
    return out


_ROWS = [
    ("Aldis", "Kirkwood Chicken Breasts", 2.47),
    ("Aldis", "Kirkwood Panko Chicken Breast Nuggets 48 oz", 3.33),
    ("BJs", "Tyson Boneless Skinless Chicken Breast, 10 lbs.", 2.80),
    ("BJs", "Wellsley Farms Boneless Skinless Chicken Breasts, 4.5-6.5 lbs.", 2.44),
    ("Tops", "TOPS 99% Fat Free Boneless Skinless Chicken Breasts", 5.79),
    ("Tops", "Bananas", 0.59),
]


class GroupSearchResultsTests(unittest.TestCase):
    def setUp(self):
        catalog = _catalog(_ROWS)
        self.columns = app.group_search_results(
            matching.match_item(catalog, "chicken breast"), ["Aldis", "BJs", "Tops"]
        )
        self.by_store = {c["store"]: c for c in self.columns}

    def test_every_store_gets_a_column(self):
        self.assertEqual([c["store"] for c in self.columns], ["Aldis", "BJs", "Tops"])

    def test_best_match_leads_even_when_a_worse_match_is_cheaper(self):
        aldi = [r["product"] for r in self.by_store["Aldis"]["rows"]]
        self.assertEqual(aldi[0], "Kirkwood Chicken Breasts")

    def test_a_better_score_beats_a_cheaper_unit_price(self):
        # Real scores: Tyson 0.40 at $2.80/lb, Wellsley 0.33 at $2.44/lb.
        bjs = [r["product"] for r in self.by_store["BJs"]["rows"]]
        self.assertEqual(bjs[0], "Tyson Boneless Skinless Chicken Breast, 10 lbs.")

    def test_equal_scores_are_ordered_by_unit_price(self):
        rows = [
            {"store": "BJs", "product": "dear", "match_score": 0.5, "unit_price": 4.0},
            {"store": "BJs", "product": "cheap", "match_score": 0.5, "unit_price": 2.0},
        ]
        col = app.group_search_results(rows, ["BJs"])[0]
        self.assertEqual([r["product"] for r in col["rows"]], ["cheap", "dear"])

    def test_a_store_with_no_match_has_an_empty_column_not_no_column(self):
        columns = app.group_search_results(
            matching.match_item(_catalog(_ROWS), "bananas"), ["Aldis", "BJs", "Tops"]
        )
        by_store = {c["store"]: c for c in columns}
        self.assertEqual(by_store["Aldis"]["rows"], [])
        self.assertEqual(by_store["Aldis"]["total"], 0)

    def test_missing_unit_price_sorts_last_within_a_score(self):
        rows = [
            {"store": "Aldis", "product": "a", "match_score": 0.5, "unit_price": None},
            {"store": "Aldis", "product": "b", "match_score": 0.5, "unit_price": 3.0},
        ]
        col = app.group_search_results(rows, ["Aldis"])[0]
        self.assertEqual([r["product"] for r in col["rows"]], ["b", "a"])

    def test_columns_are_not_truncated(self):
        # A cut would hide low-ranked real matches; see group_search_results.
        rows = [{"store": "Tops", "product": str(i), "match_score": 0.5, "unit_price": i}
                for i in range(30)]
        col = app.group_search_results(rows, ["Tops"])[0]
        self.assertEqual(len(col["rows"]), 30)
        self.assertEqual(col["total"], 30)


if __name__ == "__main__":
    unittest.main()
