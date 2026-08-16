import re

import instacart_storefront

RETAILER_SLUG = "tops-markets"
HOST = "shop.topsmarkets.com"


def calculate_rate_per_unit(price, size_quantity):
    size_quantity = size_quantity.lower()
    try:
        # Extract numeric value and unit using regex
        match = re.search(r"([\d.]+)\s*(pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt)", size_quantity)
        if not match:
            return "Rate not applicable"

        value = float(match.group(1).replace(',', ''))
        unit = match.group(2)

        if unit == "dozen" or unit == "count" or unit == "ct" or unit == 'pk' or unit == 'pc':
            value = value * 12 if unit == "dozen" else value
            unit = "each"

        # Conversion logic
        if unit == "lb" or unit == "ib":
            rate = price / value  # Rate per pound
            return f"${rate:.2f} per lb"
        elif unit == "oz":
            rate = price / (value / 16)  # Convert ounces to pounds
            return f"${rate:.2f} per lb"
        elif unit == "fl. oz":
            rate = price / (value / 128)  # Convert fluid ounces to gallons
            return f"${rate:.2f} per gallon"
        elif unit == "gal":
            rate = price / value  # Rate per gallon
            return f"${rate:.2f} per gallon"
        elif unit == "each":
            rate = price / value  # Rate per Item
            return f"${rate:.2f} per item"
        elif unit == "pint":
            rate = price / (value / 8)  # Rate per gallon
            return f"${rate:.2f} per item"
        elif unit == "l" or unit == 'liter':
            rate = price / (value / 3.78541178)  # Rate per gallon
            return f"${rate:.2f} per item"
        elif unit == "qt":
            rate = price / (value / 4)  # Rate per gallon
            return f"${rate:.2f} per item"
        else:
            return "Rate not applicable"  # Not a weight- or volume-based unit
    except Exception as e:
        return f"Error: {e} {size_quantity}"


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
