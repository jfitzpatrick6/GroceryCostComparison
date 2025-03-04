import asyncio
import json
import math
import httpx
from typing import List, Dict
from loguru import logger as log
from parsel import Selector
import random
import re
import pandas as pd

URLS = ["https://www.walmart.com/browse/food-grocery/3734780_7455738?&page=PAGE&affinityOverride=default","https://www.walmart.com/search?q=meat+%26+seafood&page=PAGE",
        "https://www.walmart.com/browse/food/fresh-produce/976759_976793?&page=PAGE", "https://www.walmart.com/browse/food/all-tea/976759_976782_1001320_9254040?&page=PAGE",
        "https://www.walmart.com/browse/baking/976759_976780?&page=PAGE", "https://www.walmart.com/browse/food/frozen-fruits-vegetables/976759_976791_5624760?&page=PAGE",
        "https://www.walmart.com/browse/dairy-eggs/976759_9176907?&page=PAGE", "https://www.walmart.com/browse/bakery-bread/976759_976779?&page=PAGE",
        "https://www.walmart.com/browse/beverages/976759_976782?&page=PAGE", "https://www.walmart.com/browse/pantry/976759_976794?&page=PAGE",
        "https://www.walmart.com/browse/deli/976759_976789?&page=PAGE", "https://www.walmart.com/browse/snacks-cookies-chips/976759_976787?&page=PAGE",
        "https://www.walmart.com/browse/alcohol/976759_2975985?&page=PAGE", "https://www.walmart.com/browse/coffee/976759_1086446?&page=PAGE"]

def parse_search(html_text:str) -> Dict:
    """extract search results from search HTML response"""
    sel = Selector(text=html_text)
    data = sel.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
    data = json.loads(data)

    total_results = data["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"][0]["count"]
    results = data["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"][0]["items"]
    return results, total_results

async def scrape_walmart_page(session:httpx.AsyncClient, query:str="", page=1, sort="price_low", url=""):
    """scrape a single walmart search page"""
    url = url.replace("PAGE", str(page))
    await asyncio.sleep(random.uniform(2.3, 6.5))
    resp = await session.get(url)
    assert resp.status_code == 200, "request is blocked"
    return resp


async def scrape_search(search_query: str, session: httpx.AsyncClient, max_scrape_pages: int = None) -> List[Dict]:
    """Scrape Walmart search pages."""
    results = []
    
    for url in URLS:
        # Scrape the first search page
        _resp_page1 = await scrape_walmart_page(query=search_query, session=session, url=url)
        page_results, total_items = parse_search(_resp_page1.text)
        results.extend(page_results)  # Append initial results

        # Determine max pages
        max_page = math.ceil(total_items / 40)
        max_page = min(max_page, 25)  # Walmart limits to 25 pages

        # Scrape additional pages in parallel
        for i in range(2, max_page + 1):
            response = await scrape_walmart_page(query=search_query, page=i, session=session, url=url)
            page_results, _ = parse_search(response.text)
            results.extend(page_results)  # Append results from the page
    return results

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.",
]

BASE_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
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

cookies = {
    "wmtlabs:reflectorid": "22222222224468469175"
}

frame = []

async def run():
    # limit connection speed to prevent scraping too fast
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=5)
    BASE_HEADERS["user-agent"] = random.choice(USER_AGENTS)
    client_session =  httpx.AsyncClient(headers=BASE_HEADERS, limits=limits, cookies=cookies)
    # run the scrape_search function
    data = await scrape_search(search_query="eggs", session=client_session, max_scrape_pages=3)
    # save the results into a JSON file "walmart_search.json"
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

def convert_price(price_str):
    """
    Convert a price string from cents per ounce (e.g. "11.7 Â¢/oz")
    to a formatted string in dollars per ounce (e.g. "$0.117/oz").
    If conversion is not applicable, return the original string.
    """
    if isinstance(price_str, str):
        # Remove any stray encoding characters (like "Â")
        clean_str = price_str.replace("Â", "").strip()
        # Look for a pattern matching something like "11.7 ¢/oz"
        match = re.search(r"([\d.]+)\s*¢\/(.+)", clean_str)
        if match:
            cents = float(match.group(1))
            dollars_per_oz = cents / 100.0
            return f"${dollars_per_oz:.3f}/{match.group(2)}"
    return price_str

asyncio.run(run())
df_all = pd.concat(frame)
if "Rate" in df_all.columns:
    df_all["Rate"] = df_all["Price"].apply(convert_price)
df_all.to_csv("Walmart.csv", index=False)