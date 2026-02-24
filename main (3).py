"""
HYPE Intelligence — Scraper Backend v1.0
=========================================
FastAPI backend with real scraping infrastructure.
Start with: uvicorn main:app --reload --port 8000

Requires:
  pip install fastapi uvicorn playwright beautifulsoup4 httpx
  playwright install chromium
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio
import json
import re
import time
from datetime import datetime

app = FastAPI(title="HYPE Intelligence API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── DATA MODELS ───

class PriceResult(BaseModel):
    platform: str
    platform_name: str
    product_name: str
    price: float
    currency: str
    url: str
    seller: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    in_stock: bool = True
    scraped_at: str

class SearchResponse(BaseModel):
    query: str
    region: str
    platforms_searched: list[str]
    results: list[PriceResult]
    total_results: int
    search_time_ms: int


# ─── SCRAPER BASE ───

class BaseScraper:
    """Base class for all platform scrapers."""
    
    PLATFORM_ID = "base"
    PLATFORM_NAME = "Base"
    
    # Common headers to look like a real browser
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    
    async def search(self, query: str, max_results: int = 5) -> list[PriceResult]:
        raise NotImplementedError


# ─── AMAZON US SCRAPER ───

class AmazonUSScraper(BaseScraper):
    """
    Amazon US scraper using httpx + BeautifulSoup.
    Falls back to Playwright for JS-heavy pages.
    """
    
    PLATFORM_ID = "amazon_us"
    PLATFORM_NAME = "Amazon US"
    BASE_URL = "https://www.amazon.com"
    
    async def search(self, query: str, max_results: int = 5) -> list[PriceResult]:
        """Search Amazon US and extract product prices."""
        
        # Strategy 1: Direct HTTP request (fast, cheap)
        results = await self._search_http(query, max_results)
        
        # Strategy 2: If HTTP fails or returns 0, use Playwright (slower, reliable)
        if not results:
            results = await self._search_playwright(query, max_results)
        
        return results
    
    async def _search_http(self, query: str, max_results: int) -> list[PriceResult]:
        """Fast HTTP-based scraping with httpx + BeautifulSoup."""
        try:
            import httpx
            from bs4 import BeautifulSoup
            
            search_url = f"{self.BASE_URL}/s"
            params = {
                "k": query,
                "ref": "nb_sb_noss",
            }
            
            async with httpx.AsyncClient(
                headers=self.HEADERS,
                follow_redirects=True,
                timeout=15.0,
            ) as client:
                response = await client.get(search_url, params=params)
                
                if response.status_code != 200:
                    print(f"[Amazon HTTP] Status {response.status_code}")
                    return []
                
                soup = BeautifulSoup(response.text, "html.parser")
                return self._parse_search_results(soup, max_results)
                
        except Exception as e:
            print(f"[Amazon HTTP] Error: {e}")
            return []
    
    async def _search_playwright(self, query: str, max_results: int) -> list[PriceResult]:
        """Playwright-based scraping for JS-rendered pages."""
        try:
            from playwright.async_api import async_playwright
            from bs4 import BeautifulSoup
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=self.HEADERS["User-Agent"],
                    viewport={"width": 1920, "height": 1080},
                )
                page = await context.new_page()
                
                search_url = f"{self.BASE_URL}/s?k={query.replace(' ', '+')}"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                
                # Wait for product grid to load
                await page.wait_for_selector('[data-component-type="s-search-result"]', timeout=10000)
                
                html = await page.content()
                await browser.close()
                
                soup = BeautifulSoup(html, "html.parser")
                return self._parse_search_results(soup, max_results)
                
        except Exception as e:
            print(f"[Amazon Playwright] Error: {e}")
            return []
    
    def _parse_search_results(self, soup, max_results: int) -> list[PriceResult]:
        """Parse Amazon search results page."""
        from bs4 import BeautifulSoup
        
        results = []
        
        # Amazon uses data-component-type="s-search-result" for each product
        items = soup.select('[data-component-type="s-search-result"]')
        
        for item in items[:max_results * 2]:  # Get extra in case some fail
            if len(results) >= max_results:
                break
                
            try:
                result = self._parse_single_item(item)
                if result:
                    results.append(result)
            except Exception as e:
                print(f"[Amazon Parse] Skipping item: {e}")
                continue
        
        return results
    
    def _parse_single_item(self, item) -> Optional[PriceResult]:
        """Parse a single Amazon search result item."""
        
        # Skip sponsored/ad results
        if item.select_one('.s-label-popover-default'):
            sponsored_text = item.select_one('.s-label-popover-default')
            if sponsored_text and 'Sponsored' in sponsored_text.get_text():
                return None
        
        # Product name
        title_elem = item.select_one('h2 a span') or item.select_one('h2 span')
        if not title_elem:
            return None
        product_name = title_elem.get_text(strip=True)
        
        if not product_name or len(product_name) < 5:
            return None
        
        # Price - Amazon has multiple price formats
        price = self._extract_price(item)
        if not price:
            return None
        
        # Product URL
        link_elem = item.select_one('h2 a')
        url = ""
        if link_elem and link_elem.get('href'):
            href = link_elem['href']
            if href.startswith('/'):
                url = f"{self.BASE_URL}{href}"
            else:
                url = href
        
        # Rating
        rating = None
        rating_elem = item.select_one('[aria-label*="out of 5 stars"]')
        if rating_elem:
            rating_text = rating_elem.get('aria-label', '')
            match = re.search(r'(\d+\.?\d*)\s+out of\s+5', rating_text)
            if match:
                rating = float(match.group(1))
        
        # Review count
        review_count = None
        review_elem = item.select_one('[aria-label*="ratings"]') or item.select_one('.s-link-style .s-underline-text')
        if review_elem:
            review_text = review_elem.get_text(strip=True).replace(',', '')
            match = re.search(r'(\d+)', review_text)
            if match:
                review_count = int(match.group(1))
        
        # Seller
        seller = None
        seller_elem = item.select_one('.a-row.a-size-base.a-color-secondary .a-size-base')
        if seller_elem:
            seller = seller_elem.get_text(strip=True)
        
        return PriceResult(
            platform=self.PLATFORM_ID,
            platform_name=self.PLATFORM_NAME,
            product_name=product_name,
            price=price,
            currency="$",
            url=url,
            seller=seller,
            rating=rating,
            review_count=review_count,
            in_stock=True,
            scraped_at=datetime.utcnow().isoformat(),
        )
    
    def _extract_price(self, item) -> Optional[float]:
        """Extract price from Amazon product item. Handles multiple formats."""
        
        # Format 1: span.a-price > span.a-offscreen (most common)
        price_elem = item.select_one('.a-price:not(.a-text-price) .a-offscreen')
        if price_elem:
            return self._parse_price_text(price_elem.get_text())
        
        # Format 2: span.a-price > span.a-price-whole + span.a-price-fraction
        whole = item.select_one('.a-price:not(.a-text-price) .a-price-whole')
        fraction = item.select_one('.a-price:not(.a-text-price) .a-price-fraction')
        if whole:
            whole_text = whole.get_text(strip=True).replace(',', '').replace('.', '')
            frac_text = fraction.get_text(strip=True) if fraction else "00"
            try:
                return float(f"{whole_text}.{frac_text}")
            except ValueError:
                pass
        
        # Format 3: Generic price pattern in text
        price_container = item.select_one('.a-price')
        if price_container:
            text = price_container.get_text()
            return self._parse_price_text(text)
        
        return None
    
    def _parse_price_text(self, text: str) -> Optional[float]:
        """Parse price from text like '$1,049.99' or '₺42.999,00'."""
        if not text:
            return None
        # Remove currency symbols and whitespace
        cleaned = re.sub(r'[^\d.,]', '', text.strip())
        if not cleaned:
            return None
        
        # US format: 1,049.99
        try:
            # Remove commas used as thousand separators
            if '.' in cleaned and ',' in cleaned:
                # Determine format by position
                if cleaned.rindex('.') > cleaned.rindex(','):
                    # US: 1,049.99
                    cleaned = cleaned.replace(',', '')
                else:
                    # EU: 1.049,99
                    cleaned = cleaned.replace('.', '').replace(',', '.')
            elif ',' in cleaned and '.' not in cleaned:
                # Could be thousand separator (1,049) or decimal (49,99)
                parts = cleaned.split(',')
                if len(parts[-1]) == 2:
                    cleaned = cleaned.replace(',', '.')
                else:
                    cleaned = cleaned.replace(',', '')
            
            return float(cleaned)
        except ValueError:
            return None


# ─── WALMART SCRAPER ───

class WalmartScraper(BaseScraper):
    """Walmart scraper."""
    
    PLATFORM_ID = "walmart"
    PLATFORM_NAME = "Walmart"
    BASE_URL = "https://www.walmart.com"
    
    async def search(self, query: str, max_results: int = 5) -> list[PriceResult]:
        try:
            import httpx
            from bs4 import BeautifulSoup
            
            search_url = f"{self.BASE_URL}/search"
            params = {"q": query}
            
            async with httpx.AsyncClient(
                headers={**self.HEADERS, "Accept": "text/html"},
                follow_redirects=True,
                timeout=15.0,
            ) as client:
                response = await client.get(search_url, params=params)
                
                if response.status_code != 200:
                    print(f"[Walmart] Status {response.status_code}")
                    return []
                
                soup = BeautifulSoup(response.text, "html.parser")
                return self._parse_results(soup, max_results)
                
        except Exception as e:
            print(f"[Walmart] Error: {e}")
            return []
    
    def _parse_results(self, soup, max_results: int) -> list[PriceResult]:
        results = []
        
        # Walmart uses [data-item-id] for product cards
        items = soup.select('[data-item-id]')
        
        for item in items[:max_results * 2]:
            if len(results) >= max_results:
                break
            try:
                # Title
                title_elem = item.select_one('[data-automation-id="product-title"]') or item.select_one('a span')
                if not title_elem:
                    continue
                name = title_elem.get_text(strip=True)
                if len(name) < 5:
                    continue
                
                # Price
                price_elem = item.select_one('[data-automation-id="product-price"] .f2') or item.select_one('[itemprop="price"]')
                if not price_elem:
                    continue
                price_text = price_elem.get_text(strip=True)
                price = self._parse_price(price_text)
                if not price:
                    continue
                
                # URL
                link = item.select_one('a[href*="/ip/"]')
                url = f"{self.BASE_URL}{link['href']}" if link and link.get('href', '').startswith('/') else ""
                
                results.append(PriceResult(
                    platform=self.PLATFORM_ID,
                    platform_name=self.PLATFORM_NAME,
                    product_name=name,
                    price=price,
                    currency="$",
                    url=url,
                    scraped_at=datetime.utcnow().isoformat(),
                ))
            except Exception as e:
                continue
        
        return results
    
    def _parse_price(self, text: str) -> Optional[float]:
        cleaned = re.sub(r'[^\d.,]', '', text)
        try:
            return float(cleaned.replace(',', ''))
        except:
            return None


# ─── BEST BUY SCRAPER ───

class BestBuyScraper(BaseScraper):
    """Best Buy scraper."""
    
    PLATFORM_ID = "bestbuy"
    PLATFORM_NAME = "Best Buy"
    BASE_URL = "https://www.bestbuy.com"
    
    async def search(self, query: str, max_results: int = 5) -> list[PriceResult]:
        try:
            import httpx
            from bs4 import BeautifulSoup
            
            search_url = f"{self.BASE_URL}/site/searchpage.jsp"
            params = {"st": query}
            
            async with httpx.AsyncClient(
                headers=self.HEADERS,
                follow_redirects=True,
                timeout=15.0,
            ) as client:
                response = await client.get(search_url, params=params)
                
                if response.status_code != 200:
                    return []
                
                soup = BeautifulSoup(response.text, "html.parser")
                return self._parse_results(soup, max_results)
                
        except Exception as e:
            print(f"[BestBuy] Error: {e}")
            return []
    
    def _parse_results(self, soup, max_results: int) -> list[PriceResult]:
        results = []
        items = soup.select('.sku-item') or soup.select('[class*="sku-item"]')
        
        for item in items[:max_results * 2]:
            if len(results) >= max_results:
                break
            try:
                title_elem = item.select_one('.sku-title a') or item.select_one('h4 a')
                if not title_elem:
                    continue
                name = title_elem.get_text(strip=True)
                
                price_elem = item.select_one('.priceView-customer-price span') or item.select_one('[data-testid="customer-price"] span')
                if not price_elem:
                    continue
                price_text = price_elem.get_text(strip=True)
                cleaned = re.sub(r'[^\d.,]', '', price_text)
                price = float(cleaned.replace(',', ''))
                
                href = title_elem.get('href', '')
                url = f"{self.BASE_URL}{href}" if href.startswith('/') else href
                
                results.append(PriceResult(
                    platform=self.PLATFORM_ID,
                    platform_name=self.PLATFORM_NAME,
                    product_name=name,
                    price=price,
                    currency="$",
                    url=url,
                    scraped_at=datetime.utcnow().isoformat(),
                ))
            except:
                continue
        
        return results


# ─── TRENDYOL SCRAPER ───

class TrendyolScraper(BaseScraper):
    """Trendyol scraper for Turkish market."""
    
    PLATFORM_ID = "trendyol"
    PLATFORM_NAME = "Trendyol"
    BASE_URL = "https://www.trendyol.com"
    
    async def search(self, query: str, max_results: int = 5) -> list[PriceResult]:
        try:
            import httpx
            from bs4 import BeautifulSoup
            
            search_url = f"{self.BASE_URL}/sr"
            params = {"q": query}
            
            headers = {**self.HEADERS, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"}
            
            async with httpx.AsyncClient(
                headers=headers,
                follow_redirects=True,
                timeout=15.0,
            ) as client:
                response = await client.get(search_url, params=params)
                
                if response.status_code != 200:
                    print(f"[Trendyol] Status {response.status_code}")
                    return []
                
                soup = BeautifulSoup(response.text, "html.parser")
                return self._parse_results(soup, max_results)
                
        except Exception as e:
            print(f"[Trendyol] Error: {e}")
            return []
    
    def _parse_results(self, soup, max_results: int) -> list[PriceResult]:
        results = []
        items = soup.select('.p-card-wrppr') or soup.select('[class*="prdct-cntnr"]')
        
        for item in items[:max_results * 2]:
            if len(results) >= max_results:
                break
            try:
                # Title
                title_elem = item.select_one('.prdct-desc-cntnr-name') or item.select_one('span[class*="prdct-desc"]')
                brand_elem = item.select_one('.prdct-desc-cntnr-ttl') or item.select_one('span[class*="brand"]')
                
                brand = brand_elem.get_text(strip=True) if brand_elem else ""
                title = title_elem.get_text(strip=True) if title_elem else ""
                name = f"{brand} {title}".strip()
                if len(name) < 3:
                    continue
                
                # Price - Trendyol uses Turkish format: 42.999,00 TL
                price_elem = item.select_one('.prc-box-dscntd') or item.select_one('.prc-box-sllng')
                if not price_elem:
                    continue
                
                price_text = price_elem.get_text(strip=True)
                price = self._parse_turkish_price(price_text)
                if not price:
                    continue
                
                # URL
                link = item.select_one('a')
                href = link.get('href', '') if link else ''
                url = f"{self.BASE_URL}{href}" if href.startswith('/') else href
                
                results.append(PriceResult(
                    platform=self.PLATFORM_ID,
                    platform_name=self.PLATFORM_NAME,
                    product_name=name,
                    price=price,
                    currency="₺",
                    url=url,
                    scraped_at=datetime.utcnow().isoformat(),
                ))
            except:
                continue
        
        return results
    
    def _parse_turkish_price(self, text: str) -> Optional[float]:
        """Parse Turkish price format: 42.999,00 TL → 42999.00"""
        cleaned = re.sub(r'[^\d.,]', '', text)
        if not cleaned:
            return None
        try:
            # Turkish: dots are thousands, comma is decimal
            cleaned = cleaned.replace('.', '').replace(',', '.')
            return float(cleaned)
        except:
            return None


# ─── HEPSIBURADA SCRAPER ───

class HepsiburadaScraper(BaseScraper):
    """Hepsiburada scraper."""
    
    PLATFORM_ID = "hepsiburada"
    PLATFORM_NAME = "Hepsiburada"
    BASE_URL = "https://www.hepsiburada.com"
    
    async def search(self, query: str, max_results: int = 5) -> list[PriceResult]:
        try:
            import httpx
            from bs4 import BeautifulSoup
            
            search_url = f"{self.BASE_URL}/ara"
            params = {"q": query}
            headers = {**self.HEADERS, "Accept-Language": "tr-TR,tr;q=0.9"}
            
            async with httpx.AsyncClient(
                headers=headers,
                follow_redirects=True,
                timeout=15.0,
            ) as client:
                response = await client.get(search_url, params=params)
                
                if response.status_code != 200:
                    return []
                
                soup = BeautifulSoup(response.text, "html.parser")
                return self._parse_results(soup, max_results)
                
        except Exception as e:
            print(f"[Hepsiburada] Error: {e}")
            return []
    
    def _parse_results(self, soup, max_results: int) -> list[PriceResult]:
        results = []
        items = soup.select('[data-test-id="product-card-item"]') or soup.select('.productListContent-item')
        
        for item in items[:max_results * 2]:
            if len(results) >= max_results:
                break
            try:
                title_elem = item.select_one('[data-test-id="product-card-name"]') or item.select_one('h3')
                if not title_elem:
                    continue
                name = title_elem.get_text(strip=True)
                
                price_elem = item.select_one('[data-test-id="price-current-price"]') or item.select_one('.product-price')
                if not price_elem:
                    continue
                price_text = price_elem.get_text(strip=True)
                cleaned = re.sub(r'[^\d.,]', '', price_text)
                price = float(cleaned.replace('.', '').replace(',', '.'))
                
                link = item.select_one('a')
                href = link.get('href', '') if link else ''
                url = f"{self.BASE_URL}{href}" if href and not href.startswith('http') else href
                
                results.append(PriceResult(
                    platform=self.PLATFORM_ID,
                    platform_name=self.PLATFORM_NAME,
                    product_name=name,
                    price=price,
                    currency="₺",
                    url=url,
                    scraped_at=datetime.utcnow().isoformat(),
                ))
            except:
                continue
        
        return results


# ─── AMAZON DE SCRAPER ───

class AmazonDEScraper(AmazonUSScraper):
    """Amazon DE - inherits Amazon US logic with DE-specific config."""
    
    PLATFORM_ID = "amazon_de"
    PLATFORM_NAME = "Amazon DE"
    BASE_URL = "https://www.amazon.de"
    
    def _extract_price(self, item) -> Optional[float]:
        """Override for EU price format."""
        price_elem = item.select_one('.a-price:not(.a-text-price) .a-offscreen')
        if price_elem:
            text = price_elem.get_text()
            # EU format: 1.199,00 €
            cleaned = re.sub(r'[^\d.,]', '', text)
            if cleaned:
                try:
                    return float(cleaned.replace('.', '').replace(',', '.'))
                except:
                    pass
        return super()._extract_price(item)


# ─── SCRAPER REGISTRY ───

SCRAPERS = {
    "us": {
        "amazon_us": AmazonUSScraper(),
        "walmart": WalmartScraper(),
        "bestbuy": BestBuyScraper(),
    },
    "tr": {
        "trendyol": TrendyolScraper(),
        "hepsiburada": HepsiburadaScraper(),
    },
    "eu": {
        "amazon_de": AmazonDEScraper(),
    },
}


# ─── API ENDPOINTS ───

@app.get("/")
async def root():
    return {
        "name": "HYPE Intelligence API",
        "version": "1.0.0",
        "status": "running",
        "supported_regions": list(SCRAPERS.keys()),
    }


@app.get("/api/search", response_model=SearchResponse)
async def search_products(
    q: str = Query(..., description="Search query", min_length=1),
    region: str = Query("us", description="Region: us, tr, eu"),
    platforms: Optional[str] = Query(None, description="Comma-separated platform IDs. If empty, searches all."),
    max_results: int = Query(5, description="Max results per platform", ge=1, le=20),
):
    """
    Search for products across platforms in a region.
    
    Example:
      GET /api/search?q=iPhone+16+Pro&region=us&max_results=5
      GET /api/search?q=iPhone+16+Pro&region=us&platforms=amazon_us,walmart
    """
    
    if region not in SCRAPERS:
        raise HTTPException(400, f"Unsupported region: {region}. Use: {list(SCRAPERS.keys())}")
    
    region_scrapers = SCRAPERS[region]
    
    # Filter platforms if specified
    if platforms:
        platform_ids = [p.strip() for p in platforms.split(",")]
        target_scrapers = {k: v for k, v in region_scrapers.items() if k in platform_ids}
    else:
        target_scrapers = region_scrapers
    
    if not target_scrapers:
        raise HTTPException(400, f"No valid platforms found. Available: {list(region_scrapers.keys())}")
    
    start = time.time()
    
    # Run all scrapers concurrently
    tasks = []
    for platform_id, scraper in target_scrapers.items():
        tasks.append(scraper.search(q, max_results))
    
    all_results_nested = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Flatten and filter errors
    all_results = []
    for result in all_results_nested:
        if isinstance(result, Exception):
            print(f"Scraper error: {result}")
            continue
        all_results.extend(result)
    
    elapsed_ms = int((time.time() - start) * 1000)
    
    return SearchResponse(
        query=q,
        region=region,
        platforms_searched=list(target_scrapers.keys()),
        results=all_results,
        total_results=len(all_results),
        search_time_ms=elapsed_ms,
    )


@app.get("/api/platforms")
async def list_platforms(region: Optional[str] = None):
    """List available platforms, optionally filtered by region."""
    if region:
        if region not in SCRAPERS:
            raise HTTPException(400, f"Unsupported region: {region}")
        return {
            "region": region,
            "platforms": [
                {"id": k, "name": v.PLATFORM_NAME}
                for k, v in SCRAPERS[region].items()
            ],
        }
    
    return {
        "regions": {
            reg: [{"id": k, "name": v.PLATFORM_NAME} for k, v in scrapers.items()]
            for reg, scrapers in SCRAPERS.items()
        }
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─── RUN ───

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
