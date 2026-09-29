"""Phase 1 of #70: app.py's pure helpers, no database.

Fixture values are the household's real data (a read-only dump of the dev
database, 2026-09-29) wherever the helper has seen real input: recipe lines like
"1.5 lb ground beef" / "0.5 tsp pepper" / "2 clove garlic", and grocery-list
quantities like "9 lb", "4 each", "8 oz", "1" and NULL. CONTRIBUTING §7:
invented fixtures encode the author's assumptions instead of the world.
"""

import datetime
import unittest

import app


class MergeQtyTests(unittest.TestCase):
    """merge_qty is where e750a5e's literal "None" in list quantities lived."""

    def test_same_unit_sums(self):
        self.assertEqual(app.merge_qty("9 lb", "1.5", "lb"), "10.5 lb")
        self.assertEqual(app.merge_qty("2 tsp", "1", "tsp"), "3 tsp")

    def test_different_units_concatenate_instead_of_guessing(self):
        self.assertEqual(app.merge_qty("8 oz", "1", "lb"), "8 oz + 1 lb")

    def test_missing_sides_never_produce_the_word_none(self):
        self.assertEqual(app.merge_qty(None, "2", None), "2")
        self.assertEqual(app.merge_qty("1", None, None), "1")
        self.assertIsNone(app.merge_qty(None, None, None))
        for result in (app.merge_qty(None, "2", None), app.merge_qty("4 each", None, None)):
            self.assertNotIn("None", result)

    def test_unitless_counts_sum(self):
        # "jar pasta sauce | 1", "large eggs | 2" are real list rows.
        self.assertEqual(app.merge_qty("2", "2", None), "4")

    def test_no_float_noise(self):
        self.assertEqual(app.merge_qty("0.1 cup", "0.2", "cup"), "0.3 cup")


class ParseNeededQtyTests(unittest.TestCase):
    def test_real_list_quantities(self):
        self.assertEqual(app._parse_needed_qty("9 lb"), (9.0, "lb"))
        # Real row "shredded cheese | 8 oz": prices are per lb, so 8 oz is 0.5 lb.
        # Before #70 this returned (None, None) and package fitting skipped it.
        self.assertEqual(app._parse_needed_qty("8 oz"), (0.5, "lb"))
        self.assertEqual(app._parse_needed_qty("2 qt"), (0.5, "gal"))

    def test_unparseable_is_skipped_not_guessed(self):
        for qty in (None, "", "1", "8 oz + 1 lb", "a handful"):
            self.assertEqual(app._parse_needed_qty(qty), (None, None), qty)


class PackageFitTests(unittest.TestCase):
    def _match(self, price, unit_price, unit):
        return {"price": price, "unit_price": unit_price, "unit": unit}

    def test_needs_several_packages(self):
        # A 1-lb pack at $5.99/lb against a 9 lb need (the real "Ground Beef | 9 lb").
        m = self._match(5.99, 5.99, "lb")
        app._annotate_package_fit(m, 9.0, "lb")
        self.assertEqual(m["packages_needed"], 9)
        self.assertAlmostEqual(m["total_cost"], 53.91)

    def test_rounds_up_not_down(self):
        m = self._match(10.0, 2.0, "lb")  # 5 lb pack
        app._annotate_package_fit(m, 5.1, "lb")
        self.assertEqual(m["packages_needed"], 2)

    def test_falls_back_to_one_package_when_it_cannot_fit(self):
        for needed, unit, m in [
            (None, None, self._match(3.0, 1.0, "lb")),
            (2.0, "lb", self._match(3.0, 1.0, "gal")),   # unit mismatch
            (2.0, "lb", self._match(3.0, None, "lb")),   # no unit price
        ]:
            app._annotate_package_fit(m, needed, unit)
            self.assertEqual((m["packages_needed"], m["total_cost"]), (1, 3.0))


class FormatAmountTests(unittest.TestCase):
    def test_display(self):
        for value, text in [(3.0, "3"), (6.5, "6.5"), (0.1 + 0.2, "0.3"), (1 / 3, "0.3333"),
                            (1000.0, "1000"), (0, "0"), (0.00001, "0")]:
            self.assertEqual(app._format_amount(value), text)


class ScalingTests(unittest.TestCase):
    def test_scale_factor(self):
        self.assertEqual(app._scale_factor(8, 4), 2.0)
        for slot, base in [(None, 4), (4, None), (0, 4), (4, 0), ("x", 4)]:
            self.assertEqual(app._scale_factor(slot, base), 1.0)

    def test_scaled_row(self):
        row = {"name": "ground beef", "amount": "1.5", "unit": "lb", "slot_servings": 8, "recipe_servings": 4}
        self.assertEqual(app._scale_ingredient_row(row)["amount"], "3")

    def test_non_numeric_amount_passes_through(self):
        row = {"name": "salt", "amount": "a pinch", "unit": None, "slot_servings": 8, "recipe_servings": 4}
        self.assertEqual(app._scale_ingredient_row(row)["amount"], "a pinch")

    def test_scaling_has_no_float_noise(self):
        # 0.5 tsp pepper (real) scaled 6 -> 4 servings is 1/3 tsp.
        row = {"name": "pepper", "amount": "0.1", "unit": "tsp", "slot_servings": 3, "recipe_servings": 1}
        self.assertEqual(app._scale_ingredient_row(row)["amount"], "0.3")


class CombineRowsTests(unittest.TestCase):
    def test_same_ingredient_across_recipes_sums_case_insensitively(self):
        # Real rows: "5 lb Ground Beef" and "1.5 lb ground beef" in two recipes.
        rows = [
            {"name": "Ground Beef", "amount": "5", "unit": "lb"},
            {"name": "ground beef", "amount": "1.5", "unit": "lb"},
            {"name": "garlic", "amount": "2", "unit": "clove"},
        ]
        combined = {c["name"].lower(): c for c in app._combine_ingredient_rows(rows)}
        self.assertEqual(combined["ground beef"]["amount"], "6.5")
        self.assertEqual(combined["garlic"]["amount"], "2")

    def test_different_units_stay_separate(self):
        rows = [{"name": "flour", "amount": "2", "unit": "cup"}, {"name": "flour", "amount": "1", "unit": "lb"}]
        self.assertEqual(len(app._combine_ingredient_rows(rows)), 2)

    def test_non_numeric_joins_instead_of_guessing(self):
        rows = [{"name": "salt", "amount": "1", "unit": None}, {"name": "salt", "amount": "a pinch", "unit": None}]
        self.assertEqual(app._combine_ingredient_rows(rows)[0]["amount"], "1 + a pinch")

    def test_no_float_noise_in_sums(self):
        rows = [{"name": "milk", "amount": "0.1", "unit": "cup"}, {"name": "milk", "amount": "0.2", "unit": "cup"}]
        self.assertEqual(app._combine_ingredient_rows(rows)[0]["amount"], "0.3")


class _PantryCur:
    def __init__(self, pantry):
        self.pantry, self.row = pantry, None

    def execute(self, sql, params):
        self.row = self.pantry.get(params[0].lower())

    def fetchone(self):
        return self.row


class ApplyPantryTests(unittest.TestCase):
    def test_subtracts_same_unit_and_clamps_at_zero(self):
        combined = [
            {"name": "ground beef", "amount": "1.5", "unit": "lb"},
            {"name": "flour", "amount": "2", "unit": "cup"},
        ]
        cur = _PantryCur({"ground beef": {"amount": 1, "unit": "lb"}, "flour": {"amount": 5, "unit": "cup"}})
        by = {c["name"]: c for c in app.apply_pantry(cur, combined)}
        self.assertEqual(by["ground beef"]["need_amount"], "0.5")
        self.assertEqual(by["flour"]["need_amount"], "0")

    def test_unit_mismatch_is_a_full_need(self):
        combined = [{"name": "flour", "amount": "2", "unit": "cup"}]
        cur = _PantryCur({"flour": {"amount": 5, "unit": "lb"}})
        self.assertEqual(app.apply_pantry(cur, combined)[0]["need_amount"], "2")
        self.assertIsNone(combined[0]["pantry_have"])

    def test_no_float_noise_in_what_is_left(self):
        combined = [{"name": "milk", "amount": "1.5", "unit": "cup"}]
        cur = _PantryCur({"milk": {"amount": 1.2, "unit": "cup"}})
        self.assertEqual(app.apply_pantry(cur, combined)[0]["need_amount"], "0.3")


class WeekStartTests(unittest.TestCase):
    def test_sunday_on_or_before(self):
        self.assertEqual(app.week_start_for(datetime.date(2026, 9, 29)), datetime.date(2026, 9, 27))  # Tue
        self.assertEqual(app.week_start_for(datetime.date(2026, 9, 27)), datetime.date(2026, 9, 27))  # Sun
        self.assertEqual(app.week_start_for(datetime.date(2026, 10, 3)), datetime.date(2026, 9, 27))  # Sat
        self.assertEqual(app.week_start_for(datetime.date(2027, 1, 1)), datetime.date(2026, 12, 27))  # year edge


class PastedRecipeTests(unittest.TestCase):
    def test_headed_recipe(self):
        name, servings, notes, ingredients = app.parse_pasted_recipe(
            "Spaghetti Night\nServes 4\nIngredients\n1 lb spaghetti\n1 lb ground beef\n1 jar pasta sauce\n"
            "Instructions\nBoil the pasta.\n"
        )
        self.assertEqual((name, servings), ("Spaghetti Night", "4"))
        self.assertEqual(ingredients.splitlines(), ["1 lb spaghetti", "1 lb ground beef", "1 jar pasta sauce"])
        self.assertEqual(notes, "Boil the pasta.")

    def test_unheaded_recipe_uses_leading_amounts(self):
        name, _, notes, ingredients = app.parse_pasted_recipe("Pancakes\n2 cup flour\n1.5 cup milk\nMix and fry.")
        self.assertEqual(name, "Pancakes")
        self.assertEqual(ingredients.splitlines(), ["2 cup flour", "1.5 cup milk"])
        self.assertEqual(notes, "Mix and fry.")


if __name__ == "__main__":
    unittest.main()
