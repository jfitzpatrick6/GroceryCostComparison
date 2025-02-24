import time
import re
import pandas as pd
import requests

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

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
    url = f"https://shop.topsmarkets.com/api/v2/store_products?fulfillment_type=instore&category_id={category}&category_ids={category}"
    headers2 = {
        "Authorization": f"Bearer {session_token}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers2)
    response_json = response.json()
    items = response_json.get('items', [])
    for item in items:
        try:
            product_name = item['name']
            product_price = item['base_price']
            if not item['order_by_weight']:
                product_size = item['size_string']
                if not product_size:
                    rate = product_price
                else:
                    try:
                        rate = str(item['uom_price']['price']) + ' per ' + item['uom_price']['uom']
                    except:
                        rate = 'N/A'
            else:
                product_size = ""
                rate = item['base_price']
            print(f"Product: {product_name}\nPrice: {product_price}\nSize/Quantity: {product_size}\nRate: {rate}\n")
            data.append(pd.DataFrame.from_dict({"Product": [product_name], "Price": [product_price], "Rate": [rate], "Size": [product_size]}))
        except Exception as e:
            print(e)
            print(item)
            time.sleep(50)
        

data = []
store_id = "102"
session = getAuth()
changeStore("102",session)
categories = getCategories(session, store_id)
for category in categories:
    getDataByCategory(category, session)
pd.concat(data).to_csv("Tops.csv", index=False)