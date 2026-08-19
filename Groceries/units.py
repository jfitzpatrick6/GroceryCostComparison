"""
Shared unit/rate conversion helper, used by every scraper. Previously
copy-pasted with slight drift into tops.py, aldis.py, and BJs.py - this is
the union of what those three actually needed (aldis.py's copy was the
most capable: more recognized units, metric fallback), consolidated once.
Walmart.py used to have its own separate mechanism (parsing a "¢/oz"-style
string Walmart's own site returns) but was switched over to this module
too (see #12) - all four scrapers now go through calculate_rate_per_unit.
"""

import re

_UNIT_PATTERN = r"([\d.]+)\s*(pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt|fl oz|pt|ea|ea.|ft)"

# Canonical unit each recognized size-unit normalizes to, for the
# *structured* parser (parse_unit_price). Weight -> lb, volume -> gal,
# count -> each, length -> ft.
_CANONICAL_UNIT = {
    "lb": "lb", "ib": "lb", "oz": "lb",
    "gal": "gal", "fl. oz": "gal", "fl oz": "gal", "pint": "gal", "pt": "gal",
    "l": "gal", "liter": "gal", "qt": "gal",
    "each": "each", "dozen": "each", "count": "each", "ct": "each",
    "pk": "each", "pc": "each", "ea": "each", "ea.": "each",
    "ft": "ft",
}


def convert_metric_to_imperial(input_str):
    """
    Converts metric measurements in the format "number unit" to imperial units.
    Supported conversions: kg→lb, g→oz, l→gal, ml→fl oz, m→ft, cm→in, km→mi
    """
    match = re.match(r"^(\d+\.?\d*)\s*([a-zA-Z]+)$", input_str)
    if not match:
        return f"Invalid input format: {input_str}"

    value_str, metric_unit = match.groups()

    try:
        value = float(value_str)
    except ValueError:
        return f"Invalid numeric value: {input_str}"

    unit = metric_unit.lower().rstrip('s')  # Remove trailing 's' for plurals

    conversion_table = {
        # Mass
        'kg': {'unit': 'lb', 'factor': 2.20462},
        'kilogram': {'unit': 'lb', 'factor': 2.20462},
        'g': {'unit': 'oz', 'factor': 0.035274},
        'gram': {'unit': 'oz', 'factor': 0.035274},

        # Volume
        'l': {'unit': 'gal', 'factor': 0.264172},
        'liter': {'unit': 'gal', 'factor': 0.264172},
        'litre': {'unit': 'gal', 'factor': 0.264172},
        'ml': {'unit': 'fl oz', 'factor': 0.033814},
        'milliliter': {'unit': 'fl oz', 'factor': 0.033814},
        'millilitre': {'unit': 'fl oz', 'factor': 0.033814},

        # Length
        'm': {'unit': 'ft', 'factor': 3.28084},
        'meter': {'unit': 'ft', 'factor': 3.28084},
        'metre': {'unit': 'ft', 'factor': 3.28084},
        'cm': {'unit': 'in', 'factor': 0.393701},
        'centimeter': {'unit': 'in', 'factor': 0.393701},
        'centimetre': {'unit': 'in', 'factor': 0.393701},
        'km': {'unit': 'mi', 'factor': 0.621371},
        'kilometer': {'unit': 'mi', 'factor': 0.621371},
        'kilometre': {'unit': 'mi', 'factor': 0.621371},
    }

    conversion = conversion_table.get(unit)
    if not conversion:
        return f"{input_str} (conversion not available)"

    converted_value = value * conversion['factor']
    return f"{round(converted_value, 2)} {conversion['unit']}"


def _match_size(size_quantity):
    """Regex-matches a size string against recognized units, falling back to
    a metric-to-imperial conversion first. Returns (value, raw_unit) or
    (None, None) if nothing matched."""
    size_quantity = size_quantity.lower()
    match = re.search(_UNIT_PATTERN, size_quantity)
    if not match:
        size_quantity = convert_metric_to_imperial(size_quantity)
        match = re.search(_UNIT_PATTERN, size_quantity)
        if not match:
            return None, None
    value = float(match.group(1).replace(',', ''))
    return value, match.group(2)


def parse_unit_price(price, size_quantity):
    """Structured version: returns (unit_price, unit) where unit is one of
    'lb'/'gal'/'each'/'ft', or (None, None) if size_quantity couldn't be
    parsed. Meant for storing numeric, comparable data (see #13) rather
    than display."""
    value, raw_unit = _match_size(size_quantity)
    if value is None or value == 0:
        return None, None

    canonical = _CANONICAL_UNIT.get(raw_unit)
    if canonical == "each" and raw_unit == "dozen":
        value = value * 12

    if raw_unit == "oz":
        value = value / 16  # -> lb
    elif raw_unit in ("fl. oz", "fl oz"):
        value = value / 128  # -> gal
    elif raw_unit in ("pint", "pt"):
        value = value / 8  # -> gal
    elif raw_unit in ("l", "liter"):
        value = value / 3.78541178  # -> gal
    elif raw_unit == "qt":
        value = value / 4  # -> gal

    if canonical is None:
        return None, None
    return price / value, canonical


def calculate_rate_per_unit(price, size_quantity):
    """Display-string version, e.g. "$3.99 per lb". Kept for the scrapers'
    existing "Rate" column; parse_unit_price() is the numeric equivalent."""
    try:
        unit_price, unit = parse_unit_price(price, size_quantity)
        if unit_price is None:
            return "Rate not applicable"
        label = {"lb": "per lb", "gal": "per gallon", "each": "per item", "ft": "per foot"}[unit]
        return f"${unit_price:.2f} {label}"
    except Exception as e:
        return f"Error: {e} {size_quantity} {price}"
