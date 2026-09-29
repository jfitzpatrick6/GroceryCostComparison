"""Tests for app.resolve_item_match (#98) - the single place that decides which
price row a grocery-list item is priced from.

A pure function over a list of dicts and a dict, so no database is needed and it
runs in the required CI tier (CONTRIBUTING §7).

Three paths matter, and the third is the one most likely to be "simplified" away
later because it looks like dead code:

1. pinned item -> exact catalogue lookup, not a fuzzy match
2. unpinned item -> fuzzy matching, exactly as before this feature existed
3. pinned item whose product has LEFT the catalogue -> fall back to fuzzy

Path 3 is not defensive padding. Products get delisted, renamed, and dropped by a
store between scrapes, and when that happens the item is still on the household's
list and still needs buying. Resolving it to "no price" would silently drop it
from the where-to-buy totals, which is the confident-wrong-answer failure mode
this project is built to avoid.
"""

import unittest

import app
import matching


def _row(product, store, price=1.00, unit_price=1.00, unit="lb", size="1 lb"):
    # `_tokens` is what matching.load_catalog() precomputes and match_item() reads
    # unconditionally, so a fixture without it is not shaped like a real catalog
    # row. Building it with the real normalizer keeps these fixtures honest
    # rather than hand-writing token lists that could drift from the real rules.
    return {"product": product, "store": store, "price": price, "size": size,
            "unit_price": unit_price, "unit": unit, "match_score": 0.9,
            "_tokens": matching.normalize_tokens(product)}


CATALOG = [
    _row("Wellsley Farms Bacon", "BJs", 8.99, 5.99),
    _row("TOPS Bacon Chips", "Tops", 3.49, 13.96),
    _row("Breakfast Pizza With Bacon", "Tops", 6.99, 6.99, "each"),
    _row("Kirkwood Chicken Breasts", "Aldis", 5.99, 2.00),
]


def _item(name, pinned_product=None, pinned_store=None):
    return {"id": 1, "name": name, "qty": "", "checked": False,
            "pinned_product": pinned_product, "pinned_store": pinned_store}


class ResolveItemMatchTests(unittest.TestCase):
    def test_a_pinned_item_resolves_to_exactly_that_product(self):
        result = app.resolve_item_match(CATALOG, _item("bacon", "Wellsley Farms Bacon", "BJs"))
        self.assertEqual(list(result), ["BJs"])
        self.assertEqual(result["BJs"]["product"], "Wellsley Farms Bacon")

    def test_a_pinned_item_ignores_a_better_fuzzy_match(self):
        # The whole point of pinning: "bacon" fuzzy-matches "TOPS Bacon Chips" and
        # "Breakfast Pizza With Bacon" at least as well as real bacon (#97/#99), so
        # if the user picked Wellsley Farms Bacon, nothing may substitute a
        # different product back in.
        result = app.resolve_item_match(CATALOG, _item("bacon", "Wellsley Farms Bacon", "BJs"))
        products = {row["product"] for row in result.values()}
        self.assertEqual(products, {"Wellsley Farms Bacon"})

    def test_a_pinned_item_prices_from_one_store_only(self):
        # Comparing a deliberately chosen product across stores would mean
        # substituting a different product for the one the user picked.
        result = app.resolve_item_match(CATALOG, _item("bacon", "Wellsley Farms Bacon", "BJs"))
        self.assertEqual(len(result), 1)

    def test_an_unpinned_item_falls_back_to_fuzzy_matching(self):
        result = app.resolve_item_match(CATALOG, _item("chicken breasts"))
        self.assertIn("Aldis", result)
        self.assertEqual(result["Aldis"]["product"], "Kirkwood Chicken Breasts")

    def test_a_pinned_item_whose_product_left_the_catalogue_falls_back_to_fuzzy(self):
        # Product was pinned, then delisted or renamed. The item is still on the
        # list and still needs buying, so it must degrade to "try to match the
        # text" - not to no price, which would silently drop it from totals.
        result = app.resolve_item_match(
            CATALOG, _item("bacon", "Brand X Bacon (discontinued)", "BJs")
        )
        self.assertTrue(result, "a stale pin must still produce some match, not nothing")
        self.assertNotIn("Brand X Bacon (discontinued)", {r["product"] for r in result.values()})

    def test_a_pin_with_only_one_half_set_is_treated_as_unpinned(self):
        # Half-written pins shouldn't be possible (add_pinned requires both), but
        # the columns are independent and nullable, so treat the combination as
        # "not pinned" rather than crashing on a None comparison.
        for item in (_item("bacon", "Wellsley Farms Bacon", None),
                     _item("bacon", None, "BJs")):
            result = app.resolve_item_match(CATALOG, item)
            self.assertTrue(result, f"half-pin {item} should still match something")
            self.assertNotEqual(list(result), ["BJs"], "a half-pin must not resolve as a pin")

    def test_a_pin_to_a_different_store_than_the_product_is_not_matched(self):
        # Wellsley Farms Bacon exists at BJs only; pinning it to Tops describes
        # something that isn't in the catalogue, so it must fall through to fuzzy
        # rather than returning Tops with the wrong product.
        result = app.resolve_item_match(CATALOG, _item("bacon", "Wellsley Farms Bacon", "Tops"))
        self.assertNotIn("Wellsley Farms Bacon", {r["product"] for r in result.values()
                                                 if r["store"] == "Tops"})

    def test_no_match_at_all_returns_empty_not_none(self):
        # Callers do `if by_store:` and treat empty as unmatched, which is what
        # puts an item under "No price match found for" (#25). Returning None
        # instead would raise on .values().
        result = app.resolve_item_match(CATALOG, _item("saffron threads"))
        self.assertEqual(result, {})

    def test_returns_the_catalog_row_shape_callers_depend_on(self):
        # where_to_buy annotates each row with package-fit fields and reads price /
        # unit_price / size; /list reads product / store / match_score. If the
        # pinned path ever returned a trimmed dict, those pages would break.
        result = app.resolve_item_match(CATALOG, _item("bacon", "Wellsley Farms Bacon", "BJs"))
        row = result["BJs"]
        for key in ("product", "store", "price", "size", "unit_price", "unit", "match_score"):
            self.assertIn(key, row)


if __name__ == "__main__":
    unittest.main()
