import instacart_storefront
from units import calculate_rate_per_unit

RETAILER_SLUG = "tops-markets"
HOST = "shop.topsmarkets.com"


def main(store_id=None):
    """Scrapes Tops' current storefront (Instacart white-label platform -
    see #41). `store_id` is accepted for call-signature compatibility with
    collector.py but is not yet used to target a specific store: safe
    store/zone targeting away from this machine's own network location
    isn't verified yet (see #43). This scrapes whatever store Instacart
    resolves via server-side IP geolocation for wherever this runs."""
    if store_id:
        print(f"tops.py: store_id={store_id!r} is not used yet (see #43) - "
              f"scraping the store resolved for this machine's network location instead.")
    return instacart_storefront.scrape_store(RETAILER_SLUG, HOST, calculate_rate_per_unit)
