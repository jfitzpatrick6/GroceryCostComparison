"""load_catalog and the #114 category column - no database.

The webapp can be deployed before the scraper has added `category` to
grocery_prices (collector's schema step runs on the next scrape, possibly not
until 03:00). Selecting a missing column would 500 every price page in that
window, so load_catalog must only ask for it when the view has it.
"""

import unittest

import matching


class _Cur:
    def __init__(self, has_category):
        self.has_category = has_category
        self.selects = []
        self._one = None

    def execute(self, sql, params=None):
        if "information_schema.columns" in sql:
            self._one = (1,) if self.has_category else None
        else:
            self.selects.append(sql)

    def fetchone(self):
        return self._one

    def fetchall(self):
        row = {"product": "TOPS Split Chicken Breast", "store": "Tops", "price": 5.0, "size": "1 lb",
               "unit_price": 5.0, "unit": "lb", "datetime": None}
        if self.has_category:
            row["category"] = "Meat & Seafood > Poultry"
        return [row]


class LoadCatalogCategoryTests(unittest.TestCase):
    def test_old_schema_without_category_still_loads(self):
        cur = _Cur(has_category=False)
        catalog = matching.load_catalog(cur)
        self.assertNotIn("category", cur.selects[0])
        self.assertEqual(len(catalog), 1)
        self.assertIsNone(catalog[0].get("category"))

    def test_new_schema_selects_category(self):
        cur = _Cur(has_category=True)
        catalog = matching.load_catalog(cur)
        self.assertIn("category", cur.selects[0])
        self.assertEqual(catalog[0]["category"], "Meat & Seafood > Poultry")


if __name__ == "__main__":
    unittest.main()
