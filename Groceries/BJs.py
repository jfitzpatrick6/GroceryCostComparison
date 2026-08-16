import time
import re
import pandas as pd
import requests

from units import calculate_rate_per_unit

def main(store):
    data = []
    page = 1
    while True:
        apiData = requests.get(f"https://ac.cnstrc.com/browse/group_id/grocery?c=ciojs-client-2.53.1&key=key_2i36vP8QTs3Ati4x&i=0a5f818b-0856-433f-a88f-6f097c36f09d&s=2&page={page}&num_results_per_page=40&&fmt_options%5Bhidden_fields%5D=prices.{store}&fmt_options%5Bhidden_fields%5D=sale_prices.{store}&fmt_options%5Bhidden_fields%5D=original_price.{store}&fmt_options%5Bhidden_fields%5D=eligibility.{store}&fmt_options%5Bhidden_fields%5D=inventory.{store}&fmt_options%5Bhidden_fields%5D=prices.online&fmt_options%5Bhidden_fields%5D=sale_prices.online&fmt_options%5Bhidden_fields%5D=original_price.online&fmt_options%5Bhidden_fields%5D=eligibility.online&fmt_options%5Bhidden_fields%5D=inventory.online&pre_filter_expression=%7B%22or%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22online%22%7D%2C%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22{store}%22%7D%2C%7B%22and%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22{store}%22%7D%2C%7B%22name%22%3A%22avail_sdd%22%2C%22value%22%3A%22{store}%22%7D%5D%7D%2C%7B%22name%22%3A%22out_of_stock%22%2C%22value%22%3A%22Y%22%7D%5D%7D&_dt=1738009529475")
        apiData = apiData.json()
        products = apiData.get('response', {}).get('results', [])
        if not products:
            break
        for product in products:
            try:
                product_name = product['value']
                facets = {f['name']: f['values'][0] for f in product.get('data', {}).get('facets', []) if f.get('values')}

                if facets.get('weighted_item') == 'Y':
                    # By-weight items (produce, fresh meat/poultry) don't carry
                    # a `prices` field at all - BJs only exposes a min/max price
                    # over the pack's min/max weight (see #16). There's no
                    # single "the" price for these (real packages vary), so
                    # this uses the midpoint of both ranges as an honest
                    # estimate rather than pretending it's exact.
                    attrs = product.get('data', {}).get('attr', {})
                    min_price, max_price = facets.get('min_price'), facets.get('max_price')
                    min_weight, max_weight = attrs.get('minpackweight'), attrs.get('maxpackweight')
                    if None in (min_price, max_price, min_weight, max_weight):
                        raise ValueError(f"weighted item missing price/weight range: {facets}, {attrs}")
                    product_price = (float(min_price) + float(max_price)) / 2
                    avg_weight = (float(min_weight) + float(max_weight)) / 2
                    product_size = f"{avg_weight} lb"
                else:
                    product_price = product['data']['prices'][store]['value']
                    product_price = float(product_price.strip().removeprefix("$"))
                    match = re.search(r"(([\d.]+)\s*(pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt))", product_name.lower().replace(',', ''))
                    if match:
                        calcmatch = re.search(r"([\d.]+)\s*(pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt).\/([\d.]+)\s*(pc|lb|oz|fl. oz|gal|each|ct|count|dozen|ib|pk|pint|l|liter|qt)", product_name.lower().replace(',', ''))
                        if calcmatch:
                            product_size = str(float(calcmatch.group(1).replace(',', '')) * float(calcmatch.group(3).replace(',', ''))) + ' ' + calcmatch.group(4)
                        else:
                            product_size = match.group(1)
                    else:
                        product_size = 'N/A'

                data.append(pd.DataFrame.from_dict({"Product": [product_name], "Price": [product_price], "Rate": [calculate_rate_per_unit(product_price, product_size)], "Size": [product_size]}))
            except Exception as e:
                print(e)
                print(product)
        page += 1
    if not data:
        return pd.DataFrame(columns=["Product", "Price", "Rate", "Size"])
    return pd.concat(data, ignore_index=True)