import instacart_storefront
from units import calculate_rate_per_unit

RETAILER_SLUG = "aldi"
HOST = "www.aldi.us"


def main(store=None):
    """Scrapes Aldi's current storefront (Instacart white-label platform -
    see #41). `store` is accepted for call-signature compatibility with
    collector.py but is not yet used to target a specific store - see the
    same note in tops.py's main() and #43."""
    if store:
        print(f"aldis.py: store={store!r} is not used yet (see #43) - "
              f"scraping the store resolved for this machine's network location instead.")
    return instacart_storefront.scrape_store(RETAILER_SLUG, HOST, calculate_rate_per_unit)
