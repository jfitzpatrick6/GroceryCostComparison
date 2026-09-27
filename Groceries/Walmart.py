import asyncio
import json
import math
import random
import re

import httpx
import pandas as pd
from parsel import Selector

from units import calculate_rate_per_unit

# Leaf grocery categories, chosen to cover the grocery department without
# overlapping each other and without relying on one huge listing that runs
# into Walmart's own per-request page cap (see #8 - the previous list also
# included a top-level "food-grocery" browse page that is a strict superset
# of every category below it, so every product got fetched twice: once via
# that umbrella page and again via its own category). Walking each of these
# leaf categories separately - each capped at 25 pages on its own - is what
# actually works around Walmart's page cap rather than what caused #8's
# missing items, so the umbrella URL is the one dropped here, not these.
#
# "all-tea" (976759_976782_1001320_9254040) used to be listed alongside
# "beverages" (976759_976782); its category id nests directly under
# beverages', i.e. it's a subset of beverages rather than a separate
# department, so it's dropped here too rather than kept and relying on
# run-level dedup (see _dedupe_key below) to paper over it.
#
# "meat & seafood" has no known equivalent browse URL (see #8 - meat was
# the category most conspicuously missing) so it stays a search query,
# which the same parser handles identically to a browse page.
GROCERY_CATEGORY_URLS = [
    "https://www.walmart.com/search?q=meat+%26+seafood&page=PAGE",
    "https://www.walmart.com/browse/food/fresh-produce/976759_976793?&page=PAGE",
    "https://www.walmart.com/browse/baking/976759_976780?&page=PAGE",
    "https://www.walmart.com/browse/food/frozen-fruits-vegetables/976759_976791_5624760?&page=PAGE",
    "https://www.walmart.com/browse/dairy-eggs/976759_9176907?&page=PAGE",
    "https://www.walmart.com/browse/bakery-bread/976759_976779?&page=PAGE",
    "https://www.walmart.com/browse/beverages/976759_976782?&page=PAGE",
    "https://www.walmart.com/browse/pantry/976759_976794?&page=PAGE",
    "https://www.walmart.com/browse/deli/976759_976789?&page=PAGE",
    "https://www.walmart.com/browse/snacks-cookies-chips/976759_976787?&page=PAGE",
    "https://www.walmart.com/browse/alcohol/976759_2975985?&page=PAGE",
    "https://www.walmart.com/browse/coffee/976759_1086446?&page=PAGE",
]

SIZE_PATTERN = r"(\d+(\.\d+)?\s*(lb|oz|fl oz|gal|each|ct|count|dozen|ounce|-ounce|-pack|pack))"

USER_AGENTS = [
    # Wrapped as adjacent string literals (implicit concatenation) purely to stay
    # under the line limit - the resulting values are byte-identical to the
    # single-line forms, which matters because these are spoofed browser UA
    # strings that a bot check will compare exactly.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.",
]

BASE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "accept-encoding": "gzip, deflate, br",  # Add Brotli compression (br), as modern browsers use it.
    "referer": "https://www.walmart.com/",  # Walmart expects internal navigation.
    "origin": "https://www.walmart.com",  # Some sites use this to validate requests.
    "dnt": "1",  # Do Not Track (optional, but some browsers send it).
    "sec-fetch-dest": "document",  # Helps mimic real browser requests.
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "connection": "keep-alive",  # Ensures persistent connections.
    "upgrade-insecure-requests": "1",  # Indicates support for secure connections.
    "cache-control": "max-age=0",  # Ensures fresh content is always fetched.
    "pragma": "no-cache",  # Prevents caching (alternative to cache-control).
}


def parse_search(html_text: str) -> tuple[list[dict], int]:
    """Extract results from a Walmart search/browse page's embedded Next.js data."""
    sel = Selector(text=html_text)
    data = sel.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
    if not data:
        # Confirmed live (see #12's investigation comments): Walmart serves a
        # "Robot or human?" interstitial instead of real content on exactly
        # these pages, even from a real prior-session browser. Raising with
        # that context instead of letting json.loads(None) blow up with a
        # bare TypeError, so a failed run's log says what actually happened.
        raise RuntimeError(
            "No __NEXT_DATA__ on the page - likely Walmart's bot-verification "
            "interstitial rather than a parsing bug (see #12)."
        )
    parsed = json.loads(data)
    item_stacks = parsed["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"]
    if not item_stacks:
        return [], 0
    return item_stacks[0]["items"], item_stacks[0]["count"]


async def scrape_walmart_page(session: httpx.AsyncClient, url: str, page: int = 1) -> httpx.Response:
    """Fetch a single Walmart search/browse page, rate-limited to avoid hammering it."""
    url = url.replace("PAGE", str(page))
    await asyncio.sleep(random.uniform(2.3, 6.5))
    resp = await session.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"Walmart request blocked or failed ({resp.status_code}): {url}")
    return resp


async def scrape_category(url: str, session: httpx.AsyncClient) -> list[dict]:
    """Walk one category/search URL across its pages, capped at 25 - Walmart's own limit."""
    results = []
    resp = await scrape_walmart_page(session, url, page=1)
    page_results, total_items = parse_search(resp.text)
    results.extend(page_results)

    max_page = min(math.ceil(total_items / 40), 25)
    for page in range(2, max_page + 1):
        resp = await scrape_walmart_page(session, url, page=page)
        page_results, _ = parse_search(resp.text)
        results.extend(page_results)
    return results


async def scrape_all_categories(session: httpx.AsyncClient) -> list[dict]:
    """Walk every category in GROCERY_CATEGORY_URLS once. Products may still show
    up more than once if Walmart cross-lists an item across categories - that's
    handled by the caller's run-level dedup, not here."""
    results = []
    for url in GROCERY_CATEGORY_URLS:
        results.extend(await scrape_category(url, session))
    return results


def _build_session(store: str | None) -> httpx.AsyncClient:
    headers = dict(BASE_HEADERS)
    headers["user-agent"] = random.choice(USER_AGENTS)
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=5)
    # Location now comes from the store id passed into main() (WALMARTSTORE,
    # threaded through by collector.py - see #21) instead of a hardcoded
    # literal, which is the actual fix for #12's "main(store) ignores store"
    # problem. What could NOT be verified: whether "wmtlabs:reflectorid" is
    # even still the right cookie key, or what shape of value it expects -
    # Walmart's bot wall challenges exactly the browse/category requests that
    # would let anyone confirm this from outside a real logged browser
    # session (see #12's investigation comments; same constraint documented
    # there applies here). This preserves the previously-observed cookie name
    # and now threads the configured store id into it rather than a hardcoded
    # value, but treat it as best-effort, unverified, until someone can check
    # it with a real browser session's devtools.
    cookies = {"wmtlabs:reflectorid": str(store)} if store else {}
    return httpx.AsyncClient(headers=headers, limits=limits, cookies=cookies)


def _extract_size(product_name: str) -> str:
    match = re.search(SIZE_PATTERN, product_name.lower())
    return match.group(1) if match else "Size/quantity not found"


def _extract_price(product: dict) -> float | None:
    raw = product.get("priceInfo", {}).get("linePrice", "")
    try:
        return float(str(raw).strip().removeprefix("$").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _dedupe_key(product_name: str, product_size: str) -> tuple[str, str]:
    """Products can appear in more than one category (cross-listed items, or a
    category id nesting under another - see GROCERY_CATEGORY_URLS comments).
    (name, size) is a reasonable stable identity here since Walmart's raw
    item payload doesn't expose a consistently-present product id field to
    key on instead."""
    return product_name, product_size


async def run(store: str | None) -> list[dict]:
    session = _build_session(store)
    try:
        return await scrape_all_categories(session)
    finally:
        await session.aclose()


def main(store):
    """Scrapes Walmart's grocery categories for the given store id (WALMARTSTORE,
    via collector.py/#21). See _build_session()'s docstring for the caveat on
    whether the location cookie this sends is actually still correct - that
    part is unverifiable without a real browser session because Walmart's bot
    wall challenges exactly the pages needed to check (see #12). Emits the
    same Product/Price/Rate/Size columns as the other scrapers, using the
    shared rate helper (units.calculate_rate_per_unit, see #17) instead of
    Walmart's own unitPrice string, and deduplicates products within the run."""
    raw_results = asyncio.run(run(store))

    seen = set()
    rows = []
    for product in raw_results:
        product_name = product.get("name", "")
        product_price = _extract_price(product)
        if not product_name or product_price is None:
            continue
        product_size = _extract_size(product_name)

        key = _dedupe_key(product_name, product_size)
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "Product": product_name,
            "Price": product_price,
            "Rate": calculate_rate_per_unit(product_price, product_size),
            "Size": product_size,
        })

    if not rows:
        return pd.DataFrame(columns=["Product", "Price", "Rate", "Size"])
    return pd.DataFrame(rows)
