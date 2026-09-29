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

import datetime
import json
import os
import re

import requests

from units import calculate_rate_per_unit

# Size/unit vocabulary BJs embeds in product names ("... 16 oz", "Family Pack
# 2.5 lb"). Was spelled out three times inline in two regexes below; kept as one
# alternation with no capturing group of its own, so _SIZE_RE's group(1) is the
# whole size phrase.
_SIZE_UNITS = r"pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt"
# "16 oz" -> take the whole matched size phrase as-is. The lookbehind stops it
# reading a size out of the tail of a fraction: "Butterball Frozen Turkey
# Burgers, 1/3 lb. Patties, 12 ct." was stored as "3 lb" (#73).
#
# The number is \d+(\.\d+)? rather than [\d.]+: the old class matched a bare
# ".", so "The Little Potato Co. Little Yellows, 3 lbs." parsed as ". l" (from
# "co. little") and units.py raised on float("."), losing the real 3 lb.
#
# "-" is in the lookbehind so a weight RANGE yields no size at all. Without it,
# "Spare Ribs, 5-8.5 lbs." read as "8.5 lb" - the upper bound, a confident
# wrong $/lb where the honest answer is none (CONTRIBUTING §6).
_SIZE_RE = re.compile(rf"(?<![\d./-])((\d+(?:\.\d+)?)\s*({_SIZE_UNITS}))")

# Multi-packs: "<count> <pack word> / <each size> <unit>" -> one total size.
# Built from every BJs name containing "/" in the 2026-09-29 catalogue (961 of
# 3,099), not from the pattern's intent (#73). The regex this replaces required
# exactly one character between the pack word and the slash, which happened to
# be the period in "pk./" and "ct./", so it matched 928 of those names - #73's
# "never matches" was wrong - but it missed every real variant below, and each
# miss fell back to the pack COUNT or the per-item size as if it were the total:
#   "6 Bags/12 oz."          -> stored 12 oz, real total 72 oz (6x the $/lb)
#   "3 pk/6 oz."             -> stored "3 pk" (no period before the slash)
#   "12 pk./ 2 oz."          -> stored "12 pk" (space after the slash)
#   "2 pk./42 fl oz."        -> stored "2 pk" ("fl. oz" in _SIZE_UNITS means
#                               fl + any char + " oz", so "fl oz" never matched)
#   "12 ct./3.25 fl.oz."     -> stored "12 ct"
#   "18 ct./330 ml."         -> stored "18 ct" (ml was not a unit here)
# Also kept working, which the old regex got right: "4 pk./2 Liters" (8 l) and
# "Lotus Biscoff Cookies, 32 ct./2 pk." (64 each). A range per item ("30 ct./
# 1.5-2 oz.") deliberately does not match: there is no single total, and the
# pack count is the honest fallback. Size-first names ("10.5 oz./36 ct.") don't
# match either and fall to the plain size; the old regex multiplied those into
# nonsense like "378 ct" of bacon.
_PACK_SIZE_RE = re.compile(
    r"(?<![\d./])(\d+(?:\.\d+)?)\s*(?:pk|ct|count|bags?|pack)\.?\s*/\s*"
    r"(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|oz|lbs?|ct|count|pk|gal|qt|ml|liters?|litres?|l)\b(?!\s*-)"
)


def parse_size(product_name):
    """Size string for a packaged BJs product, from its name. 'N/A' when none.

    Separate function so it is testable against real names (#73); the output
    feeds units.calculate_rate_per_unit / parse_unit_price unchanged.
    """
    name = product_name.lower().replace(',', '')
    pack = _PACK_SIZE_RE.search(name)
    if pack:
        count, each, unit = float(pack.group(1)), float(pack.group(2)), pack.group(3)
        if unit.startswith("fl"):
            unit = "fl oz"
        elif unit == "lbs":
            unit = "lb"
        elif unit.startswith("lit"):
            unit = "l"
        return f"{count * each:g} {unit}"
    match = _SIZE_RE.search(name)
    return match.group(1) if match else 'N/A'

ROW_COLUMNS = ["Product", "Price", "Rate", "Size", "Category", "RegularPrice"]


def club_sale_price(data, store, listed, today=None):
    """(price to compare, regular price or None) for one BJs product (#19).

    Measured live 2026-09-29: `prices.<club>` is the REGULAR club price; an
    active club sale is separate, in `sale_prices.<club>` as {salePrice,
    saleStart, saleEnd}. Reading only `prices` meant every BJs sale was
    compared at full price - Tyson panko popcorn chicken at $18.99 while on
    sale for $14.99 - biasing /list/where-to-buy against BJs. Some products
    instead show an already-reduced `prices` value with the old one in
    `original_price.<club>`.

    The sale applies only between its start and end dates (inclusive, by date),
    and only if it is actually lower. "Today" is the container's date, which is
    UTC: the 03:00 UTC scrape is ~23:00 Eastern the night before, so the UTC date
    is the local day these prices will be used (BJs sales run 00:00-23:59).

    A product can carry BOTH an already-reduced price (original_price) and an
    active sale - four did on 2026-09-29 (Skittles: original 13.99, listed
    6.98, sale 3.98). The compared price is the sale price, BJs' own declared
    club price; "was" is the highest earlier price BJs states (review of #19).
    `online` sale prices are never used - they are the ship-to-home channel,
    same reasoning as #96's no_store_price.
    """
    today = today or datetime.date.today()
    sale = (data.get('sale_prices') or {}).get(store) or {}
    try:
        sale_price = float(sale['salePrice'])
        start = datetime.date.fromisoformat(str(sale['saleStart'])[:10])
        end = datetime.date.fromisoformat(str(sale['saleEnd'])[:10])
    except (KeyError, TypeError, ValueError):
        sale_price = None
    original = (data.get('original_price') or {}).get(store) or {}
    try:
        was = float(str(original['value']).strip().removeprefix("$"))
    except (KeyError, TypeError, ValueError):
        was = None
    if sale_price is not None and start <= today <= end and 0 < sale_price < listed:
        return round(sale_price, 2), max(listed, was or 0)
    return listed, (was if was is not None and was > listed else None)

# group_ids under "grocery>" that are NOT departments - brand pages and
# cross-cutting collections a product also appears in. Observed live on
# 2026-09-29 (#114): a case of water carries 25 group ids, most of them
# seasonal ("seasonal>summer>heat-wave-prep"), plus grocery>wellsley-farms>...
# and grocery>beverages>water - only the last is its department.
_BJS_NON_DEPARTMENTS = {"wellsley-farms", "kids-grocery", "specialty-shops", "protein-and-nutrition"}


def bjs_category(group_ids):
    """Most specific real grocery department for a BJs product, as
    "Meat > Chicken", or None (#114). Deepest wins; ties keep API order."""
    best = None
    for gid in group_ids or []:
        parts = str(gid).split(">")
        if len(parts) < 2 or parts[0] != "grocery" or parts[1] in _BJS_NON_DEPARTMENTS:
            continue
        if best is None or len(parts) > len(best):
            best = parts
    if not best:
        return None
    # Word-by-word capitalize, not str.title(): title() capitalizes after an
    # apostrophe ("kid's" -> "Kid'S") - review of #114.
    return " > ".join(
        " ".join(w[:1].upper() + w[1:] for w in p.replace("-", " ").replace(" and ", " & ").split())
        for p in best[1:]
    )

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

        # Sale pricing for weighted items isn't handled: their price comes from
        # a per-lb min/max range, not prices/sale_prices (#16, #51).
        regular_price = None
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
            product_price = float(prices[store]['value'].strip().removeprefix("$"))
            product_price, regular_price = club_sale_price(product.get('data', {}), store, product_price)
            product_size = parse_size(product_name)

        row = {
            "Product": product_name,
            "Price": product_price,
            "Rate": calculate_rate_per_unit(product_price, product_size),
            "Size": product_size,
            "Category": bjs_category(product.get('data', {}).get('group_ids')),
            "RegularPrice": regular_price,
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


_BROWSE_URL = "https://ac.cnstrc.com/browse/group_id/grocery"

# Per-store fields the browse API only returns when asked for by name. Without
# `prices.<club>` there is no club price at all, so every product would be a
# no_store_price skip (#96); the `online` set is what makes that skip
# explainable in the log.
_HIDDEN_FIELD_KINDS = ("prices", "sale_prices", "original_price", "eligibility", "inventory")


def _browse_params(store, page, key):
    """The browse request as a readable parameter list (#72).

    Was one hand-percent-encoded 1,129-character f-string with the API key
    inline. Rebuilt field by field and checked live against that string: same
    total_num_results, and every product the old walk returned is returned by
    this one with identical price and facets (see the #72 commit). Page-by-page
    order is NOT comparable - the old URL didn't even agree with itself, which
    is the bug described next. A list of pairs rather than a dict because
    `fmt_options[hidden_fields]` repeats.

    `c` identifies the Constructor.io JS client version BJs' site uses and is
    kept as captured. The old URL also carried a browser session id `i`, a
    session count `s` and a `_dt` cache-buster, all frozen at capture time
    (2025-01-28). Dropping `i`/`s` is a FIX, not just cleanup - measured live
    2026-09-29 over full walks of 3,103 products: with the frozen session the
    API reorders results between page requests, so each walk returned 78-92
    duplicate products and silently missed as many (3,011 and 3,025 distinct in
    two walks; 3,021 with only `i`/`s` added back). Without them: 3,103 distinct
    in both walks. `_dt` alone made no difference and was dropped as dead.
    """
    params = [
        ("c", "ciojs-client-2.53.1"),
        ("key", key),
        ("page", page),
        ("num_results_per_page", 40),
    ]
    for owner in (store, "online"):
        for kind in _HIDDEN_FIELD_KINDS:
            params.append(("fmt_options[hidden_fields]", f"{kind}.{owner}"))
    # Which products to list: sold online, or stocked at this club (optionally
    # same-day-delivery eligible), or out of stock. Kept exactly as captured;
    # #96 notes it asks for online-only products the parse then skips, and that
    # tightening it is a separate decision.
    params.append(("pre_filter_expression", json.dumps({"or": [
        {"name": "avail_stores", "value": "online"},
        {"name": "avail_stores", "value": store},
        {"and": [
            {"name": "avail_stores", "value": store},
            {"name": "avail_sdd", "value": store},
        ]},
        {"name": "out_of_stock", "value": "Y"},
    ]}, separators=(",", ":"))))
    return params


def browse_key():
    """The Constructor.io key BJs' site uses, from BJS_CNSTRC_KEY (#72).

    A public client-side key - BJs ships it in browser JavaScript - but still
    configuration, not source (CONTRIBUTING §9): it can change without notice,
    and then fixing the scraper must be an .env edit, not a commit. Raises at
    the start of the BJs scrape rather than letting every page 401, so the run
    summary says exactly what is wrong.
    """
    key = os.getenv("BJS_CNSTRC_KEY")
    if not key:
        raise RuntimeError("BJS_CNSTRC_KEY is not set - see README's .env section")
    return key


def _fetch_page(store, page, key):
    """One browse page as parsed JSON, or None if it could not be read.

    Returning None rather than raising is what lets collect_products() report a
    truncated walk instead of losing the pages already fetched - collector.py
    would swallow a raise at store level and BJs would vanish from the run
    entirely (#24).
    """
    try:
        # A timeout, because without one a stalled connection never returns and
        # the None-means-truncated contract above never gets a chance to apply:
        # the whole scheduled run hangs instead of reporting a short walk.
        response = requests.get(_BROWSE_URL, params=_browse_params(store, page, key), timeout=60)
        if response.status_code != 200:
            print(f"[BJs] page {page}: HTTP {response.status_code}")
            return None
        return response.json()
    except Exception as e:
        print(f"[BJs] page {page}: {type(e).__name__}: {e}")
        return None


def _page_bodies(store, key):
    """Lazily fetch browse pages in order. Lazy so that collect_products()
    breaking on the API's empty-page terminator doesn't leave a request in
    flight for a page nobody will read."""
    page = 1
    while True:
        body = _fetch_page(store, page, key)
        yield body
        if body is None:
            return
        if not ((body.get('response') or {}).get('results') or []):
            return
        page += 1


def main(store):
    rows, stats = collect_products(_page_bodies(store, browse_key()), store)
    report("BJs", stats)
    if not rows and stats["stopped_early"]:
        # Nothing at all, because the very first page failed. Returning an empty
        # frame here reported "BJs ok (0 items)" and an OK run verdict (#63) -
        # which is exactly what a wrong or rotated BJS_CNSTRC_KEY looks like
        # (the API answers 401 on page 1). Raising makes collector.py record
        # "BJs FAILED: ..." instead. A walk that fetched something and then
        # stopped still returns its rows with #96's INCOMPLETE warning.
        raise RuntimeError(
            f"BJs returned no products ({stats['stopped_early']}). An HTTP 401/403 above "
            f"usually means BJS_CNSTRC_KEY is wrong or has been rotated."
        )

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
