from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
import time
import re


def initDriver(options=''):
    service = Service(ChromeDriverManager().install().replace('THIRD_PARTY_NOTICES.chromedriver', 'chromedriver.exe'))
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def calculate_rate_per_pound(price, size_quantity):
    size_quantity = size_quantity.lower()
    try:
        # Extract numeric value and unit using regex
        match = re.search(r"(\d+(\.\d+)?)\s*(lb|oz|each|ct)", size_quantity)
        if not match:
            return "Rate not applicable"
        
        # Get the numeric value and unit
        value = float(match.group(1))
        unit = match.group(3)

        # Convert to pounds if necessary
        if unit == "lb":
            rate_per_pound = price / value
        elif unit == "oz":
            rate_per_pound = price / (value / 16)  # Convert ounces to pounds
        else:
            return "Rate not applicable"  # Not a weight-based unit

        return f"${rate_per_pound:.2f} per lb"
    except:
        return "Rate not applicable"

driver = initDriver()

driver.get("https://new.aldi.us/products/fresh-produce/k/13")
time.sleep(10)

products = driver.find_elements(By.CLASS_NAME, "product-tile")
for product in products:
    product_title = product.get_attribute("title")
    
    # Find the price element within the current product
    try:
        price_element = product.find_element(By.CLASS_NAME, "base-price__regular")
        product_price = float(price_element.text.strip().removeprefix("$"))  # Get the price text
    except Exception as e:
        print(e)
        product_price = 0
    
    try:
        size_element = product.find_element(By.CLASS_NAME, "product-tile__unit-of-measurement")
        product_size = size_element.text  # Get the size/quantity text
    except:
        product_size = "Size/quantity not found"

    if product_price and product_size:
        rate_per_pound = calculate_rate_per_pound(product_price, product_size)
    else:
        rate_per_pound = "Insufficient data"
    # Print the product details
    print(f"Product: {product_title}\nPrice: ${product_price:.2f}\nSize/Quantity: {product_size}\nRate per Pound: {rate_per_pound}\n")


# Close the browser
