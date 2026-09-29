"""Tests for /prices' per-store grouping (#107) - no database.

Product names are verbatim from the 2026-09-29 catalogue export (CONTRIBUTING
§7); scores are whatever matching.match_item gives them, not hand-set, so these
tests break if the grouping and the matcher stop agreeing.
"""

import unittest
import unittest.mock

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


class _Cur:
    def __init__(self):
        self.last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.last = sql

    def fetchone(self):
        return {"table_exists": True}

    def fetchall(self):
        return []


class _Conn:
    def cursor(self, **kwargs):
        return _Cur()

    def commit(self):
        pass

    def close(self):
        pass


class StoreNameRoutingTests(unittest.TestCase):
    """Review of #113: a store-name query must go to the substring table without
    loading the catalog (that was two full scans of the view), and "aldi" must
    count as the store "Aldis"."""

    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()

    def _get(self, q):
        loads = []
        with unittest.mock.patch.object(app, "get_connection", _Conn), \
                unittest.mock.patch.object(matching, "cached_catalog", lambda cur: loads.append(1) or []):
            resp = self.client.get("/prices", query_string={"q": q})
        return resp, len(loads)

    def test_store_names_skip_the_catalog(self):
        for q in ["aldi", "Aldis", "bjs", "BJ's", "tops"]:
            resp, loads = self._get(q)
            self.assertEqual((resp.status_code, loads), (200, 0), q)

    def test_product_query_uses_the_matcher(self):
        _, loads = self._get("chicken breast")
        self.assertEqual(loads, 1)


class SaleBadgeTests(unittest.TestCase):
    """#19: a product on sale shows what it was. Real row from the live BJs
    scrape of 2026-09-29 (Tyson panko popcorn chicken, club sale)."""

    def test_sale_badge_on_search_results(self):
        seq, raw = matching.token_sequences("Tyson Frozen All Natural Panko Breaded Popcorn Chicken, 3.5 lbs.")
        row = {"store": "BJs", "product": "Tyson Frozen All Natural Panko Breaded Popcorn Chicken, 3.5 lbs.",
               "price": 14.99, "regular_price": 18.99, "size": "3.5 lb", "unit_price": 4.28, "unit": "lb",
               "datetime": None, "category": "Frozen Foods > Frozen Meat",
               "_tokens": frozenset(seq), "_token_seq": seq, "_raw_seq": raw}
        app.app.config["TESTING"] = True
        with unittest.mock.patch.object(app, "get_connection", _Conn), \
                unittest.mock.patch.object(matching, "cached_catalog", lambda cur: [row]):
            body = app.app.test_client().get("/prices", query_string={"q": "popcorn chicken"}).data.decode()
        self.assertIn("$14.99", body)
        self.assertIn("sale &middot; was $18.99", body)
