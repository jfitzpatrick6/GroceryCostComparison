"""
Shared scraper for grocery chains whose online storefront runs on Instacart's
"Storefront Pro" white-label platform (confirmed: Tops Markets and Aldi US -
see GitHub issue #41). Both tenants share the same GraphQL API, URL scheme,
and behavior; this module implements it once and is called from both
tops.py and aldis.py with different (host, retailer_slug) pairs.

How this works, and why it's shaped this way (see #41/#43 for the full
investigation): the catalog/price API requires a browser-issued session -
plain HTTP requests get "Not Authenticated". A real headless browser (no
login needed) works. It also requires a `zoneId` per request that is never
returned by any API response and appears to be computed client-side by
Instacart's own JS - a wrong value doesn't error, it silently returns wrong
regional prices. The only verified-safe way to get a correct (shopId,
zoneId) pair is to let the real site's own JS resolve it (via server-side
IP geolocation) and passively capture what it actually used. That means
this scraper targets whatever store Instacart resolves for the machine's
own network location - accurate for a home-server deployment on a
residential IP, not necessarily controllable to an arbitrary zip yet.
"""

import json
import re
import time
import urllib.parse
import uuid

import pandas as pd
from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Persisted-query hashes captured from Instacart's Storefront Pro frontend
# bundle on 2026-08-16. These are captured constants, not a stable public
# API - they WILL go stale when Instacart ships a new bundle. A
# "PersistedQueryNotFound"/"PersistedQueryNotSupported" error means they
# need to be re-captured from a real browser session's network traffic.
GRAPHQL_HASHES = {
    "CollectionsHeaderDepartments": "92f1b9298f8cbef411e9d5e042110c5b050702675680ad848486f8abf86a26c5",
    "CollectionProductsWithFeaturedProducts": "65d04cc11e791b65c7c39443c9d4a56f10ed37fa4e50dbf5aac11a9365bddd41",
    "Items": "9ad66078d7fa81276b6bd4eb6a6f6fcdd1f4022ff0c3f5b4663c62877f06692a",
}

ITEMS_BATCH_SIZE = 10


class StaleQueryHashError(RuntimeError):
    pass


def _graphql_get(context, host, operation, variables):
    extensions = {"persistedQuery": {"version": 1, "sha256Hash": GRAPHQL_HASHES[operation]}}
    url = (
        f"https://{host}/graphql?operationName={operation}"
        f"&variables={urllib.parse.quote(json.dumps(variables))}"
        f"&extensions={urllib.parse.quote(json.dumps(extensions))}"
    )
    response = context.request.get(url)
    body = response.json()
    errors = body.get("errors")
    if errors:
        msg = json.dumps(errors)
        if "PersistedQueryNotFound" in msg or "PersistedQueryNotSupported" in msg:
            raise StaleQueryHashError(
                f"Instacart persisted-query hash for {operation} is stale - "
                f"re-capture it from a real browser session. {msg}"
            )
        raise RuntimeError(f"{operation} failed: {msg}")
    return body.get("data") or {}


def _harvest_shop_and_zone(page, host):
    """Loads the homepage and passively captures the (shopId, zoneId,
    postalCode) the site's own JS resolves for this machine's network
    location - see module docstring for why this is the only verified-safe
    way to get a correct pair rather than deriving it ourselves."""
    found = {}

    def on_request(request):
        url = request.url
        if found or "/graphql" not in url or "operationName=Items" not in url:
            return
        m = re.search(r"variables=([^&]+)", url)
        if not m:
            return
        try:
            variables = json.loads(urllib.parse.unquote(m.group(1)))
        except Exception:
            return
        if variables.get("shopId") and variables.get("zoneId"):
            found["shop_id"] = variables["shopId"]
            found["zone_id"] = variables["zoneId"]
            found["postal_code"] = variables.get("postalCode")

    page.on("request", on_request)
    page.goto(f"https://{host}/", timeout=45000, wait_until="load")
    deadline = time.time() + 15
    while not found and time.time() < deadline:
        page.wait_for_timeout(500)
    page.remove_listener("request", on_request)

    if not found:
        raise RuntimeError(
            f"Could not determine shop/zone for {host} - no Items request "
            f"observed within 15s of loading the homepage."
        )
    return found


def _collect_leaf_slugs(nodes):
    """Department tree -> flat list of leaf collection slugs. Only leaves
    are queried (not parent departments) since a parent's item count is
    capped the same way a leaf's is, and querying leaves avoids silently
    losing items that don't fit under the parent's own cap."""
    leaves = []
    for node in nodes:
        children = node.get("childCollections") or []
        if children:
            leaves.extend(_collect_leaf_slugs(children))
        else:
            leaves.append(node["slug"])
    return leaves


def get_department_slugs(context, host, shop_id, postal_code):
    data = _graphql_get(
        context,
        host,
        "CollectionsHeaderDepartments",
        {"includeSlugs": ["dynamic_collection-sales"], "shopId": shop_id, "postalCode": postal_code},
    )
    return _collect_leaf_slugs(data.get("deptCollections") or [])


def get_collection_item_ids(context, host, shop_id, zone_id, postal_code, slug):
    data = _graphql_get(
        context,
        host,
        "CollectionProductsWithFeaturedProducts",
        {
            "shopId": shop_id,
            "postalCode": postal_code,
            "zoneId": zone_id,
            "slug": slug,
            "filters": [],
            "pageViewId": str(uuid.uuid4()),
            "itemsDisplayType": "collections_items_grid",
            "first": 4,
            "pageSource": "browse",
        },
    )
    collection_products = data.get("collectionProducts") or {}
    return collection_products.get("itemIds") or []


def get_items(context, host, shop_id, zone_id, postal_code, item_ids):
    data = _graphql_get(
        context,
        host,
        "Items",
        {"ids": item_ids, "shopId": shop_id, "zoneId": zone_id, "postalCode": postal_code},
    )
    return data.get("items") or []


def _parse_item(item, calculate_rate_per_unit):
    name = item.get("name")
    size = item.get("size") or "N/A"
    price_view = (item.get("price") or {}).get("viewSection") or {}
    price_str = price_view.get("priceValueString")
    if not name or not price_str:
        return None
    price = float(price_str)
    rate = calculate_rate_per_unit(price, size) if size != "N/A" else "N/A"
    return {"Product": [name], "Price": [price], "Rate": [rate], "Size": [size]}


def scrape_store(retailer_slug, host, calculate_rate_per_unit):
    """Scrapes one Instacart white-label storefront (Tops or Aldi) for
    whatever store the machine's network location resolves to. Returns a
    DataFrame with Product/Price/Rate/Size columns, matching the other
    scrapers' shape.
    """
    rows = []
    with sync_playwright() as p:
        # --no-sandbox: required running as root in Docker.
        # --disable-dev-shm-usage: Docker's default /dev/shm is 64MB, too
        # small for Chromium and a common cause of crashes on long runs;
        # this makes it use /tmp instead.
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        try:
            shop = _harvest_shop_and_zone(page, host)
            shop_id = shop["shop_id"]
            zone_id = shop["zone_id"]
            postal_code = shop["postal_code"]

            slugs = get_department_slugs(context, host, shop_id, postal_code)
            for slug in slugs:
                try:
                    item_ids = get_collection_item_ids(context, host, shop_id, zone_id, postal_code, slug)
                except Exception as e:
                    print(f"[{retailer_slug}] {slug}: failed to list items - {e}")
                    continue
                for i in range(0, len(item_ids), ITEMS_BATCH_SIZE):
                    batch = item_ids[i : i + ITEMS_BATCH_SIZE]
                    try:
                        items = get_items(context, host, shop_id, zone_id, postal_code, batch)
                    except Exception as e:
                        print(f"[{retailer_slug}] {slug} batch: failed to fetch items - {e}")
                        continue
                    for item in items:
                        try:
                            parsed = _parse_item(item, calculate_rate_per_unit)
                            if parsed:
                                rows.append(pd.DataFrame.from_dict(parsed))
                        except Exception as e:
                            print(f"[{retailer_slug}] item parse error: {e}")
                            print(item)
        finally:
            browser.close()

    if not rows:
        return pd.DataFrame(columns=["Product", "Price", "Rate", "Size"])
    # Leaf collections are usually disjoint, but a "sales" collection can
    # cross-list an item that's also in its normal category - dedupe those.
    return pd.concat(rows, ignore_index=True).drop_duplicates(ignore_index=True)
