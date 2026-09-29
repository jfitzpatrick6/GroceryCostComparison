"""Phase 2 of #70: route-level tests for the mutating/money flows, no database.

The database is a small fake cursor that answers only the statements these
routes issue; product names and quantities are real (household list rows and
the dev catalogue, 2026-09-29), per CONTRIBUTING §7. Where a flow's logic could
be pulled out of the route it was (summarize_where_to_buy), and is tested
directly - #70: "if a test genuinely needs Postgres, that's a signal the logic
should be extracted into a pure function instead".
"""

import re
import unittest
from unittest import mock

import app
import matching
from test_csrf import post_with_token


def _catalog_row(store, product, price, unit_price, unit="lb"):
    seq, raw = matching.token_sequences(product)
    return {"store": store, "product": product, "price": price, "size": "", "unit_price": unit_price,
            "unit": unit, "datetime": None, "_tokens": frozenset(seq), "_token_seq": seq, "_raw_seq": raw}


class _Cur:
    """Answers where-to-buy's and add-to-list's queries from in-memory state."""

    def __init__(self, list_items=(), existing=None):
        self.list_items = list(list_items)
        self.existing = existing or {}
        self.writes = []
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "to_regclass('grocery_prices_latest')" in sql:
            self._one = {"table_exists": True}
        elif "FROM grocery_list_items WHERE checked = FALSE ORDER BY" in sql:
            self._all = self.list_items
        elif sql.startswith("SELECT id, qty FROM grocery_list_items"):
            self._one = self.existing.get(params[0].lower())
        elif sql.startswith(("UPDATE grocery_list_items", "INSERT INTO grocery_list_items")):
            self.writes.append((sql.split()[0], params))
        elif "FROM profiles" in sql:
            self._all = []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self, **kwargs):
        return self.cur

    def commit(self):
        pass

    def close(self):
        pass


_WTB_CATALOG = (
    _catalog_row("Aldis", "Kirkwood Chicken Breasts", 19.79, 2.47),
    _catalog_row("BJs", "Wellsley Farms Boneless Skinless Chicken Breasts, 4.5-6.5 lbs.", 13.42, 2.44),
    _catalog_row("Tops", "TOPS Boneless Chicken Breast Skinless with Rib Meat", 15.94, 2.99),
    _catalog_row("Tops", "TOPS Spaghetti", 1.29, 1.29),
)


class WhereToBuyRouteTests(unittest.TestCase):
    def test_cheapest_store_and_totals_on_the_page(self):
        items = [
            {"id": 1, "name": "chicken breast", "qty": None, "pinned_product": None, "pinned_store": None},
            {"id": 2, "name": "spaghetti", "qty": "1 lb", "pinned_product": None, "pinned_store": None},
            {"id": 3, "name": "saffron", "qty": None, "pinned_product": None, "pinned_store": None},
        ]
        cur = _Cur(list_items=items)
        with mock.patch.object(app, "get_connection", lambda *a, **k: _Conn(cur)), \
                mock.patch.object(matching, "cached_catalog", lambda c: list(_WTB_CATALOG)):
            body = app.app.test_client().get("/list/where-to-buy").data.decode()
        # BJs' package is cheapest for chicken; Tops is the only spaghetti; saffron unmatched.
        self.assertIn("Wellsley Farms Boneless Skinless Chicken Breasts", body)
        self.assertRegex(body, r"saffron")
        self.assertIn("No price match found", body)
        # Split total = 13.42 + 1.29
        self.assertIn("14.71", body)


class SummarizeWhereToBuyTests(unittest.TestCase):
    def _p(self, **by_store):
        by = {s: {"total_cost": c} for s, c in by_store.items()}
        return {"by_store": by, "cheapest": min(by.values(), key=lambda m: m["total_cost"])}

    def test_full_coverage_ranks_before_a_cheaper_partial_store(self):
        per_item = [self._p(BJs=13.42, Tops=15.94, Aldis=19.79), self._p(Tops=1.29)]
        split, totals = app.summarize_where_to_buy(per_item, {"BJs", "Tops", "Aldis"})
        self.assertAlmostEqual(split, 14.71)
        self.assertEqual([t["store"] for t in totals], ["Tops", "BJs", "Aldis"])
        self.assertTrue(totals[0]["covers_all"])
        # BJs is cheaper for what it has, but it lacks spaghetti - never ranked as if free.
        self.assertEqual((totals[1]["covered"], totals[1]["of_total"]), (1, 2))

    def test_empty_list(self):
        self.assertEqual(app.summarize_where_to_buy([], set()), (None, []))


class AddWeekToListRouteTests(unittest.TestCase):
    def _post(self, combined, existing):
        cur = _Cur(existing=existing)
        with mock.patch.object(app, "get_connection", lambda *a, **k: _Conn(cur)), \
                mock.patch.object(app, "get_week_ingredients", lambda c, w: combined), \
                mock.patch.object(app, "apply_pantry", lambda c, comb: comb):
            client = app.app.test_client()
            post_with_token(client, "/planner/add_to_list", {"week_start": "2026-09-27"})
            with client.session_transaction() as sess:
                flashes = [m for _, m in sess.get("_flashes", [])]
        return cur.writes, flashes

    def test_merges_same_unit_adds_new_and_skips_pantry_covered(self):
        # Real list rows: "Ground Beef | 9 lb" already on the list.
        combined = [
            {"name": "ground beef", "amount": "1.5", "unit": "lb", "pantry_have": None, "need_amount": "1.5"},
            {"name": "spaghetti", "amount": "1", "unit": "lb", "pantry_have": None, "need_amount": "1"},
            {"name": "salt", "amount": "1", "unit": "tsp", "pantry_have": 5.0, "need_amount": "0"},
        ]
        writes, flashes = self._post(combined, {"ground beef": {"id": 7, "qty": "9 lb"}})
        self.assertIn(("UPDATE", ("10.5 lb", 7)), writes)
        self.assertIn(("INSERT", ("spaghetti", "1 lb")), writes)
        self.assertEqual(len(writes), 2)  # salt is covered by the pantry
        self.assertTrue(any(re.search(r"1 added.*1 merged.*1 skipped", f) for f in flashes), flashes)

    def test_no_word_none_in_quantities(self):
        # e750a5e: a unitless, amountless ingredient wrote the literal "None".
        combined = [{"name": "lettuce", "amount": "", "unit": None, "pantry_have": None, "need_amount": ""}]
        writes, _ = self._post(combined, {})
        self.assertEqual(writes, [("INSERT", ("lettuce", None))])


class _PantryCur:
    def __init__(self, pantry):
        self.pantry = pantry  # lower name -> {"id", "amount", "unit"}
        self.log = []
        self._one = None

    def execute(self, sql, params=None):
        self.log.append((sql.split()[0], params))
        if sql.startswith("SELECT id, amount, unit FROM pantry_items"):
            self._one = self.pantry.get(params[0].lower())
        elif sql.startswith("UPDATE pantry_items"):
            for row in self.pantry.values():
                if row["id"] == params[2]:
                    row["amount"] = params[0]

    def fetchone(self):
        return self._one


class MarkCookedDepletionTests(unittest.TestCase):
    """deplete_pantry_for_slot (#30/#48) - the path #75 first ran end to end."""

    def _deplete(self, lines, pantry):
        cur = _PantryCur(pantry)
        with mock.patch.object(app, "slot_ingredient_lines", lambda *a: lines), \
                app.app.test_request_context("/"):
            app.deplete_pantry_for_slot(cur, "2026-09-27", 2, "dinner")
        return cur

    def test_subtracts_matching_name_and_unit_and_records_the_snapshot(self):
        # Mirrors #75's real run: 2 lb ground beef in the pantry, recipe uses 1 lb.
        cur = self._deplete([{"name": "ground beef", "amount": "1", "unit": "lb"}],
                            {"ground beef": {"id": 5, "amount": 2.0, "unit": "lb"}})
        self.assertEqual(cur.pantry["ground beef"]["amount"], 1.0)
        inserts = [p for v, p in cur.log if v == "INSERT"]
        self.assertEqual(inserts, [("2026-09-27", 2, "dinner", "ground beef", "lb", 2.0, 1.0)])

    def test_clamps_at_zero_and_skips_unit_mismatch_and_non_numbers(self):
        cur = self._deplete(
            [{"name": "flour", "amount": "5", "unit": "cup"},
             {"name": "milk", "amount": "1", "unit": "cup"},
             {"name": "salt", "amount": "a pinch", "unit": None}],
            {"flour": {"id": 1, "amount": 2.0, "unit": "cup"},
             "milk": {"id": 2, "amount": 1.0, "unit": "gal"}},
        )
        self.assertEqual(cur.pantry["flour"]["amount"], 0.0)
        self.assertEqual(cur.pantry["milk"]["amount"], 1.0)  # gal vs cup: untouched, not guessed
        self.assertEqual(sum(1 for v, _ in cur.log if v == "INSERT"), 1)


if __name__ == "__main__":
    unittest.main()
