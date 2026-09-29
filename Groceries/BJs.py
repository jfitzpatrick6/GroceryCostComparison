"""BJ's Club scraper: walks the club's Constructor.io browse catalogue.

Shape of a run (#96): page through `browse/group_id/grocery` until the API
returns an empty page, parse each product into a Product/Price/Rate/Size row,
and report what could not be parsed. The reporting is the point of the split
below - this scraper used to drop unparsable products with `print(e);
print(product)`, which is both unidentifiable and uncounted, so a catalogue
that quietly lost half its products was indistinguishable from a smaller
catalogue. That cost a full misdiagnosis: see the commit body for #96.

Why the parse/accounting functions are separate from the HTTP and pandas work,
and why pandas is imported inside main() rather than at module level: the
required test tier installs only the webapp's requirements (CONTRIBUTING §7,
§11), which deliberately exclude pandas and playwright. `parse_product` and
`collect_products` are the pieces whose failure mode is *silently losing
catalogue*, so they are the pieces that have to be regression-testable -
keeping them free of pandas is what allows `Groceries/test_bjs.py` to import
this module in CI. Same reasoning retention.py's module docstring gives for
existing separately from collector.py.
"""

import re

import requests

from units import calculate_rate_per_unit

# Size/unit vocabulary BJs embeds in product names ("... 16 oz", "Family Pack
# 2.5 lb"). Was spelled out three times inline in two regexes below; kept as one
# alternation with no capturing group of its own so the group numbering the
# callers depend on (match.group(1); calcmatch.group(1)/(3)/(4)) is unchanged.
_SIZE_UNITS = r"pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt"
# "16 oz" -> take the whole matched size phrase as-is.
_SIZE_RE = re.compile(rf"(([\d.]+)\s*({_SIZE_UNITS}))")
# "5 oz/12 count" style ratios -> multiply the two quantities into one size.
_SIZE_RATIO_RE = re.compile(rf"([\d.]+)\s*({_SIZE_UNITS}).\/([\d.]+)\s*({_SIZE_UNITS})")

ROW_COLUMNS = ["Product", "Price", "Rate", "Size"]

# A product this club does not price. Reason it gets its own label rather than
# falling into the generic error bucket: it is a *catalogue* fact (BJs lists it
# online-only) and not a parse failure, so it is the one skip count worth
# watching run over run. See parse_product() for why it is skipped rather than
# priced from the `online` value the API does return.
NO_STORE_PRICE = "no_store_price"

# How many example products to name per skip reason in the log. Uncapped output
# is what #96 was originally filed about - hundreds of characters per failure,
# burying everything else in a 20-minute scrape log.
_MAX_SKIP_EXAMPLES = 10


def _identity(product):
    """Concise product identity for log lines: id and a truncated name.

    Replaces `print(product)`, which dumped the entire API object (hundreds of
    characters, no product identity in the exception line above it). Never
    raises - this runs on the error path, where the product is by definition
    not shaped the way we expected.
    """
    data = product.get("data") if isinstance(product, dict) else None
    if not isinstance(data, dict):
        return f"<unparseable product: {str(product)[:40]}>"
    name = product.get("value")
    return f"id={data.get('id')} name={str(name)[:60]!r}"


def parse_product(product, store):
    """Parse one browse-API product into a row dict.

    Returns `(row, None)` on success, or `(None, reason)` when the product
    cannot be priced for this club. Never raises: one malformed product must
    not abort a 20-minute scrape (#24), so the broad guard stays - but the
    caller now gets a *reason* back instead of the product vanishing (#96).

    `store` is the club id, which BJs uses verbatim as the key into `prices` /
    `eligibility` / `inventory`. It is location-identifying: never log it (see
    CONTRIBUTING §9), which is why `_identity` reports the product id instead.
    """
    try:
        product_name = product['value']
        facets = {
            f['name']: f['values'][0]
            for f in product.get('data', {}).get('facets', [])
            if f.get('values')
        }

        if facets.get('weighted_item') == 'Y':
            # By-weight items (produce, fresh meat/poultry) don't carry a
            # correct `prices` entry for rate purposes - BJs only exposes a
            # min/max *per-pound rate* over the pack's min/max weight (see
            # #16, #51 - verified against live data: a "$2.19-2.69" range on a
            # 4.5-6.5 lb chicken breast pack is obviously $/lb, not a total
            # package price). There's no single "the" rate for these (real
            # packages vary), so this uses the midpoint of both ranges as an
            # honest estimate rather than pretending it's exact.
            attrs = product.get('data', {}).get('attr', {})
            min_price, max_price = facets.get('min_price'), facets.get('max_price')
            min_weight, max_weight = attrs.get('minpackweight'), attrs.get('maxpackweight')
            if None in (min_price, max_price, min_weight, max_weight):
                # Names the absent fields instead of dumping `facets` and
                # `attrs` wholesale. The dump was the same defect #96 was filed
                # about - hundreds of characters per failure, with the product
                # identified only by a separate print(e) above it - and it is
                # redundant now that _identity() names the product on the same
                # line. Naming the fields is also strictly more useful: it is
                # how the real gap below was found.
                missing = "/".join(
                    name for name, value in (
                        ("min_price", min_price), ("max_price", max_price),
                        ("minpackweight", min_weight), ("maxpackweight", max_weight),
                    ) if value is None
                )
                # Deliberately still a skip, not a guess. Observed live on
                # 2026-09-28 (#96): BJs ships some weighted items with a
                # maxpackweight and NO minpackweight - a 3.5-6 lb pork
                # tenderloin and a 1.25-2 lb Muenster, 2 of 3,135 products.
                # Weighting those from the max alone would invent a package
                # weight the listing does not state, and the resulting $/lb
                # would be a confident wrong number in /list/where-to-buy
                # (CONTRIBUTING §6). Counted and named instead, so the gap is
                # visible; deciding whether a one-sided range is good enough
                # is a separate call with its own issue.
                raise ValueError(f"weighted item missing {missing}")
            avg_rate = (float(min_price) + float(max_price)) / 2
            avg_weight = (float(min_weight) + float(max_weight)) / 2
            # calculate_rate_per_unit() below derives $/lb from
            # (total price / size) - so product_price has to be the
            # *total* price for an average-weight package, not the
            # bare rate, or the rate gets divided by weight twice.
            product_price = avg_rate * avg_weight
            product_size = f"{avg_weight} lb"
        else:
            prices = product.get('data', {}).get('prices')
            if not isinstance(prices, dict) or store not in prices:
                # Online-only listing: `avail_in_club: "N"`, `prices` holds an
                # `online` value and no club value. Skipped deliberately, and
                # NOT priced from `online` - that is BJs' ship-to-home price, a
                # different fulfilment channel, and /list/where-to-buy compares
                # what it costs to walk into a store and buy the thing. Passing
                # a delivery price off as a club price is exactly the confident
                # wrong answer CONTRIBUTING §6 exists to prevent, so this
                # returns no answer and gets counted instead.
                #
                # Reachable at all because the browse URL's
                # `pre_filter_expression` ORs in `avail_stores=online` (and
                # `out_of_stock=Y`), so the query asks for products the parse
                # then rejects. Verified against live data for #96: 2 of 450
                # spread-sampled products were online-only (~0.4%, both
                # promotional/specialty - e.g. an emergency-foods peanut
                # butter powder), so this is a rounding error on the
                # catalogue, not a cause of shrinkage. Tightening that filter
                # would change which products we see at all, so it is left
                # alone here and filed separately.
                #
                # This branch also closes a small leak: the code used to reach
                # `prices[store]['value']` directly and let the broad except
                # print the exception, and a KeyError's message *is its key* -
                # so every online-only product wrote the bare club id to the
                # scrape log. Store ids are location-identifying and must not
                # appear in logs (CONTRIBUTING §9). Returning a reason string
                # with no key in it makes that unreachable rather than
                # something a future reader has to remember.
                return None, NO_STORE_PRICE
            product_price = prices[store]['value']
            product_price = float(product_price.strip().removeprefix("$"))
            name_for_size = product_name.lower().replace(',', '')
            match = _SIZE_RE.search(name_for_size)
            if match:
                calcmatch = _SIZE_RATIO_RE.search(name_for_size)
                if calcmatch:
                    total_qty = float(calcmatch.group(1).replace(',', ''))
                    total_qty *= float(calcmatch.group(3).replace(',', ''))
                    product_size = f"{total_qty} {calcmatch.group(4)}"
                else:
                    product_size = match.group(1)
            else:
                product_size = 'N/A'

        row = {
            "Product": product_name,
            "Price": product_price,
            "Rate": calculate_rate_per_unit(product_price, product_size),
            "Size": product_size,
        }
        return row, None
    except Exception as e:
        # Deliberate broad catch (#24, and CONTRIBUTING §10 on why BLE001 is
        # not enabled): a single malformed product must not discard the rest of
        # the catalogue. What #96 changed is that this is now *accounted for* -
        # the reason string carries the exception type and message, and the
        # caller counts it and names the product, instead of the product
        # disappearing behind a bare `print(e); print(product)`.
        #
        # Correcting the record here, because #96's own description guessed
        # wrong and the guess is plausible: it says `print(e)` for the missing
        # club price emits "'online'-style noise". It does not. The failing
        # lookup was `prices[store]`, so the KeyError's key is the *club id*,
        # and `print(e)` wrote the bare club id to the scrape log - a
        # location-identifying value, which §9 forbids. Reproduced rather than
        # inferred: the pre-fix parse, run verbatim over real captured products
        # on 2026-09-28, printed `'9999'` for a placeholder club id. The
        # explicit no-store-price branch above is what makes that path
        # unreachable.
        return None, f"{type(e).__name__}: {e}"


def collect_products(page_bodies, store):
    """Turn already-fetched browse pages into rows, accounting for every product.

    `page_bodies` is an iterable of parsed JSON response bodies in page order,
    or `None` for a page that could not be fetched. Walks until the API returns
    an empty page - its own end-of-catalogue signal, verified live for #96:
    with `total_num_results` 3130 at 40 per page, page 78 returned 40 results,
    page 79 returned the final 10, and page 80 returned 0.

    Returns `(rows, stats)` where `stats` carries the counts a run needs to be
    interpretable: how many products were fetched, how many became rows, how
    many were skipped and why, and what the API itself declared the catalogue
    size to be. `fetched` vs `declared_total` is the check that makes a
    truncated walk visible - collector.py counts what it *inserted*, so before
    this a short walk looked like a successful smaller scrape (#96).

    No HTTP and no pandas in here, so it is testable in the required tier.
    """
    rows = []
    skipped = {}
    examples = {}
    fetched = 0
    declared_total = None
    stopped_early = None

    for page, body in enumerate(page_bodies, start=1):
        if body is None:
            stopped_early = f"page {page} could not be fetched"
            break
        response = body.get('response') or {}
        if response.get('total_num_results') is not None:
            declared_total = response['total_num_results']
        products = response.get('results') or []
        if not products:
            break
        fetched += len(products)
        for product in products:
            row, reason = parse_product(product, store)
            if row is not None:
                rows.append(row)
                continue
            skipped[reason] = skipped.get(reason, 0) + 1
            bucket = examples.setdefault(reason, [])
            if len(bucket) < _MAX_SKIP_EXAMPLES:
                bucket.append(_identity(product))

    stats = {
        "fetched": fetched,
        "parsed": len(rows),
        "skipped": sum(skipped.values()),
        "skipped_by_reason": skipped,
        "skip_examples": examples,
        "declared_total": declared_total,
        "stopped_early": stopped_early,
    }
    return rows, stats


def report(store_label, stats):
    """Print one run's accounting beside collector.py's own per-store line.

    Warnings go to stdout like everything else in the scraper - there is no
    logging setup in this container, and collector.py's #33 timing lines are
    what an operator actually reads.
    """
    reasons = ", ".join(f"{reason}: {n}" for reason, n in sorted(stats["skipped_by_reason"].items()))
    declared = stats["declared_total"]
    print(
        f"[{store_label}] parsed {stats['parsed']} of {stats['fetched']} products fetched; "
        f"skipped {stats['skipped']}" + (f" ({reasons})" if reasons else "")
        + (f"; API declares {declared} in this catalogue" if declared is not None else "")
    )
    for reason, ids in sorted(stats["skip_examples"].items()):
        for identity in ids:
            print(f"[{store_label}] skipped ({reason}): {identity}")
        extra = stats["skipped_by_reason"][reason] - len(ids)
        if extra > 0:
            print(f"[{store_label}] ... and {extra} more skipped for {reason}")

    if declared is not None and stats["fetched"] < declared:
        # The loud one. A truncated walk is not a smaller catalogue: it means
        # products this club does sell are missing from grocery_prices, and
        # /list/where-to-buy then systematically favours whichever store's
        # scrape completed (#96). Deliberately a warning rather than an
        # exception - collector.py would drop the whole store on a raise, and
        # partial prices plus a visible warning beats no prices plus none.
        cause = stats["stopped_early"] or "the browse walk ended before the last page"
        print(
            f"[{store_label}] WARNING: fetched only {stats['fetched']} of the {declared} "
            f"products the API says this catalogue has - {cause}. This run's prices are "
            f"INCOMPLETE and will bias /list/where-to-buy against this store (#96)."
        )


def _browse_url(store, page):
    return f"https://ac.cnstrc.com/browse/group_id/grocery?c=ciojs-client-2.53.1&key=key_2i36vP8QTs3Ati4x&i=0a5f818b-0856-433f-a88f-6f097c36f09d&s=2&page={page}&num_results_per_page=40&&fmt_options%5Bhidden_fields%5D=prices.{store}&fmt_options%5Bhidden_fields%5D=sale_prices.{store}&fmt_options%5Bhidden_fields%5D=original_price.{store}&fmt_options%5Bhidden_fields%5D=eligibility.{store}&fmt_options%5Bhidden_fields%5D=inventory.{store}&fmt_options%5Bhidden_fields%5D=prices.online&fmt_options%5Bhidden_fields%5D=sale_prices.online&fmt_options%5Bhidden_fields%5D=original_price.online&fmt_options%5Bhidden_fields%5D=eligibility.online&fmt_options%5Bhidden_fields%5D=inventory.online&pre_filter_expression=%7B%22or%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22online%22%7D%2C%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22{store}%22%7D%2C%7B%22and%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22{store}%22%7D%2C%7B%22name%22%3A%22avail_sdd%22%2C%22value%22%3A%22{store}%22%7D%5D%7D%2C%7B%22name%22%3A%22out_of_stock%22%2C%22value%22%3A%22Y%22%7D%5D%7D&_dt=1738009529475"


def _fetch_page(store, page):
    """One browse page as parsed JSON, or None if it could not be read.

    Returning None rather than raising is what lets collect_products() report a
    truncated walk instead of losing the pages already fetched - collector.py
    would swallow a raise at store level and BJs would vanish from the run
    entirely (#24).
    """
    try:
        response = requests.get(_browse_url(store, page))
        if response.status_code != 200:
            print(f"[BJs] page {page}: HTTP {response.status_code}")
            return None
        return response.json()
    except Exception as e:
        print(f"[BJs] page {page}: {type(e).__name__}: {e}")
        return None


def _page_bodies(store):
    """Lazily fetch browse pages in order. Lazy so that collect_products()
    breaking on the API's empty-page terminator doesn't leave a request in
    flight for a page nobody will read."""
    page = 1
    while True:
        body = _fetch_page(store, page)
        yield body
        if body is None:
            return
        if not ((body.get('response') or {}).get('results') or []):
            return
        page += 1


def main(store):
    rows, stats = collect_products(_page_bodies(store), store)
    report("BJs", stats)

    # Imported here rather than at module level: the required test tier does not
    # install pandas (CONTRIBUTING §7, §11 - see the module docstring), and
    # everything above this line is testable without it.
    import pandas as pd

    if not rows:
        return pd.DataFrame(columns=ROW_COLUMNS)
    # One DataFrame from the accumulated rows. This used to build a 1-row
    # DataFrame per product and pd.concat() the lot - ~3,100 allocations and a
    # full copy per concat for this scraper alone. Checked equivalent rather
    # than assumed: driven side by side over real captured products (including
    # a rate-less one, so the mixed str/float Rate column is exercised) on the
    # container's pandas 2.2.1, both constructions give identical columns,
    # shape, index and dtypes (Product/Rate/Size object, Price float64), and
    # the empty-catalogue path matches too. That comparison cannot live in
    # test_bjs.py, which runs in the required tier where pandas is not
    # installed - see the module docstring.
    return pd.DataFrame(rows, columns=ROW_COLUMNS)
