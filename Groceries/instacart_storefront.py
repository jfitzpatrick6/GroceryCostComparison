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


def _collect_leaf_paths(nodes, path=()):
    """Department tree -> [(leaf slug, "Dept > Leaf" path)] (#114).

    Same leaves as _collect_leaf_slugs, plus the human path to each, so every
    item can record which department it was listed under. On Tops that is a
    real taxonomy (Meat & Seafood > Poultry, Deli & Bakery > Prepared Meals,
    Pet > Dog Treats & Bones); on Aldi the tree is brands and diets ("ALDI
    Exclusive Brands > Kirkwood"), which is not a category - see scrape_store.
    """
    leaves = []
    for node in nodes:
        here = (*path, (node.get("name") or "").strip())
        children = node.get("childCollections") or []
        if children:
            leaves.extend(_collect_leaf_paths(children, here))
        else:
            leaves.append((node["slug"], " > ".join(p for p in here if p)))
    return leaves


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
    return [slug for slug, _ in get_department_paths(context, host, shop_id, postal_code)]


def get_department_paths(context, host, shop_id, postal_code):
    data = _graphql_get(
        context,
        host,
        "CollectionsHeaderDepartments",
        {"includeSlugs": ["dynamic_collection-sales"], "shopId": shop_id, "postalCode": postal_code},
    )
    # Real departments first, promotions last, so a cross-listed item keeps its
    # department rather than the promo it also appears in (dedupe keeps the
    # first listing - see scrape_store). Observed on Tops 2026-09-29: the tree
    # OPENS with a one-leaf seasonal collection ("Everything Peach", slug
    # "rc-08-22-26everythingpeach"), while every real department's top-level
    # slug starts "n-" ("n-meat-seafood-7"). Sorting is stable, so department
    # order is otherwise unchanged.
    tops = data.get("deptCollections") or []
    ordered = sorted(tops, key=lambda node: not (node.get("slug") or "").startswith("n-"))
    return _collect_leaf_paths(ordered)


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
    price_view = (item.get("price") or {}).get("viewSection") or {}
    price_str = price_view.get("priceValueString")
    if not name or not price_str:
        return None
    price = float(price_str)

    # Weight-variable items (fresh meat/poultry) don't reliably carry a
    # correct top-level `size` field - verified against live data (see #54):
    # a "TOPS Whole Fryer Chicken" priced $14.99 came back with
    # size="48.5 lb" ($0.31/lb) when the item is really a ~6 lb chicken
    # ($2.49/lb per Instacart's own displayed rate); a "TOPS B Boneless
    # Chicken Thigh" priced directly at $3.79/lb came back with
    # size="36 lb" ($0.105/lb) despite there being no 36 lb package at
    # all - it's sold by the pound with a customer-adjustable quantity.
    # `size` for these appears to be bogus/unrelated data, not a rate or
    # a package weight. The correct data instead lives on
    # `quantityAttributes`, which mirrors what Instacart's own site shows
    # a shopper:
    #   - quantityType == "weight": the item is priced directly *per
    #     pound* (item card shows e.g. "$3.79 /lb") - price already IS
    #     the $/lb rate, no size lookup needed.
    #   - parWeight present: an "each"-sold item (whole chicken, sausage
    #     links/patties) whose price is a total for Instacart's own
    #     average-weight estimate (parWeight.quantity, in lb) - that
    #     estimate is the real per-package weight, not whatever `size`
    #     says.
    #   - neither: a normal fixed-size item (e.g. "19 oz" packaged
    #     sausage) - `size` is fine as before.
    quantity_attrs = item.get("quantityAttributes") or {}
    par_weight = (quantity_attrs.get("parWeight") or {}).get("quantity")
    if quantity_attrs.get("quantityType") == "weight":
        size = "1 lb"
    elif par_weight:
        size = f"{par_weight} lb"
    else:
        size = item.get("size") or "N/A"

    rate = calculate_rate_per_unit(price, size) if size != "N/A" else "N/A"
    return {"Product": [name], "Price": [price], "Rate": [rate], "Size": [size], "Category": [None]}


def scrape_store(retailer_slug, host, calculate_rate_per_unit, categories=True):
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

            # categories=False for a storefront whose tree is not a taxonomy
            # (Aldi: brands and diets). Recording "Kirkwood" as a category would
            # be a wrong answer dressed as data; None says "unknown" (#114).
            leaves = get_department_paths(context, host, shop_id, postal_code)
            for slug, path in leaves:
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
                                if categories:
                                    parsed["Category"] = [path]
                                # One dict per item, one DataFrame at the end
                                # (#74): this used to build a one-row DataFrame
                                # per item - ~17,500 for Tops - then concat them.
                                rows.append({k: v[0] for k, v in parsed.items()})
                        except Exception as e:
                            print(f"[{retailer_slug}] item parse error: {e}")
                            print(item)
        finally:
            browser.close()

    if not rows:
        return pd.DataFrame(columns=["Product", "Price", "Rate", "Size", "Category"])
    # Leaf collections are usually disjoint, but a "sales" collection can
    # cross-list an item that's also in its normal category - dedupe those.
    # Deduped on the priced fields only, NOT Category: a cross-listed item now
    # differs by category per listing, and keeping every copy would duplicate
    # ~1,700 Tops products (#96 measured the cross-listing). The first listing
    # wins, which is the department walk order - a real department before the
    # dynamic "sales" collection.
    return pd.DataFrame(rows, columns=["Product", "Price", "Rate", "Size", "Category"]).drop_duplicates(
        subset=["Product", "Price", "Rate", "Size"], ignore_index=True
    )
