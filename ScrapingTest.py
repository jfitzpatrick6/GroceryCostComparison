from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
import selenium.webdriver.support.expected_conditions as EC
import time
import re


def initDriver(options=''):
    service = Service(ChromeDriverManager().install().replace('THIRD_PARTY_NOTICES.chromedriver', 'chromedriver.exe'))
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def calculate_rate_per_unit(price, size_quantity):
    size_quantity = size_quantity.lower()
    try:
        # Extract numeric value and unit using regex
        match = re.search(r"(\d+(\.\d+)?)\s*(lb|oz|fl oz|gal|each|ct)", size_quantity)
        if not match:
            return "Rate not applicable"
        
        # Get the numeric value and unit
        value = float(match.group(1))
        unit = match.group(3)

        # Conversion logic
        if unit == "lb":
            rate = price / value  # Rate per pound
            return f"${rate:.2f} per lb"
        elif unit == "oz":
            rate = price / (value / 16)  # Convert ounces to pounds
            return f"${rate:.2f} per lb"
        elif unit == "fl oz":
            rate = price / (value / 128)  # Convert fluid ounces to gallons
            return f"${rate:.2f} per gallon"
        elif unit == "gal":
            rate = price / value  # Rate per gallon
            return f"${rate:.2f} per gallon"
        else:
            return "Rate not applicable"  # Not a weight- or volume-based unit
    except Exception as e:
        return f"Error: {e}"

driver = initDriver()

driver.get("https://new.aldi.us/products/dairy-eggs/k/10")
WebDriverWait(driver,60).until(EC.element_to_be_clickable((By.CLASS_NAME, "product-tile")))

try:
    WebDriverWait(driver,10).until(EC.element_to_be_clickable((By.ID, 'onetrust-accept-btn-handler')))
    driver.find_element(By.ID, 'onetrust-accept-btn-handler').click()
except:
    ''

next = driver.find_element(By.XPATH, '//a[@aria-label="Next"]')

while next:
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
            rate_per_pound = calculate_rate_per_unit(product_price, product_size)
        else:
            rate_per_pound = "Insufficient data"
        # Print the product details
        print(f"Product: {product_title}\nPrice: ${product_price:.2f}\nSize/Quantity: {product_size}\nRate per Pound: {rate_per_pound}\n")
    print("NEXT")   
    next.click()
    time.sleep(2)
    WebDriverWait(driver,60).until(EC.element_to_be_clickable((By.CLASS_NAME, "product-tile")))
    try:
        next = driver.find_element(By.XPATH, '//a[@aria-label="Next"]')
    except:
        next = None

# Close the browser
driver.close()