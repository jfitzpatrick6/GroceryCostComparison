import requests
from bs4 import BeautifulSoup

# URL of the ALDI storefront
url = 'https://shop.aldi.us/store/aldi/storefront'

# Send a GET request to the URL
response = requests.get(url)

# Check if the request was successful
if response.status_code == 200:
    # Parse the HTML content of the page
    soup = BeautifulSoup(response.text, 'html.parser')
    print(soup)
    
    # Find all product containers
    products = soup.find_all('div', class_='product-card')
    
    # Iterate over each product and extract details
    for product in products:
        # Extract product name
        name = product.find('h3', class_='product-card__title').get_text(strip=True)
        
        # Extract current price
        price = product.find('span', class_='product-card__price').get_text(strip=True)
        
        print(f'Product: {name}\nPrice: {price}\n')
else:
    print(f'Failed to retrieve the page. Status code: {response.status_code}')
