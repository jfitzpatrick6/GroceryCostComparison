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

url = "https://www.walmart.com/browse/food-grocery/3734780_7455738?&page=1&affinityOverride=default"

def initDriver(options=''):
    options = webdriver.ChromeOptions()
    options.add_argument('--profile-directory=Default')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument("user-data-dir=Y:\\SHARE\\ECM\\Development\\Code\\PSEGinvoices\\User Data2\\")
    options.add_argument("--start-maximized")
    service = Service(ChromeDriverManager().install().replace('THIRD_PARTY_NOTICES.chromedriver', 'chromedriver.exe'))
    driver = webdriver.Chrome(service=service, options=options)
    return driver

driver = initDriver()
driver.get(url)
time.sleep(100)