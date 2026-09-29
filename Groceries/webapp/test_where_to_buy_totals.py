"""Renders where_to_buy.html's trip-total block and checks which option gets
marked "cheapest" (#101).

This is decision logic, not decoration: the card carrying `is-best` is the one the
household reads as the recommendation. It lives in the template because it needs
`store_totals` and `split_total` together, and a presentation-only change was not
supposed to grow route logic - but "in a template" is not the same as "untestable",
and an untested recommendation is exactly the failure mode this project exists to
avoid.

The rule under test: an option that is cheaper only because it cannot supply the
whole list must never be marked best. A store missing items has a lower total for
a smaller basket, which is not a cheaper trip.
"""

import re
import unittest

import app


def _totals_html(split_total, store_totals):
    with app.app.test_request_context("/list/where-to-buy"):
        html = app.app.jinja_env.get_template("where_to_buy.html").render(
            per_item=[{"item": {"name": "x", "qty": ""},
                       "cheapest": {"product": "P", "store": "Tops", "price": 1.0,
                                    "size": "1", "unit_price": 1.0, "unit": "ea",
                                    "packages_needed": 1, "total_cost": 1.0,
                                    "match_score": 0.9}}],
            unmatched=[], split_total=split_total, store_totals=store_totals,
            freshness=None, low_confidence=0.45,
            all_profile_names=[], active_profile_name=None,
        )
    return html


def _best_labels(html):
    """The label text of every totals card that carries is-best."""
    out = []
    for block in re.findall(r'<div class="totals__item is-best">(.*?)</div>\s*</div>', html, re.S):
        m = re.search(r'totals__label">(.*?)</div>', block, re.S)
        out.append(re.sub(r"\s+", " ", m.group(1)).strip() if m else block[:40])
    return out


def _store(store, total, covered, of_total):
    return {"store": store, "total": total, "covered": covered, "of_total": of_total,
            "covers_all": covered == of_total}


class TripTotalBestOptionTests(unittest.TestCase):
    def test_a_store_that_is_cheaper_only_because_it_is_missing_items_is_not_best(self):
        # BJs "wins" on price by not selling two of the three items. Marking it
        # best would recommend a trip that cannot complete the list.
        html = _totals_html(
            split_total=12.00,
            store_totals=[_store("BJs", 8.99, 1, 3), _store("Tops", 13.50, 3, 3)],
        )
        best = _best_labels(html)
        self.assertEqual(best, ["Split across cheapest stores"])
        self.assertIn("missing 2", html, "the incomplete basket must say so")

    def test_a_cheaper_full_coverage_store_beats_splitting(self):
        html = _totals_html(
            split_total=12.00,
            store_totals=[_store("Tops", 11.25, 3, 3), _store("Aldis", 14.00, 3, 3)],
        )
        self.assertEqual(_best_labels(html), ["Shop only at Tops"])

    def test_splitting_wins_when_no_single_store_covers_everything(self):
        html = _totals_html(
            split_total=9.50,
            store_totals=[_store("Tops", 10.00, 2, 3), _store("Aldis", 12.00, 2, 3)],
        )
        self.assertEqual(_best_labels(html), ["Split across cheapest stores"])

    def test_a_tie_between_full_coverage_stores_marks_both_not_neither(self):
        # Two stores at the same price both cover everything. Marking neither
        # would leave the page with no recommendation; marking one arbitrarily
        # would be a coin flip presented as an answer.
        html = _totals_html(
            split_total=15.00,
            store_totals=[_store("Tops", 12.00, 3, 3), _store("Aldis", 12.00, 3, 3)],
        )
        self.assertEqual(sorted(_best_labels(html)), ["Shop only at Aldis", "Shop only at Tops"])

    def test_exactly_one_card_is_best_when_there_is_a_clear_winner(self):
        html = _totals_html(
            split_total=20.00,
            store_totals=[_store("Tops", 18.00, 3, 3), _store("Aldis", 19.00, 3, 3),
                          _store("BJs", 7.00, 1, 3)],
        )
        self.assertEqual(_best_labels(html), ["Shop only at Tops"])


if __name__ == "__main__":
    unittest.main()
