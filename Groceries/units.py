"""
Shared unit/rate conversion helper, used by every scraper. Previously
copy-pasted with slight drift into tops.py, aldis.py, and BJs.py - this is
the union of what those three actually needed (aldis.py's copy was the
most capable: more recognized units, metric fallback), consolidated once.
Walmart.py's rate handling is a genuinely different mechanism (parses a
"¢/oz"-style string Walmart's own site returns) and isn't part of this.
"""

import re


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


def calculate_rate_per_unit(price, size_quantity):
    size_quantity = size_quantity.lower()
    try:
        match = re.search(r"([\d.]+)\s*(pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt|fl oz|pt|ea|ea.|ft)", size_quantity)
        if not match:
            size_quantity = convert_metric_to_imperial(size_quantity)
            match = re.search(r"([\d.]+)\s*(pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt|fl oz|pt|ea|ea.|ft)", size_quantity)
            if not match:
                return "Rate not applicable"

        value = float(match.group(1).replace(',', ''))
        unit = match.group(2)

        if unit == "dozen" or unit == "count" or unit == "ct" or unit == 'pk' or unit == 'pc' or unit == 'ea' or unit == 'ea.':
            value = value * 12 if unit == "dozen" else value
            unit = "each"

        if unit == "lb" or unit == "ib":
            rate = price / value  # Rate per pound
            return f"${rate:.2f} per lb"
        elif unit == "oz":
            rate = price / (value / 16)  # Convert ounces to pounds
            return f"${rate:.2f} per lb"
        elif unit == "fl. oz" or unit == "fl oz":
            rate = price / (value / 128)  # Convert fluid ounces to gallons
            return f"${rate:.2f} per gallon"
        elif unit == "gal":
            rate = price / value  # Rate per gallon
            return f"${rate:.2f} per gallon"
        elif unit == "each":
            rate = price / value  # Rate per item
            return f"${rate:.2f} per item"
        elif unit == "pint" or unit == "pt":
            rate = price / (value / 8)  # Rate per gallon
            return f"${rate:.2f} per item"
        elif unit == "l" or unit == 'liter':
            rate = price / (value / 3.78541178)  # Rate per gallon
            return f"${rate:.2f} per item"
        elif unit == "qt":
            rate = price / (value / 4)  # Rate per gallon
            return f"${rate:.2f} per item"
        elif unit == "ft":
            rate = price / value  # Rate per foot
            return f"${rate:.2f} per foot"
        else:
            return "Rate not applicable"  # Not a weight- or volume-based unit
    except Exception as e:
        return f"Error: {e} {size_quantity} {price}"
