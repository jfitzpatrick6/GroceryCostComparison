"""
Tests for BJs.py's per-product parse and its per-run accounting (#96).

Why this module exists: BJs.py used to parse each product inside a bare
`except Exception as e: print(e); print(product)`. A product it could not price
therefore vanished with no count and no identity, so a catalogue that quietly
lost products was indistinguishable in the data from a catalogue that was simply
smaller. That is not hypothetical - it cost a full misdiagnosis. A 3,137-item
scrape was compared against a 6,204-item "baseline" and read as the scraper
losing half of BJs, when the baseline turned out to be two same-day runs summed
in `grocery_prices` (see the commit body for #96; the real single-run baseline
was 3,102). Nothing in the scraper's output would have distinguished those
cases, so these tests pin down the output that does.

What is covered:
  - parse_product(): the club price is taken from the club key and never from
    `online`; an online-only listing is skipped and *counted*, not priced; the
    weighted-item path from #16/#51 still produces the midpoint estimate.
  - collect_products(): every product is accounted for as parsed or skipped, and
    a walk that ends before the API's own declared catalogue size is recorded as
    `stopped_early` rather than looking like a complete smaller scrape.
  - report(): the truncated walk produces an unmistakable warning, a complete
    one does not, and no log line ever contains the club id.

Fixtures are real products captured from BJs' live browse API on 2026-09-28
while investigating #96, not invented examples (CONTRIBUTING §7). They are
trimmed to the fields the parse reads plus the availability flags that explain
why a product has no club price. **The club id has been replaced with `9999`**:
BJs keys `prices` by club, and club ids are location-identifying (§9). `CLUB`
below is the placeholder the tests pass as `store`; it is not a real value and
must not be "restored" to one.

No database and no network - `parse_product`/`collect_products`/`report` are
pure, and BJs.py imports pandas only inside main(), which these tests do not
call (CONTRIBUTING §7, §11). Run with:

    pytest Groceries/test_bjs.py -v
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import BJs

# Placeholder club id - see the module docstring. Real BJs club ids are
# 4-digit strings; 9999 keeps the shape without being location-identifying.
CLUB = "9999"


def _page(products, total=None):
    """Build a browse-API response body around a list of products.

    Shape matches the live capture: results under `response.results`, catalogue
    size under `response.total_num_results`. `total` defaults to the number of
    products given, which is what a single-page catalogue looks like.
    """
    return {
        "response": {
            "results": products,
            "total_num_results": len(products) if total is None else total,
        }
    }


# --- Real captured products, 2026-09-28, club id replaced with 9999 ---------

# Club and online prices differ on the majority of products, and this one shows
# it starkly: $18.49 in the club, $23.99 to ship. It is the reason
# parse_product() must never fall back to the `online` value - see
# NO_CLUB_PRICE_PRODUCT below.
PRICED_PRODUCT = {
    "value": "Frito-Lay Variety Pack of Snacks and Chips, 30 ct./1.5-2 oz.",
    "data": {
        "id": "290682",
        "out_of_stock": "N",
        "avail_online": "Y",
        "avail_in_club": "Y",
        "facets": [
            {"name": "avg_rating", "values": [5]},
            {"name": "ebt_eligible", "values": ["Y"]},
            {"name": "max_price", "values": [23.99]},
            {"name": "min_price", "values": [18.49]},
            {"name": "weighted_item", "values": ["N"]},
        ],
        "prices": {"online": {"value": "23.99"}, CLUB: {"value": "18.49"}},
    },
}

# The by-weight case from #16/#51, and the exact product CONTRIBUTING §6 uses
# as its worked example: a 4.5-6.5 lb pack of chicken breast at $2.19-2.69/lb.
WEIGHTED_PRODUCT = {
    "value": "Wellsley Farms Boneless Skinless Chicken Breasts, 4.5-6.5 lbs.",
    "data": {
        "id": "980423",
        "out_of_stock": "N",
        "avail_online": "N",
        "avail_in_club": "Y",
        "attr": {
            "spec_badge": "Perfect For Pan Searing!",
            "maxpackweight": "6.5",
            "minpackweight": "4.50",
        },
        "facets": [
            {"name": "avg_rating", "values": [5]},
            {"name": "ebt_eligible", "values": ["Y"]},
            {"name": "max_price", "values": [2.69]},
            {"name": "min_price", "values": [2.19]},
            {"name": "weighted_item", "values": ["Y"]},
        ],
        "prices": {CLUB: {"value": "2.19"}},
    },
}

# The product #96 was filed about. It is one of the two that the original scrape
# log dumped verbatim (id 342930), still online-only six weeks later:
# avail_in_club "N", and `prices` holds an `online` value with no club value.
NO_CLUB_PRICE_PRODUCT = {
    "value": "Augason Farms Peanut Butter Powder Can, 32 oz.",
    "data": {
        "id": "342930",
        "out_of_stock": "N",
        "avail_online": "Y",
        "avail_in_club": "N",
        "facets": [
            {"name": "ebt_eligible", "values": ["N"]},
            {"name": "max_price", "values": [24.99]},
            {"name": "min_price", "values": [24.99]},
            {"name": "weighted_item", "values": ["N"]},
        ],
        "prices": {"online": {"value": "24.99"}},
    },
}

# A second real online-only listing, captured in the same sample. Two independent
# products matter here: the skip has to be a category of product, not one
# malformed record.
NO_CLUB_PRICE_PRODUCT_2 = {
    "value": "Wise Company 372-Serving Ultimate Preparedness Pack with Seychelle Water Bottle",
    "data": {
        "id": "169693",
        "out_of_stock": "N",
        "avail_online": "Y",
        "avail_in_club": "N",
        "facets": [
            {"name": "avg_rating", "values": [5]},
            {"name": "ebt_eligible", "values": ["N"]},
            {"name": "max_price", "values": [349.99]},
            {"name": "min_price", "values": [349.99]},
            {"name": "weighted_item", "values": ["N"]},
        ],
        "prices": {"online": {"value": "349.99"}},
    },
}


# A weighted item BJs lists with a maxpackweight and NO minpackweight. Real,
# and found only because #96 made skips countable: a full live walk on
# 2026-09-28 hit exactly two of these in 3,135 products (this pork tenderloin
# and a 1.25-2 lb Muenster). Field values are verbatim from that run's skip
# output, which is the API's own payload for this product.
WEIGHTED_PRODUCT_MISSING_MIN_WEIGHT = {
    "value": "Wellsley Farms Boneless Pork Tenderloin,  3.5-6 lbs.",
    "data": {
        "id": "981070",
        "attr": {
            "free_shipping": "N",
            "maxpackweight": "6.00",
            "showpricecartpostlogin": "N",
        },
        "facets": [
            {"name": "avg_rating", "values": [5]},
            {"name": "ebt_eligible", "values": ["Y"]},
            {"name": "max_price", "values": [3.69]},
            {"name": "min_price", "values": [2.39]},
            {"name": "weighted_item", "values": ["Y"]},
        ],
        "prices": {CLUB: {"value": "2.39"}},
    },
}


class ParseProductTests(unittest.TestCase):
    def test_club_price_is_used_and_not_the_online_price(self):
        # The live capture has this product at $18.49 in club and $23.99
        # online - a 30% spread on a real shelf item. Reading `online` would
        # not look broken in the data; it would just make BJs look expensive
        # on exactly the products it is cheap on.
        row, skip = BJs.parse_product(PRICED_PRODUCT, CLUB)
        self.assertIsNone(skip)
        self.assertEqual(row["Price"], 18.49)
        self.assertEqual(row["Product"], PRICED_PRODUCT["value"])

    def test_size_parsing_survives_the_move_out_of_main(self):
        # #96 lifted this parse out of main() unchanged. These values are what
        # the pre-existing regexes produce for these real product names, pinned
        # so the extraction is provably behaviour-preserving: "30 ct." is a
        # count, and the rate helper renders it per item.
        row, _ = BJs.parse_product(PRICED_PRODUCT, CLUB)
        self.assertEqual(row["Size"], "30 ct")
        self.assertEqual(row["Rate"], "$0.62 per item")

    def test_weighted_item_uses_the_midpoint_estimate(self):
        # #16/#51: min_price/max_price on a weighted item are per-pound rates,
        # not a package total, so the stored Price has to be rate x weight or
        # calculate_rate_per_unit() divides by weight twice. Midpoints of the
        # real ranges: (2.19+2.69)/2 = 2.44 $/lb over (4.50+6.5)/2 = 5.5 lb.
        row, skip = BJs.parse_product(WEIGHTED_PRODUCT, CLUB)
        self.assertIsNone(skip)
        self.assertEqual(row["Size"], "5.5 lb")
        self.assertAlmostEqual(row["Price"], 13.42)
        self.assertEqual(row["Rate"], "$2.44 per lb")

    def test_a_one_sided_weight_range_is_skipped_and_names_the_missing_field(self):
        # Real product, real gap (#96). BJs does not always supply both ends of
        # a weighted item's pack-weight range. Guessing from the max alone
        # would invent a package weight the listing never stated and turn it
        # into a $/lb that /list/where-to-buy trusts, so this stays a skip -
        # but the reason has to say *which* field was absent, because the
        # previous message dumped the whole facets and attrs dicts instead
        # (hundreds of characters per failure, the complaint #96 was filed on).
        row, skip = BJs.parse_product(WEIGHTED_PRODUCT_MISSING_MIN_WEIGHT, CLUB)
        self.assertIsNone(row)
        self.assertEqual(skip, "ValueError: weighted item missing minpackweight")
        self.assertNotIn("avg_rating", skip, "skip reason must not dump the payload")

    def test_no_club_price_is_skipped_with_a_named_reason(self):
        # The #96 regression. This product used to raise KeyError inside the
        # broad except, print the bare club id, dump the whole API object, and
        # disappear without being counted. It must now come back as a skip with
        # a reason the run can total up.
        row, skip = BJs.parse_product(NO_CLUB_PRICE_PRODUCT, CLUB)
        self.assertIsNone(row)
        self.assertEqual(skip, BJs.NO_STORE_PRICE)

    def test_no_club_price_is_not_substituted_with_the_online_price(self):
        # Skipping is a deliberate choice, not an oversight, and this is the
        # assertion that stops it being "fixed" by falling back to `online`.
        # $24.99 is BJs' ship-to-home price for this item; /list/where-to-buy
        # compares what it costs to buy in a store, so recording it as a club
        # price would be a confident wrong answer (CONTRIBUTING §6).
        row, _ = BJs.parse_product(NO_CLUB_PRICE_PRODUCT, CLUB)
        self.assertIsNone(row, "an online-only product must produce no price row at all")

    def test_malformed_product_is_a_counted_skip_not_an_exception(self):
        # The broad guard stays (#24) - one bad record must not abort a
        # 20-minute scrape - but it now yields a reason instead of silence.
        row, skip = BJs.parse_product({}, CLUB)
        self.assertIsNone(row)
        self.assertIn("KeyError", skip)

    def test_no_skip_reason_or_log_line_contains_the_club_id(self):
        # CONTRIBUTING §9: store ids are location-identifying and must not
        # reach a log. The old `print(e)` on a missing club price printed
        # exactly that, because a KeyError's message is its key. Asserted on
        # both the reason string and report()'s whole output.
        _, skip = BJs.parse_product(NO_CLUB_PRICE_PRODUCT, CLUB)
        self.assertNotIn(CLUB, skip)
        _, stats = BJs.collect_products([_page([NO_CLUB_PRICE_PRODUCT])], CLUB)
        with redirect_stdout(io.StringIO()) as out:
            BJs.report("BJs", stats)
        self.assertNotIn(CLUB, out.getvalue())


class CollectProductsAccountingTests(unittest.TestCase):
    def test_every_product_is_either_parsed_or_counted_as_skipped(self):
        # The invariant #96 exists for. Before this, the only number available
        # was len(df) - the count of products that *survived* - so a run that
        # dropped 1,500 products and a catalogue 1,500 products smaller were
        # the same number.
        products = [
            PRICED_PRODUCT,
            NO_CLUB_PRICE_PRODUCT,
            WEIGHTED_PRODUCT,
            NO_CLUB_PRICE_PRODUCT_2,
            {},
        ]
        rows, stats = BJs.collect_products([_page(products)], CLUB)
        self.assertEqual(stats["fetched"], len(products))
        self.assertEqual(len(rows), stats["parsed"])
        self.assertEqual(stats["parsed"] + stats["skipped"], stats["fetched"])
        self.assertEqual(stats["skipped_by_reason"][BJs.NO_STORE_PRICE], 2)
        self.assertEqual(stats["skipped"], 3)

    def test_skip_examples_name_the_products_concisely(self):
        # "identifies the product concisely (id and name) instead of dumping
        # the whole API object". Both real online-only items must be named by
        # id, and the example must be one short line, not the payload.
        _, stats = BJs.collect_products(
            [_page([NO_CLUB_PRICE_PRODUCT, NO_CLUB_PRICE_PRODUCT_2])], CLUB
        )
        examples = stats["skip_examples"][BJs.NO_STORE_PRICE]
        self.assertEqual(len(examples), 2)
        self.assertIn("id=342930", examples[0])
        self.assertIn("Augason Farms Peanut Butter Powder", examples[0])
        for line in examples:
            self.assertLess(len(line), 120, f"skip example is not concise: {line!r}")

    def test_skip_examples_are_capped(self):
        # The other half of the original complaint: hundreds of characters per
        # failure burying a scrape log. Ten examples per reason, then a count.
        products = [dict(NO_CLUB_PRICE_PRODUCT, data=dict(NO_CLUB_PRICE_PRODUCT["data"],
                                                          id=str(i))) for i in range(50)]
        _, stats = BJs.collect_products([_page(products)], CLUB)
        self.assertEqual(stats["skipped_by_reason"][BJs.NO_STORE_PRICE], 50)
        self.assertEqual(len(stats["skip_examples"][BJs.NO_STORE_PRICE]), BJs._MAX_SKIP_EXAMPLES)
        with redirect_stdout(io.StringIO()) as out:
            BJs.report("BJs", stats)
        self.assertIn("and 40 more skipped", out.getvalue())

    def test_a_walk_shorter_than_the_declared_catalogue_is_flagged(self):
        # The scenario the issue comment raised as a candidate cause: a run
        # that stops early "without reporting it", because collector.py counts
        # what it inserted and a short walk looks like a successful smaller
        # scrape. `total_num_results` is already in every browse response, so
        # this costs nothing to check. Verified live on 2026-09-28: the API
        # reported total_num_results 3130 and the walk reached 78 full pages of
        # 40 plus a final page of 10 - i.e. the numbers do agree when nothing
        # is wrong, which is what makes a disagreement meaningful.
        _, stats = BJs.collect_products(
            [_page([PRICED_PRODUCT] * 40, total=3130), None], CLUB
        )
        self.assertEqual(stats["fetched"], 40)
        self.assertEqual(stats["declared_total"], 3130)
        self.assertIsNotNone(stats["stopped_early"])
        with redirect_stdout(io.StringIO()) as out:
            BJs.report("BJs", stats)
        self.assertIn("WARNING", out.getvalue())
        self.assertIn("40 of the 3130", out.getvalue())

    def test_a_complete_walk_produces_no_warning(self):
        # The guard has to stay quiet when the scrape is fine, or it becomes
        # the kind of noise that trains people to ignore the log.
        _, stats = BJs.collect_products([_page([PRICED_PRODUCT, WEIGHTED_PRODUCT])], CLUB)
        self.assertEqual(stats["fetched"], stats["declared_total"])
        self.assertIsNone(stats["stopped_early"])
        with redirect_stdout(io.StringIO()) as out:
            BJs.report("BJs", stats)
        self.assertNotIn("WARNING", out.getvalue())

    def test_an_unfetchable_page_stops_the_walk_and_keeps_what_it_got(self):
        # `_fetch_page` yields None rather than raising, so a network failure
        # on page 50 of 79 keeps pages 1-49 and says so. Raising instead would
        # lose the whole store for the run (collector.py catches per store).
        rows, stats = BJs.collect_products(
            [_page([PRICED_PRODUCT], total=100), None], CLUB
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["stopped_early"], "page 2 could not be fetched")

    def test_the_empty_page_terminator_is_a_clean_end_not_a_truncation(self):
        # Live behaviour, measured for #96: with 3130 products at 40 per page,
        # page 79 returns the final 10 and page 80 returns `results: []` with
        # total_num_results still 3130. That empty page is the API's own end
        # marker and must not be reported as a truncated walk.
        pages = [_page([PRICED_PRODUCT] * 40, total=50), _page([PRICED_PRODUCT] * 10, total=50),
                 _page([], total=50)]
        _, stats = BJs.collect_products(pages, CLUB)
        self.assertEqual(stats["fetched"], 50)
        self.assertIsNone(stats["stopped_early"])
        with redirect_stdout(io.StringIO()) as out:
            BJs.report("BJs", stats)
        self.assertNotIn("WARNING", out.getvalue())

    def test_a_page_bodies_generator_is_not_drained_past_the_terminator(self):
        # _page_bodies() is lazy on purpose: collect_products() breaking on the
        # empty page must not leave a request in flight for a page nobody reads.
        requested = []

        def bodies():
            for n in range(1, 100):
                requested.append(n)
                yield _page([PRICED_PRODUCT], total=2) if n <= 2 else _page([], total=2)

        _, stats = BJs.collect_products(bodies(), CLUB)
        self.assertEqual(stats["fetched"], 2)
        self.assertEqual(requested, [1, 2, 3], "walked past the end-of-catalogue page")



class ParseSizeTests(unittest.TestCase):
    """#73. Every name is verbatim from the 2026-09-29 catalogue export; the
    expected sizes were checked by hand against the name, and the old outputs
    are noted where they were wrong."""

    def test_multipacks_multiply_out(self):
        for name, expected in [
            # old: 12 oz - one bag priced as all six
            ("Diana Dry Lentils, 6 Bags/12 oz.", "72 oz"),
            # old: "3 pk" - no period before the slash
            ("Galbani Fresh Mozzarella Cheese Pouches, 3 pk/6 oz.", "18 oz"),
            # old: "24 pk" - space after the slash
            ("IBC Root Beer Made with Sugar Cane, 24 pk./ 12 oz.", "288 oz"),
            # old: "2 pk" - "fl oz" never matched "fl. oz"
            ("La Colombe Unsweetened Brazilian Cold Brew Coffee, 2 pk./42 fl oz.", "84 fl oz"),
            ("Oh Snap! Dilly Bites Classic Dill Pickle Snack Packs, 12 ct./3.25 fl.oz.", "39 fl oz"),
            # old: "18 ct" - ml was not a unit
            ("Vita Coco Coconut Water, 18 ct./330 ml.", "5940 ml"),
            # the old regex already handled these; they must not regress
            ("Wellsley Farms Premium Chunk Chicken Breast in Water, 6 ct./12.5 oz.", "75 oz"),
            ("Perdue No Antibiotics Ever Breaded Chicken Breast Nuggets, 3 pk./0.75 lb.", "2.25 lb"),
            ("Coca-Cola Soda Soft Drink, Bottles, 4 pk./2 Liters", "8 l"),
            ("Lotus Biscoff Cookies, 32 ct./2 pk.", "64 pk"),
        ]:
            self.assertEqual(BJs.parse_size(name), expected, name)

    def test_no_size_read_out_of_a_fraction(self):
        # old: "3 lb", from "1/3 lb"
        self.assertEqual(
            BJs.parse_size("Butterball Frozen Turkey Burgers, Original Seasoned, 1/3 lb. Patties, 12 ct."),
            "12 ct",
        )

    def test_size_first_names_are_not_multiplied(self):
        # old: "378.0 ct" of bacon
        self.assertEqual(BJs.parse_size("Hormel Black Thick Cut Fully Cooked Bacon, 10.5 oz./36 ct."), "10.5 oz")

    def test_per_item_range_falls_back_to_the_count(self):
        self.assertEqual(BJs.parse_size("Frito-Lay Variety Pack of Snacks and Chips, 30 ct./1.5-2 oz."), "30 ct")

    def test_a_period_is_not_a_number(self):
        # old: ". l" from "Co. Little", which units.py cannot parse
        self.assertEqual(BJs.parse_size("The Little Potato Co. Little Yellows, 3 lbs."), "3 lb")

    def test_a_weight_range_is_no_size_not_its_upper_bound(self):
        self.assertEqual(
            BJs.parse_size("Wellsley Farms Fresh Pork St. Louis Style Spare Ribs, 5-8.5 lbs."), "N/A"
        )

    def test_plain_sizes_and_no_size(self):
        self.assertEqual(BJs.parse_size("Tyson Boneless Skinless Chicken Breast, 10 lbs."), "10 lb")
        self.assertEqual(BJs.parse_size("Wellsley Farms 1/2 Sheet Gold & Chocolate Base Cake, Serves 32"), "N/A")


class BrowseRequestTests(unittest.TestCase):
    """#72: the key comes from the environment, and the request still asks for
    this club's prices."""

    def test_no_constructor_key_literal_in_any_scraper_source(self):
        # Constructor.io keys are "key_" plus 16 alphanumerics; {16,} avoids
        # tripping on identifiers like key_fingerprint. Scans every .py under
        # Groceries/, not just BJs.py - a copy in an old test script is how the
        # key survived the first pass of #72.
        import pathlib
        root = pathlib.Path(BJs.__file__).parent
        for path in root.rglob("*.py"):
            self.assertNotRegex(path.read_text(), r"key_[A-Za-z0-9]{16,}", str(path))

    def test_first_page_failure_is_a_failure_not_zero_items(self):
        # A wrong/rotated key: the API 401s on page 1.
        with mock.patch.dict("os.environ", {"BJS_CNSTRC_KEY": "k"}), \
                mock.patch.object(BJs, "_fetch_page", lambda store, page, key: None), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "BJS_CNSTRC_KEY"):
                BJs.main("9999")

    def test_missing_key_fails_before_any_request(self):
        with mock.patch.dict("os.environ", {"BJS_CNSTRC_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "BJS_CNSTRC_KEY"):
                BJs.browse_key()

    def test_params_request_this_clubs_price_fields_and_no_frozen_session(self):
        params = BJs._browse_params("9999", 3, "k")
        hidden = [v for k, v in params if k == "fmt_options[hidden_fields]"]
        self.assertIn("prices.9999", hidden)
        self.assertIn("prices.online", hidden)
        names = {k for k, _ in params}
        self.assertNotIn("i", names)  # the frozen session that duplicated products
        self.assertIn(("page", 3), params)
        self.assertIn(("key", "k"), params)


if __name__ == "__main__":
    unittest.main()
