import time
import re
import pandas as pd
import requests

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

total_products = 0

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

# Returns session token to be passed with all requests
def getAuth():
    data = {
        "binary": "web-ecom",
        "binary_version": "4.43.5",
        "is_retina": False,
        "os_version": "Win32",
        "pixel_density": "1.0",
        "push_token": "",
        "screen_height": 1080,
        "screen_width": 1920
    }
    url = "https://shop.topsmarkets.com/api/v3/user_init?with_configs=true?"
    response = requests.post(url, headers=headers, json=data)
    response_json = response.json()
    session_token = response_json.get("session_token")
    if not session_token:
        print("Failed to retrieve session token")
        return ""
    return session_token

# Returns True or False based on success or failure
def changeStore(store_id, session_token):
    url = "https://shop.topsmarkets.com/api/v2/user"
    data = {
        "has_changed_store": True,
        "store_id": 102
    }
    headers2 = {
        "Authorization": f"Bearer {session_token}",
        "Content-Type": "application/json"
    }
    response = requests.patch(url, headers=headers2, json=data)
    response_json = response.json()
    store_id = response_json.get("user", {}).get("store", {}).get("id")
    if store_id != store_id:
        return False
    return True

def getCategories(session_token, store_id):
    url = f"https://shop.topsmarkets.com/api/v2/categories/store/{store_id}"
    headers2 = {
        "Authorization": f"Bearer {session_token}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers2)
    response_json = response.json()
    category_ids = [str(item["id"]) for item in response_json["items"]]
    return category_ids


def getDataByCategory(category, session_token):
    global total_products
    data = []
    limit =  60
    offset = 0
    total = 100
    while offset < total:
        url = f"https://shop.topsmarkets.com/api/v2/store_products?fulfillment_type=instore&category_id={category}&category_ids={category}&limit={limit}&offset={offset}"
        headers2 = {
            "Authorization": f"Bearer {session_token}",
            "Content-Type": "application/json"
        }
        response = requests.get(url, headers=headers2)
        response_json = response.json()
        
        total = response_json['item_count']
        offset += limit
        
        items = response_json.get('items', [])
        for item in items:
            try:
                product_name = item['name']
                if not item['order_by_weight']:
                    product_price = item['base_price']
                    product_size = item['size_string']
                    if product_size:
                        rate = calculate_rate_per_unit(product_price, product_size)
                    else:
                        rate = 'N/A'
                else:
                    # if ordered by weight
                    product_price = ''
                    product_size = 'N/A'
                    rate = calculate_rate_per_unit(item['base_price'], item['display_uom'])
                print(f"Product: {product_name}\nPrice: {product_price}\nSize/Quantity: {product_size}\nRate: {rate}\n")
                data.append(pd.DataFrame.from_dict({"Product": [product_name], "Price": [product_price], "Rate": [rate], "Size": [product_size]}))
            except Exception as e:
                print(e)
                print(item)
                time.sleep(50)
    total_products += total
    return pd.concat(data)
        
def main():
    data = []
    store_id = "102"
    session = getAuth()
    changeStore("102",session)
    categories = getCategories(session, store_id)
    for category in categories:
        data.append(getDataByCategory(category, session))
    return pd.concat(data)