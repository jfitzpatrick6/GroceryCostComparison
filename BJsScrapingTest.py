from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
import selenium.webdriver.support.expected_conditions as EC
import time
import re
import pandas as pd
import requests

def initDriver(options=''):
    service = Service(ChromeDriverManager().install().replace('THIRD_PARTY_NOTICES.chromedriver', 'chromedriver.exe'))
    driver = webdriver.Chrome(service=service, options=options)
    return driver

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

url = f"https://ac.cnstrc.com/browse/group_id/grocery?c=ciojs-client-2.53.1&key=key_2i36vP8QTs3Ati4x&i=0a5f818b-0856-433f-a88f-6f097c36f09d&s=2&page=1&num_results_per_page=40&&fmt_options%5Bhidden_fields%5D=prices.0213&fmt_options%5Bhidden_fields%5D=sale_prices.0213&fmt_options%5Bhidden_fields%5D=original_price.0213&fmt_options%5Bhidden_fields%5D=eligibility.0213&fmt_options%5Bhidden_fields%5D=inventory.0213&fmt_options%5Bhidden_fields%5D=prices.online&fmt_options%5Bhidden_fields%5D=sale_prices.online&fmt_options%5Bhidden_fields%5D=original_price.online&fmt_options%5Bhidden_fields%5D=eligibility.online&fmt_options%5Bhidden_fields%5D=inventory.online&pre_filter_expression=%7B%22or%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22online%22%7D%2C%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%220213%22%7D%2C%7B%22and%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%220213%22%7D%2C%7B%22name%22%3A%22avail_sdd%22%2C%22value%22%3A%220213%22%7D%5D%7D%2C%7B%22name%22%3A%22out_of_stock%22%2C%22value%22%3A%22Y%22%7D%5D%7D&_dt=1738009529475"


data = []
done = False
page = 1
while not done:
    apiData = requests.get(f"https://ac.cnstrc.com/browse/group_id/grocery?c=ciojs-client-2.53.1&key=key_2i36vP8QTs3Ati4x&i=0a5f818b-0856-433f-a88f-6f097c36f09d&s=2&page={page}&num_results_per_page=40&&fmt_options%5Bhidden_fields%5D=prices.0213&fmt_options%5Bhidden_fields%5D=sale_prices.0213&fmt_options%5Bhidden_fields%5D=original_price.0213&fmt_options%5Bhidden_fields%5D=eligibility.0213&fmt_options%5Bhidden_fields%5D=inventory.0213&fmt_options%5Bhidden_fields%5D=prices.online&fmt_options%5Bhidden_fields%5D=sale_prices.online&fmt_options%5Bhidden_fields%5D=original_price.online&fmt_options%5Bhidden_fields%5D=eligibility.online&fmt_options%5Bhidden_fields%5D=inventory.online&pre_filter_expression=%7B%22or%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%22online%22%7D%2C%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%220213%22%7D%2C%7B%22and%22%3A%5B%7B%22name%22%3A%22avail_stores%22%2C%22value%22%3A%220213%22%7D%2C%7B%22name%22%3A%22avail_sdd%22%2C%22value%22%3A%220213%22%7D%5D%7D%2C%7B%22name%22%3A%22out_of_stock%22%2C%22value%22%3A%22Y%22%7D%5D%7D&_dt=1738009529475")
    apiData = apiData.json()
    products = apiData['response']['results']
    try:
        for product in products:
            product_name = product['value']
            product_price = product['data']['prices']['0213']['value']
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
    except:
        done = True
    page += 1

pd.concat(data).to_csv("BJs.csv", index=False)