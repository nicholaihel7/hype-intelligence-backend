"""
HYPE Intelligence — Scraper Backend v2.0
=========================================
Playwright stealth + real browser scraping.
uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio
import json
import re
import time
import random
from datetime import datetime

app = FastAPI(title="HYPE Intelligence API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── MODELS ───

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
    image_url: Optional[str] = None
    in_stock: bool = True
    scraped_at: str

class SearchResponse(BaseModel):
    query: str
    region: str
    platforms_searched: list[str]
    results: list[PriceResult]
    total_results: int
    search_time_ms: int


# ─── STEALTH BROWSER ───

async def get_stealth_page(playwright):
    """Launch a stealth browser that looks like a real user."""
    
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-accelerated-2d-canvas",
            "--disable-gpu",
        ]
    )
    
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="America/New_York",
        permissions=["geolocation"],
        java_script_enabled=True,
        has_touch=False,
        is_mobile=False,
        color_scheme="light",
    )
    
    # Stealth: Override navigator.webdriver
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
        
        // Override permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
    """)
    
    page = await context.new_page()
    return browser, page


async def stealth_goto(page, url, wait_selector=None, timeout=20000):
    """Navigate with random delays to mimic human behavior."""
    
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    
    # Random small delay like a human would have
    await asyncio.sleep(random.uniform(1.0, 2.5))
    
    if wait_selector:
        try:
            await page.wait_for_selector(wait_selector, timeout=10000)
        except:
            # Selector not found, continue anyway
            await asyncio.sleep(1)
    
    # Scroll down a bit like a human
    await page.evaluate("window.scrollBy(0, 300)")
    await asyncio.sleep(random.uniform(0.3, 0.8))
    
    return await page.content()


# ─── PARSERS ───

def parse_amazon_results(html, max_results=5, currency="$", platform_id="amazon_us", platform_name="Amazon US"):
    """Parse Amazon search results HTML."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    items = soup.select('[data-component-type="s-search-result"]')
    
    for item in items:
        if len(results) >= max_results:
            break
        
        try:
            # Skip sponsored
            sponsored = item.select_one('[data-component-type="sp-sponsored-result"]')
            if sponsored:
                continue
            ad_badge = item.find(string=re.compile(r"Sponsored", re.I))
            if ad_badge:
                continue
            
            # Title
            title_el = item.select_one("h2 a span") or item.select_one("h2 span")
            if not title_el:
                continue
            name = title_el.get_text(strip=True)
            if len(name) < 5:
                continue
            
            # Price
            price = None
            
            # Method 1: a-offscreen
            offscreen = item.select_one(".a-price:not(.a-text-price) .a-offscreen")
            if offscreen:
                price = parse_price(offscreen.get_text(), currency)
            
            # Method 2: whole + fraction
            if not price:
                whole_el = item.select_one(".a-price:not(.a-text-price) .a-price-whole")
                frac_el = item.select_one(".a-price:not(.a-text-price) .a-price-fraction")
                if whole_el:
                    w = whole_el.get_text(strip=True).replace(",", "").replace(".", "")
                    f = frac_el.get_text(strip=True) if frac_el else "00"
                    try:
                        price = float(f"{w}.{f}")
                    except:
                        pass
            
            if not price:
                continue
            
            # URL
            link_el = item.select_one("h2 a")
            url = ""
            if link_el and link_el.get("href"):
                href = link_el["href"]
                base = "https://www.amazon.com" if platform_id == "amazon_us" else "https://www.amazon.de"
                url = f"{base}{href}" if href.startswith("/") else href
            
            # Rating
            rating = None
            rating_el = item.select_one("[aria-label*='out of 5']")
            if rating_el:
                m = re.search(r"(\d+\.?\d*)\s+out", rating_el.get("aria-label", ""))
                if m:
                    rating = float(m.group(1))
            
            # Image
            img = None
            img_el = item.select_one("img.s-image")
            if img_el:
                img = img_el.get("src")
            
            results.append(PriceResult(
                platform=platform_id,
                platform_name=platform_name,
                product_name=name,
                price=price,
                currency=currency,
                url=url,
                rating=rating,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
            ))
        except Exception as e:
            continue
    
    return results


def parse_walmart_results(html, max_results=5):
    """Parse Walmart search results."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    # Try JSON-LD first (Walmart embeds structured data)
    scripts = soup.select('script[type="application/ld+json"]')
    for script in scripts:
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                for item in data.get("itemListElement", [])[:max_results]:
                    offer = item.get("offers", {})
                    results.append(PriceResult(
                        platform="walmart",
                        platform_name="Walmart",
                        product_name=item.get("name", ""),
                        price=float(offer.get("price", 0)),
                        currency="$",
                        url=item.get("url", ""),
                        image_url=item.get("image", ""),
                        scraped_at=datetime.utcnow().isoformat(),
                    ))
        except:
            continue
    
    if results:
        return results
    
    # Fallback: HTML parsing
    items = soup.select('[data-item-id]') or soup.select('[link-identifier]')
    
    for item in items:
        if len(results) >= max_results:
            break
        try:
            title_el = item.select_one('[data-automation-id="product-title"]') or item.select_one("span.lh-title")
            if not title_el:
                continue
            name = title_el.get_text(strip=True)
            
            price_el = item.select_one('[data-automation-id="product-price"] .f2') or item.select_one('[itemprop="price"]')
            if not price_el:
                # Try any element with $ sign
                all_text = item.get_text()
                m = re.search(r"\$(\d+(?:,\d{3})*(?:\.\d{2})?)", all_text)
                if m:
                    price = float(m.group(1).replace(",", ""))
                else:
                    continue
            else:
                price = parse_price(price_el.get_text(), "$")
                if not price:
                    continue
            
            link = item.select_one('a[href*="/ip/"]')
            url = f"https://www.walmart.com{link['href']}" if link and link.get("href", "").startswith("/") else ""
            
            img = None
            img_el = item.select_one("img[data-testid='productTileImage']") or item.select_one("img")
            if img_el:
                img = img_el.get("src")
            
            results.append(PriceResult(
                platform="walmart",
                platform_name="Walmart",
                product_name=name,
                price=price,
                currency="$",
                url=url,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
            ))
        except:
            continue
    
    return results


def parse_bestbuy_results(html, max_results=5):
    """Parse Best Buy search results."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    items = soup.select(".sku-item") or soup.select('[class*="sku-item"]') or soup.select(".list-item")
    
    for item in items:
        if len(results) >= max_results:
            break
        try:
            title_el = item.select_one(".sku-title a") or item.select_one("h4 a")
            if not title_el:
                continue
            name = title_el.get_text(strip=True)
            
            price_el = item.select_one(".priceView-customer-price span") or item.select_one('[data-testid="customer-price"] span')
            if not price_el:
                continue
            price = parse_price(price_el.get_text(), "$")
            if not price:
                continue
            
            href = title_el.get("href", "")
            url = f"https://www.bestbuy.com{href}" if href.startswith("/") else href
            
            img = None
            img_el = item.select_one(".product-image img") or item.select_one("img")
            if img_el:
                img = img_el.get("src")
            
            results.append(PriceResult(
                platform="bestbuy",
                platform_name="Best Buy",
                product_name=name,
                price=price,
                currency="$",
                url=url,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
            ))
        except:
            continue
    
    return results


def parse_trendyol_results(html, max_results=5):
    """Parse Trendyol search results."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    items = soup.select(".p-card-wrppr") or soup.select('[class*="prdct-cntnr"]')
    
    for item in items:
        if len(results) >= max_results:
            break
        try:
            brand_el = item.select_one(".prdct-desc-cntnr-ttl") or item.select_one('[class*="brand"]')
            title_el = item.select_one(".prdct-desc-cntnr-name") or item.select_one('[class*="prdct-desc"]')
            
            brand = brand_el.get_text(strip=True) if brand_el else ""
            title = title_el.get_text(strip=True) if title_el else ""
            name = f"{brand} {title}".strip()
            if len(name) < 3:
                continue
            
            price_el = item.select_one(".prc-box-dscntd") or item.select_one(".prc-box-sllng")
            if not price_el:
                continue
            price = parse_turkish_price(price_el.get_text())
            if not price:
                continue
            
            link = item.select_one("a")
            href = link.get("href", "") if link else ""
            url = f"https://www.trendyol.com{href}" if href.startswith("/") else href
            
            img = None
            img_el = item.select_one("img.p-card-img") or item.select_one("img")
            if img_el:
                img = img_el.get("src") or img_el.get("data-src")
            
            results.append(PriceResult(
                platform="trendyol",
                platform_name="Trendyol",
                product_name=name,
                price=price,
                currency="TRY",
                url=url,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
            ))
        except:
            continue
    
    return results


def parse_hepsiburada_results(html, max_results=5):
    """Parse Hepsiburada search results."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    items = soup.select('[data-test-id="product-card-item"]') or soup.select(".productListContent-item") or soup.select('[class*="product-card"]')
    
    for item in items:
        if len(results) >= max_results:
            break
        try:
            title_el = item.select_one('[data-test-id="product-card-name"]') or item.select_one("h3") or item.select_one('[class*="product-title"]')
            if not title_el:
                continue
            name = title_el.get_text(strip=True)
            
            price_el = item.select_one('[data-test-id="price-current-price"]') or item.select_one('[class*="product-price"]')
            if not price_el:
                continue
            price = parse_turkish_price(price_el.get_text())
            if not price:
                continue
            
            link = item.select_one("a")
            href = link.get("href", "") if link else ""
            url = f"https://www.hepsiburada.com{href}" if href and not href.startswith("http") else href
            
            img = None
            img_el = item.select_one("img")
            if img_el:
                img = img_el.get("src") or img_el.get("data-src")
            
            results.append(PriceResult(
                platform="hepsiburada",
                platform_name="Hepsiburada",
                product_name=name,
                price=price,
                currency="TRY",
                url=url,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
            ))
        except:
            continue
    
    return results


# ─── PRICE UTILS ───

def parse_price(text, currency="$"):
    """Parse US/EU price: $1,049.99 or €1.049,99"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text.strip())
    if not cleaned:
        return None
    try:
        if "." in cleaned and "," in cleaned:
            if cleaned.rindex(".") > cleaned.rindex(","):
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            parts = cleaned.split(",")
            if len(parts[-1]) == 2:
                cleaned = cleaned.replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        return float(cleaned)
    except:
        return None


def parse_turkish_price(text):
    """Parse Turkish price: 42.999,00 TL → 42999.00"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text.strip())
    if not cleaned:
        return None
    try:
        cleaned = cleaned.replace(".", "").replace(",", ".")
        return float(cleaned)
    except:
        return None


# ─── SEARCH FUNCTIONS ───

async def search_amazon_us(query, max_results=5):
    """Search Amazon US with stealth browser."""
    from playwright.async_api import async_playwright
    
    try:
        async with async_playwright() as p:
            browser, page = await get_stealth_page(p)
            url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
            html = await stealth_goto(page, url, '[data-component-type="s-search-result"]')
            await browser.close()
            return parse_amazon_results(html, max_results, "$", "amazon_us", "Amazon US")
    except Exception as e:
        print(f"[Amazon US] Error: {e}")
        return []


async def search_amazon_de(query, max_results=5):
    """Search Amazon DE."""
    from playwright.async_api import async_playwright
    
    try:
        async with async_playwright() as p:
            browser, page = await get_stealth_page(p)
            url = f"https://www.amazon.de/s?k={query.replace(' ', '+')}"
            html = await stealth_goto(page, url, '[data-component-type="s-search-result"]')
            await browser.close()
            return parse_amazon_results(html, max_results, "EUR", "amazon_de", "Amazon DE")
    except Exception as e:
        print(f"[Amazon DE] Error: {e}")
        return []


async def search_walmart(query, max_results=5):
    """Search Walmart with stealth browser."""
    from playwright.async_api import async_playwright
    
    try:
        async with async_playwright() as p:
            browser, page = await get_stealth_page(p)
            url = f"https://www.walmart.com/search?q={query.replace(' ', '+')}"
            html = await stealth_goto(page, url, '[data-item-id]')
            await browser.close()
            return parse_walmart_results(html, max_results)
    except Exception as e:
        print(f"[Walmart] Error: {e}")
        return []


async def search_bestbuy(query, max_results=5):
    """Search Best Buy with stealth browser."""
    from playwright.async_api import async_playwright
    
    try:
        async with async_playwright() as p:
            browser, page = await get_stealth_page(p)
            url = f"https://www.bestbuy.com/site/searchpage.jsp?st={query.replace(' ', '+')}"
            html = await stealth_goto(page, url, ".sku-item")
            await browser.close()
            return parse_bestbuy_results(html, max_results)
    except Exception as e:
        print(f"[Best Buy] Error: {e}")
        return []


async def search_trendyol(query, max_results=5):
    """Search Trendyol."""
    from playwright.async_api import async_playwright
    
    try:
        async with async_playwright() as p:
            browser, page = await get_stealth_page(p)
            url = f"https://www.trendyol.com/sr?q={query.replace(' ', '+')}"
            html = await stealth_goto(page, url, ".p-card-wrppr")
            await browser.close()
            return parse_trendyol_results(html, max_results)
    except Exception as e:
        print(f"[Trendyol] Error: {e}")
        return []


async def search_hepsiburada(query, max_results=5):
    """Search Hepsiburada."""
    from playwright.async_api import async_playwright
    
    try:
        async with async_playwright() as p:
            browser, page = await get_stealth_page(p)
            url = f"https://www.hepsiburada.com/ara?q={query.replace(' ', '+')}"
            html = await stealth_goto(page, url, '[data-test-id="product-card-item"]')
            await browser.close()
            return parse_hepsiburada_results(html, max_results)
    except Exception as e:
        print(f"[Hepsiburada] Error: {e}")
        return []


# ─── SCRAPER REGISTRY ───

SCRAPERS = {
    "us": {
        "amazon_us": search_amazon_us,
        "walmart": search_walmart,
        "bestbuy": search_bestbuy,
    },
    "tr": {
        "trendyol": search_trendyol,
        "hepsiburada": search_hepsiburada,
    },
    "eu": {
        "amazon_de": search_amazon_de,
    },
}

PLATFORM_NAMES = {
    "amazon_us": "Amazon US",
    "walmart": "Walmart",
    "bestbuy": "Best Buy",
    "trendyol": "Trendyol",
    "hepsiburada": "Hepsiburada",
    "amazon_de": "Amazon DE",
}


# ─── API ───

@app.get("/")
async def root():
    return {
        "name": "HYPE Intelligence API",
        "version": "2.0.0",
        "status": "running",
        "supported_regions": list(SCRAPERS.keys()),
        "scraping_method": "playwright_stealth",
    }


@app.get("/api/search", response_model=SearchResponse)
async def search_products(
    q: str = Query(..., min_length=1),
    region: str = Query("us"),
    platforms: Optional[str] = Query(None),
    max_results: int = Query(5, ge=1, le=10),
):
    if region not in SCRAPERS:
        raise HTTPException(400, f"Unsupported region: {region}")
    
    region_scrapers = SCRAPERS[region]
    
    if platforms:
        platform_ids = [p.strip() for p in platforms.split(",")]
        targets = {k: v for k, v in region_scrapers.items() if k in platform_ids}
    else:
        targets = region_scrapers
    
    if not targets:
        raise HTTPException(400, f"No valid platforms. Available: {list(region_scrapers.keys())}")
    
    start = time.time()
    
    # Run scrapers concurrently
    tasks = [func(q, max_results) for func in targets.values()]
    results_nested = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_results = []
    for r in results_nested:
        if isinstance(r, Exception):
            print(f"Scraper error: {r}")
            continue
        all_results.extend(r)
    
    elapsed = int((time.time() - start) * 1000)
    
    return SearchResponse(
        query=q,
        region=region,
        platforms_searched=list(targets.keys()),
        results=all_results,
        total_results=len(all_results),
        search_time_ms=elapsed,
    )


@app.get("/api/platforms")
async def list_platforms(region: Optional[str] = None):
    if region:
        if region not in SCRAPERS:
            raise HTTPException(400, f"Unsupported region: {region}")
        return {"region": region, "platforms": [{"id": k, "name": PLATFORM_NAMES.get(k, k)} for k in SCRAPERS[region]]}
    return {
        "regions": {
            reg: [{"id": k, "name": PLATFORM_NAMES.get(k, k)} for k in scrapers]
            for reg, scrapers in SCRAPERS.items()
        }
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat(), "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
