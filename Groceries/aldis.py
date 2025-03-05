import time
import re
import pandas as pd
import requests
import json

def convert_metric_to_imperial(input_str):
    """
    Converts metric measurements in the format "number unit" to imperial units.
    Supported conversions: kg→lb, g→oz, l→gal, ml→fl oz, m→ft, cm→in, km→mi
    """
    # Match the pattern "number unit"
    match = re.match(r"^(\d+\.?\d*)\s*([a-zA-Z]+)$", input_str)
    if not match:
        return f"Invalid input format: {input_str}"

    value_str, metric_unit = match.groups()
    
    try:
        value = float(value_str)
    except ValueError:
        return f"Invalid numeric value: {input_str}"

    # Normalize unit (lowercase, singular form)
    unit = metric_unit.lower().rstrip('s')  # Remove trailing 's' for plurals
    
    # Conversion mapping - expand this as needed
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
        'kilometre': {'unit': 'mi', 'factor': 0.621371}
    }

    # Find matching conversion
    conversion = conversion_table.get(unit)
    if not conversion:
        return f"{input_str} (conversion not available)"

    converted_value = value * conversion['factor']
    return f"{round(converted_value, 2)} {conversion['unit']}"

def calculate_rate_per_unit(price, size_quantity):
    size_quantity = size_quantity.lower()
    try:
        # Extract numeric value and unit using regex
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

        # Conversion logic
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
            rate = price / value  # Rate per Item
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

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

def main():
    frame = []
    store = "465-089"
    limit = 48

    # https://api.aldi.us/v3/product-search?currency=USD&serviceType=pickup&categoryKey=20&limit=30&offset=0&sort=relevance&servicePoint=465-089
    url = f"https://api.aldi.us/v3/product-search?currency=USD&serviceType=pickup&servicePoint={store}&limit={limit}"

    response = requests.get(url, headers=headers)
    response_json = response.json()

    pagination = response_json['meta']['pagination']
    offset = 0
    while offset < pagination['totalCount']:
        url = f"https://api.aldi.us/v3/product-search?currency=USD&serviceType=pickup&limit={pagination['limit']}&offset={offset}&sort=relevance&servicePoint={store}"
        response = requests.get(url, headers=headers)
        response_json = response.json()
        data = response_json['data']
        for product in data:
            try:
                product_name = product['name']
                product_size = product['sellingSize']
                product_price = float(product['price']['amountRelevantDisplay'].replace("$", ''))
                if product_size:
                    product_rate = calculate_rate_per_unit(product_price, product_size)
                else:
                    product_rate = None
                print(f"Product: {product_name}\nPrice: {product_price}\nSize/Quantity: {product_size}\nRate: {product_rate}\n")
                frame.append(pd.DataFrame.from_dict({"Product": [product_name], "Price": [product_price], "Rate": [product_rate], "Size": [product_size]}))
            except Exception as e:
                print(e)
                print(product)
                time.sleep(100)
        offset += pagination['limit']
    return pd.concat(frame)