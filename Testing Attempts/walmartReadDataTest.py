import json
import re
import pandas as pd

with open("walmart_search.json", "r", encoding="utf-8") as file:
    data = json.load(file)

frame = []

for product in data:
    product_name = product.get("name", '')
    product_price = product.get("priceInfo", {}).get('linePrice', '')
    rate = product.get("priceInfo", {}).get('unitPrice', '')

    match = re.search(r"(\d+(\.\d+)?\s*(lb|oz|fl oz|gal|each|ct|count|dozen|quarts|packs|ounce|-ounce|-pack|pack))", product_name.lower())
    if match:
        product_size = match.group(1)
    else:
        product_size = "Size/quantity not found"
    print(f"Product: {product_name}\nPrice: {product_price}\nSize/Quantity: {product_size}\nRate per Pound: {rate}\n")
    frame.append(pd.DataFrame.from_dict({"Product": [product_name], "Price": [product_price], "Rate": [rate], "Size": [product_size]}))


pd.concat(frame).to_csv("Walmart.csv", index=False)