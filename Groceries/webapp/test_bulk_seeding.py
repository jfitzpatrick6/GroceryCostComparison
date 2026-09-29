"""Bulk entry for pantry and recipes (#75) - parsers only, no database.

The end-to-end check (bulk pantry -> plan -> mark cooked -> pantry decremented,
against real Postgres) is in the #75 commit; the required tier has no database.
"""

import unittest

import app


class PantryAmountTests(unittest.TestCase):
    def test_numbers_and_fractions(self):
        self.assertEqual(app.parse_pantry_amount("2"), 2.0)
        self.assertEqual(app.parse_pantry_amount("0.5"), 0.5)
        self.assertEqual(app.parse_pantry_amount("1/2"), 0.5)
        self.assertEqual(app.parse_pantry_amount(" 1/3 "), 0.3333)

    def test_blank_is_no_amount(self):
        self.assertIsNone(app.parse_pantry_amount(""))
        self.assertIsNone(app.parse_pantry_amount(None))

    def test_unreadable_is_refused_not_guessed(self):
        for bad in ["two", "1/0", "-1", "1..2", "inf", "nan"]:
            with self.assertRaises((ValueError, ZeroDivisionError), msg=bad):
                app.parse_pantry_amount(bad)


class LineParsingTests(unittest.TestCase):
    """Review of #75: mixed numbers used to be misread silently."""

    def test_mixed_numbers_and_unicode_fractions(self):
        self.assertEqual(app.parse_ingredient_line("1 1/2 cups flour"), ("1.5", "cup", "flour"))
        self.assertEqual(app.parse_ingredient_line("2 1/2 lb chicken"), ("2.5", "lb", "chicken"))
        self.assertEqual(app.parse_ingredient_line("½ cup sugar"), ("0.5", "cup", "sugar"))
        self.assertEqual(app.parse_ingredient_line("2½ lb beef"), ("2.5", "lb", "beef"))

    def test_existing_shapes_unchanged(self):
        self.assertEqual(app.parse_ingredient_line("1/2 cup rice"), ("1/2", "cup", "rice"))
        self.assertEqual(app.parse_ingredient_line("2 lb ground beef"), ("2", "lb", "ground beef"))
        self.assertEqual(app.parse_ingredient_line("salt"), (None, None, "salt"))
        self.assertEqual(app.parse_ingredient_line("12 eggs"), ("12", None, "eggs"))

    def test_a_range_is_not_collapsed(self):
        amount, _, name = app.parse_ingredient_line("1-2 cups flour")
        self.assertTrue(name.startswith("-") or amount is None, (amount, name))


class RecipeSplitTests(unittest.TestCase):
    PASTE = """Taco Night
Serves 4
Ingredients
1 lb ground beef
8 each taco shells
Instructions
Brown the beef.
---
Pancakes
Ingredients
2 cups flour
1 cup milk

   ----
"""

    def test_splits_on_dashed_lines_and_drops_empty_chunks(self):
        recipes = app.split_recipe_paste(self.PASTE)
        self.assertEqual([r["name"] for r in recipes], ["Taco Night", "Pancakes"])
        self.assertEqual([r["ingredient_count"] for r in recipes], [2, 2])
        self.assertEqual(recipes[0]["servings"], "4")
        self.assertIn("Brown the beef.", recipes[0]["notes"])

    def test_single_recipe_without_separator(self):
        self.assertEqual(len(app.split_recipe_paste("Soup\n1 cup broth")), 1)

    def test_nothing(self):
        self.assertEqual(app.split_recipe_paste("  \n---\n "), [])

    def test_servings_never_500s_the_insert(self):
        self.assertEqual(app._servings_or_none("4"), 4)
        for bad in ["4-6", "serves 4", "", None, "0", "-2", "99999999999"]:
            self.assertIsNone(app._servings_or_none(bad), bad)


if __name__ == "__main__":
    unittest.main()
