import asyncio
import json
import math
import httpx
from typing import List, Dict
from loguru import logger as log
from parsel import Selector

def parse_search(html_text:str) -> Dict:
    """extract search results from search HTML response"""
    sel = Selector(text=html_text)
    data = sel.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
    data = json.loads(data)

    total_results = data["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"][0]["count"]
    results = data["props"]["pageProps"]["initialData"]["searchResult"]["itemStacks"][0]["items"]
    return results, total_results

async def scrape_walmart_page(session:httpx.AsyncClient, query:str="", page=1, sort="price_low"):
    """scrape a single walmart search page"""
    url = f"https://www.walmart.com/browse/food-grocery/3734780_7455738?seo=food-grocery&seo=3734780_7455738&page={page}&affinityOverride=default"
    resp = await session.get(url)
    assert resp.status_code == 200, "request is blocked"
    return resp


async def scrape_search(search_query:str, session:httpx.AsyncClient, max_scrape_pages:int=None) -> List[Dict]:
    """scrape Walmart search pages"""
    # scrape the first search page first
    _resp_page1 = await scrape_walmart_page(query=search_query, session=session)
    results, total_items = parse_search(_resp_page1.text)
    max_page = math.ceil(total_items / 40)
    if max_page > 25: # the max number of pages is 25
        max_page = 25
    
    # scrape the remaining search pages
    log.info(f"scraped the first search, remaining ({max_page-1}) more pages")
    for response in await asyncio.gather(
        *[scrape_walmart_page(query=search_query, page=i, session=session) for i in range(2, max_page)]
    ):
        results.extend(parse_search(response.text)[0])
    log.success(f"scraped {len(results)} products from walmart search")
    return results

BASE_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US;en;q=0.9",
    "accept-encoding": "gzip, deflate",
}

async def run():
    # limit connection speed to prevent scraping too fast
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=5)
    client_session =  httpx.AsyncClient(headers=BASE_HEADERS, limits=limits)
    # run the scrape_search function
    data = await scrape_search(search_query="eggs", session=client_session, max_scrape_pages=3)
    # save the results into a JSON file "walmart_search.json"
    for product in data:
        print(product.get("name", ''), product.get("priceInfo", {}).get('linePrice', ''))
    with open("walmart_search.json", "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    asyncio.run(run())