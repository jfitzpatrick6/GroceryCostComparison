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

BASE_HEADERS = {
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.99 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "accept-encoding": "gzip, deflate, br",  # Add Brotli compression (br), as modern browsers use it.
    "referer": "https://www.walmart.com/",  # Walmart expects internal navigation.
    "origin": "https://www.walmart.com",  # Some sites use this to validate requests.
    "dnt": "1",  # Do Not Track (optional, but some browsers send it).
    "sec-fetch-dest": "document",  # Helps mimic real browser requests.
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "connection": "keep-alive",  # Ensures persistent connections.
    "upgrade-insecure-requests": "1",  # Indicates support for secure connections.
    "cache-control": "max-age=0",  # Ensures fresh content is always fetched.
    "pragma": "no-cache",  # Prevents caching (alternative to cache-control).
}

def initDriver(options=''):
    service = Service(ChromeDriverManager().install().replace('THIRD_PARTY_NOTICES.chromedriver', 'chromedriver.exe'))
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": BASE_HEADERS})
    return driver

def calculate_rate_per_unit(price, size_quantity):
    size_quantity = size_quantity.lower()
    try:
        # Extract numeric value and unit using regex
        match = re.search(r"(\d+(\.\d+)?)\s*(lb|oz|fl oz|gal|each|ct|count|dozen)", size_quantity)
        if not match:
            return "Rate not applicable"
        
        calc_match = re.search(r"(\d+)\s*?x\s*?([\d.]+)", size_quantity)
        if calc_match:
            value = float(calc_match.group(1)) * float(calc_match.group(2))
        else:
            value = float(match.group(1))
        unit = match.group(3)

        if unit == "dozen" or unit == "count" or unit == "ct":
            value = value * 12 if unit == "dozen" else value
            unit = "each"

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
        elif unit == "each":
            rate = price / value  # Rate per gallon
            return f"${rate:.2f} per item"
        else:
            return "Rate not applicable"  # Not a weight- or volume-based unit
    except Exception as e:
        return f"Error: {e}"

driver = initDriver()
data = []
urls = [
    "https://new.aldi.us/products/personal-care/k/17",
    "https://new.aldi.us/products/household-essentials/k/15",
    "https://new.aldi.us/products/pet-supplies/k/18",
    "https://new.aldi.us/products/baby-items/k/5",
    "https://new.aldi.us/products/bbq-picnic/k/234",
    "https://new.aldi.us/products/beverages/k/7",
    "https://new.aldi.us/products/pantry-essentials/k/16",
    "https://new.aldi.us/products/breakfast-cereals/k/9",
    "https://new.aldi.us/products/snacks/k/20",
    "https://new.aldi.us/products/bakery-bread/k/6",
    "https://new.aldi.us/products/frozen-foods/k/14",
    "https://new.aldi.us/products/deli/k/11",
    "https://new.aldi.us/products/fresh-produce/k/13",
    "https://new.aldi.us/products/dairy-eggs/k/10",
    "https://new.aldi.us/products/fresh-meat-seafood/k/12",
    "https://new.aldi.us/products/aldi-finds/k/2",
    "https://new.aldi.us/products/game-day/k/249",
    "https://new.aldi.us/products/healthy-living/k/208",
    "https://new.aldi.us/products/featured/k/228"
]

for url in urls:
    driver.get(url)
    try:
        WebDriverWait(driver,60).until(EC.element_to_be_clickable((By.CLASS_NAME, "product-tile")))

        try:
            WebDriverWait(driver,10).until(EC.element_to_be_clickable((By.ID, 'onetrust-accept-btn-handler')))
            driver.find_element(By.ID, 'onetrust-accept-btn-handler').click()
        except:
            ''

        try:
            next = driver.find_element(By.XPATH, '//a[@aria-label="Next"]')
        except:
            next = True

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
                    match = re.search(r"(\d+(\.\d+)?\s*(lb|oz|fl oz|gal|each|ct|count|dozen))", product_title)
                    if match:
                        product_size = match.group(1)
                    else:
                        product_size = "Size/quantity not found"

                if product_price and product_size:
                    rate_per_pound = calculate_rate_per_unit(product_price, product_size)
                else:
                    rate_per_pound = "Insufficient data"
                # Print the product details
                print(f"Product: {product_title}\nPrice: ${product_price:.2f}\nSize/Quantity: {product_size}\nRate per Pound: {rate_per_pound}\n")
                data.append(pd.DataFrame.from_dict({"Product": [product_title], "Price": [product_price], "Rate": [rate_per_pound], "Size": [product_size]}))
            print("NEXT")
            if next != True:   
                next.click()
            time.sleep(2)
            WebDriverWait(driver,60).until(EC.element_to_be_clickable((By.CLASS_NAME, "product-tile")))
            try:
                next = driver.find_element(By.XPATH, '//a[@aria-label="Next"]')
            except:
                next = None
    except:
        ''

# Close the browser
driver.close()
pd.concat(data).to_csv("Aldis.csv", index=False)