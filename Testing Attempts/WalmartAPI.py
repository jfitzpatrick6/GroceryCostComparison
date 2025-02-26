import asyncio
import json
import math
import httpx
from typing import List, Dict
import random
import re
import pandas as pd
from urllib.parse import quote

class WalmartAPI:
    def __init__(self):
        self.base_headers = {
            "authority": "www.walmart.com",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "x-requested-with": "XMLHttpRequest",
            "referer": "https://www.walmart.com/",
        }

        cookies = {
            "wmtlabs:reflectorid": "22222222224468469175"
        }

        self.graphql_hash = self.get_current_graphql_hash()
        self.session = httpx.AsyncClient(
            headers=self.base_headers,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=5),
            http2=True,
            cookies=cookies
        )

        self.session.headers.update({
            "x-client-data": "CJK2yQEIo7bJAQjEtskBCKmdygEIuv3KAQjr+soB",
            "x-wm-page-version": "1.0.0",
            "x-wm-cx-source": "walmart-web"
        })

    def get_current_graphql_hash(self):
        """Extract latest GraphQL hash from page source (update periodically)"""
        return "eb372d5d7e01c660513894e686ce7b16a761cbef5ab67b9b94f4a1867c5d2088"

    async def get_api_url(self, category_id: str, page: int):
        variables = {
            "catId": category_id,
            "page": page,
            "sort": "best_match",
            "prg": "desktop",
            "ps": 40,
            "limit": 40,
            "fSeo": True,
            "tenant": "WM_GLASS",
            "enableFacetCount": True,
            "searchArgs": {"cat_id": category_id}
        }
        encoded_vars = quote(json.dumps(variables, separators=(",", ":")))
        return f"https://www.walmart.com/orchestra/snb/graphql/Browse/{self.graphql_hash}/browse?variables={encoded_vars}"

    async def fetch_category(self, category_id: str, max_pages: int = 3):
        results = []
        try:
            # Initial page visit to set cookies
            await self.session.get("https://www.walmart.com")
            
            for page in range(1, max_pages + 1):
                url = await self.get_api_url(category_id, page)
                response = await self.session.get(url)
                
                if response.status_code == 418:
                    raise Exception("Bot detected - refresh cookies/hash")
                
                data = response.json()
                results.extend(self.parse_api_response(data))
                
                await asyncio.sleep(random.uniform(1.2, 2.5))
        
        except Exception as e:
            print(f"Error fetching {category_id}: {str(e)}")
        
        return results

    def parse_api_response(self, data: Dict):
        products = []
        for item in data.get("data", {}).get("browse", {}).get("products", []):
            try:
                product = {
                    "name": item.get("name"),
                    "price": item.get("priceInfo", {}).get("currentPrice"),
                    "unit": item.get("priceInfo", {}).get("unitPriceDisplay"),
                    "size": self.extract_size(item.get("name", "")),
                    "url": f"https://walmart.com{item.get('canonicalUrl', '')}"
                }
                products.append(product)
            except Exception as e:
                print(f"Error parsing item: {str(e)}")
        return products

    def extract_size(self, product_name: str):
        patterns = [
            r"(\d+\.?\d*)\s?(oz|fl oz|lb|gallon|gal|each|ct|count|pk)",
            r"(\d+)-?(pack|count|ct|pc)",
            r"(\d+\.?\d*)\s?(ounce|pound|liter|quart)s?"
        ]
        for pattern in patterns:
            match = re.search(pattern, product_name, re.IGNORECASE)
            if match:
                return f"{match.group(1)} {match.group(2)}".lower()
        return "N/A"

async def main():
    walmart = WalmartAPI()
    categories = {
        "3734780_7455738": "Food & Grocery",
        "976759_976782": "Beverages",
        "976759_976780": "Baking"
    }
    
    all_products = []
    for cat_id, cat_name in categories.items():
        print(f"Scraping {cat_name}...")
        products = await walmart.fetch_category(cat_id)
        all_products.extend(products)
    
    df = pd.DataFrame(all_products)
    df.to_csv("walmart_products.csv", index=False)
    print(f"Saved {len(df)} products")

if __name__ == "__main__":
    asyncio.run(main())