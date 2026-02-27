"""
HYPE Intelligence — Scraper Backend v4.0
=========================================
SerpAPI Google Shopping + Amazon direct + Akakce/Cimri Playwright.
Best of both worlds: reliable API + direct scraping.

uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from urllib.parse import urlparse, parse_qs
import asyncio
import json
import re
import time
import random
import traceback
import logging
import os
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("hype")

app = FastAPI(title="HYPE Intelligence API", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SerpAPI Key — env variable override available
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "008f10fdf7243f76a522c290d21a1a13f19f16bb98c0a43bc22f836e9819ce15")


# ═══════════════════════════════════════════
#  MODELS
# ═══════════════════════════════════════════

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
    source: str = "direct"

class SearchResponse(BaseModel):
    query: str
    region: str
    platforms_searched: list[str]
    results: list[PriceResult]
    total_results: int
    search_time_ms: int
    sources_used: list[str]


# ═══════════════════════════════════════════
#  URL UTILS
# ═══════════════════════════════════════════

def extract_real_url(raw_url: str) -> str:
    """
    Google Shopping URL'lerinden gerçek satıcı URL'sini çıkarır.
    Örnek: google.com/aclk?...&adurl=https://www.nike.com/... → https://www.nike.com/...
    """
    if not raw_url:
        return ""
    if "google.com" not in raw_url:
        return raw_url
    try:
        parsed = urlparse(raw_url)
        params = parse_qs(parsed.query)
        # Google farklı parametrelerde gerçek URL'yi saklıyor
        for key in ["url", "adurl", "q", "dest"]:
            if key in params and params[key]:
                candidate = params[key][0]
                if candidate.startswith("http"):
                    return candidate
    except Exception:
        pass
    return raw_url


# ═══════════════════════════════════════════
#  SERPAPI — GOOGLE SHOPPING (US / EU / TR)
# ═══════════════════════════════════════════

async def search_serpapi_shopping(query: str, region: str = "us", max_results: int = 15):
    """
    SerpAPI Google Shopping — returns structured JSON.
    One request = prices from Amazon, Walmart, Best Buy, Target, eBay, etc.
    Works for US, EU, and TR.
    """
    import httpx

    config = {
        "us": {"gl": "us", "hl": "en", "currency": "$", "location": "United States"},
        "eu": {"gl": "de", "hl": "de", "currency": "€", "location": "Germany"},
        "tr": {"gl": "tr", "hl": "tr", "currency": "₺", "location": "Turkey"},
    }
    cfg = config.get(region, config["us"])

    params = {
        "engine": "google_shopping",
        "q": query,
        "gl": cfg["gl"],
        "hl": cfg["hl"],
        "location": cfg["location"],
        "api_key": SERPAPI_KEY,
        "num": max_results,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info(f"[SerpAPI] Searching: {query} | region={region}")
            response = await client.get("https://serpapi.com/search.json", params=params)

            if response.status_code != 200:
                logger.error(f"[SerpAPI] HTTP {response.status_code}: {response.text[:200]}")
                return []

            data = response.json()

            # Check for errors
            if "error" in data:
                logger.error(f"[SerpAPI] Error: {data['error']}")
                return []

            results = []

            # Parse shopping_results
            shopping = data.get("shopping_results", [])
            logger.info(f"[SerpAPI] Got {len(shopping)} shopping results")

            for item in shopping[:max_results]:
                try:
                    name = item.get("title", "")
                    if not name:
                        continue

                    # Price extraction
                    price = None
                    price_raw = item.get("extracted_price")
                    if price_raw is not None:
                        price = float(price_raw)
                    else:
                        price_str = item.get("price", "")
                        price = _extract_any_price(price_str)

                    if not price or price <= 0:
                        continue

                    # Seller / Source
                    seller = item.get("source", "") or item.get("seller", "")
                    platform_id, platform_name = _identify_platform(seller, region)

                    # URL — gerçek satıcı URL'sini çıkar
                    raw_url = item.get("link") or item.get("product_link") or ""
                    url = extract_real_url(raw_url)

                    # Rating
                    rating = None
                    if item.get("rating"):
                        try:
                            rating = float(item["rating"])
                        except:
                            pass

                    # Reviews
                    review_count = None
                    if item.get("reviews"):
                        try:
                            review_count = int(item["reviews"])
                        except:
                            pass

                    # Image
                    image = item.get("thumbnail") or item.get("image") or None

                    results.append(PriceResult(
                        platform=platform_id,
                        platform_name=platform_name,
                        product_name=name,
                        price=price,
                        currency=cfg["currency"],
                        url=url,
                        seller=seller,
                        rating=rating,
                        review_count=review_count,
                        image_url=image,
                        scraped_at=datetime.utcnow().isoformat(),
                        source="serpapi_google_shopping",
                    ))
                except Exception as e:
                    logger.warning(f"[SerpAPI] Parse error: {e}")
                    continue

            logger.info(f"[SerpAPI] Parsed {len(results)} results")
            return results

    except Exception as e:
        logger.error(f"[SerpAPI] Error: {e}\n{traceback.format_exc()}")
        return []


# ═══════════════════════════════════════════
#  AMAZON DIRECT (proven working)
# ═══════════════════════════════════════════

async def search_amazon_us(query, max_results=5):
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser, page = await create_stealth_browser(p)
            url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
            logger.info(f"[Amazon US] Navigating...")

            html = await stealth_navigate(page, url, '[data-component-type="s-search-result"]')
            await browser.close()

            results = _parse_amazon_html(html, max_results, "$", "amazon_us", "Amazon US")
            logger.info(f"[Amazon US] Got {len(results)} results")
            return results
    except Exception as e:
        logger.error(f"[Amazon US] Error: {e}\n{traceback.format_exc()}")
        return []


# ═══════════════════════════════════════════
#  STEALTH BROWSER (for Amazon + future TR scrapers)
# ═══════════════════════════════════════════

async def create_stealth_browser(playwright, locale="en-US", timezone="America/New_York"):
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
        ]
    )
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        locale=locale,
        timezone_id=timezone,
        java_script_enabled=True,
        color_scheme="light",
    )
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ]
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
    """)
    page = await context.new_page()
    return browser, page


async def stealth_navigate(page, url, wait_for=None, timeout=25000):
    await asyncio.sleep(random.uniform(0.5, 1.5))
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    await asyncio.sleep(random.uniform(1.5, 3.0))
    if wait_for:
        try:
            await page.wait_for_selector(wait_for, timeout=8000)
        except:
            await asyncio.sleep(2)
    await page.evaluate("window.scrollBy(0, Math.floor(Math.random() * 400) + 200)")
    await asyncio.sleep(random.uniform(0.5, 1.0))
    return await page.content()


# ═══════════════════════════════════════════
#  AMAZON HTML PARSER
# ═══════════════════════════════════════════

def _parse_amazon_html(html, max_results, currency, platform_id, platform_name):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    results = []
    items = soup.select('[data-component-type="s-search-result"]')

    for item in items:
        if len(results) >= max_results:
            break
        try:
            if item.select_one('[data-component-type="sp-sponsored-result"]'):
                continue
            if item.find(string=re.compile(r"Sponsored", re.I)):
                continue

            title_el = item.select_one("h2 a span") or item.select_one("h2 span")
            if not title_el:
                continue
            name = title_el.get_text(strip=True)
            if len(name) < 5:
                continue

            price = None
            offscreen = item.select_one(".a-price:not(.a-text-price) .a-offscreen")
            if offscreen:
                price = _extract_any_price(offscreen.get_text())
            if not price:
                whole = item.select_one(".a-price:not(.a-text-price) .a-price-whole")
                frac = item.select_one(".a-price:not(.a-text-price) .a-price-fraction")
                if whole:
                    w = whole.get_text(strip=True).replace(",", "").replace(".", "")
                    f = frac.get_text(strip=True) if frac else "00"
                    try:
                        price = float(f"{w}.{f}")
                    except:
                        pass
            if not price:
                continue

            link_el = item.select_one("h2 a")
            url = ""
            if link_el and link_el.get("href"):
                href = link_el["href"]
                url = f"https://www.amazon.com{href}" if href.startswith("/") else href

            rating = None
            rating_el = item.select_one("[aria-label*='out of 5']")
            if rating_el:
                m = re.search(r"(\d+\.?\d*)\s+out", rating_el.get("aria-label", ""))
                if m:
                    rating = float(m.group(1))

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
                source="direct",
            ))
        except:
            continue
    return results


# ═══════════════════════════════════════════
#  UTILS
# ═══════════════════════════════════════════

def _extract_any_price(text):
    """Extract price from any format."""
    if not text:
        return None
    cleaned = re.sub(r'[^\d.,]', '', str(text).strip())
    if not cleaned:
        return None
    try:
        if '.' in cleaned and ',' in cleaned:
            if cleaned.rindex('.') > cleaned.rindex(','):
                cleaned = cleaned.replace(',', '')
            else:
                cleaned = cleaned.replace('.', '').replace(',', '.')
        elif ',' in cleaned:
            parts = cleaned.split(',')
            if len(parts[-1]) == 2:
                cleaned = cleaned.replace(',', '.')
            else:
                cleaned = cleaned.replace(',', '')
        return float(cleaned)
    except:
        return None


def _identify_platform(seller, region):
    if not seller:
        return "unknown", "Unknown"
    s = seller.lower()
    mappings = {
        "amazon": ("amazon_us" if region == "us" else "amazon_de" if region == "eu" else "amazon_tr", "Amazon"),
        "walmart": ("walmart", "Walmart"),
        "best buy": ("bestbuy", "Best Buy"),
        "bestbuy": ("bestbuy", "Best Buy"),
        "target": ("target", "Target"),
        "ebay": ("ebay", "eBay"),
        "newegg": ("newegg", "Newegg"),
        "b&h": ("bh", "B&H Photo"),
        "apple": ("apple", "Apple"),
        "trendyol": ("trendyol", "Trendyol"),
        "hepsiburada": ("hepsiburada", "Hepsiburada"),
        "n11": ("n11", "n11"),
        "mediamarkt": ("mediamarkt", "MediaMarkt"),
        "saturn": ("saturn", "Saturn"),
        "coolblue": ("coolblue", "Coolblue"),
        "fnac": ("fnac", "Fnac"),
        "otto": ("otto", "Otto"),
    }
    for key, (pid, pname) in mappings.items():
        if key in s:
            return pid, pname
    return "other", seller


# ═══════════════════════════════════════════
#  SEARCH ORCHESTRATOR
# ═══════════════════════════════════════════

async def search_region(query, region, max_results=15):
    tasks = []
    sources = []

    if region == "us":
        # SerpAPI Google Shopping + Amazon direct
        tasks.append(search_serpapi_shopping(query, "us", max_results))
        tasks.append(search_amazon_us(query, min(max_results, 5)))
        sources = ["serpapi_google_shopping", "amazon_us_direct"]

    elif region == "tr":
        # SerpAPI Google Shopping Turkey
        tasks.append(search_serpapi_shopping(query, "tr", max_results))
        sources = ["serpapi_google_shopping_tr"]

    elif region == "eu":
        # SerpAPI Google Shopping Germany
        tasks.append(search_serpapi_shopping(query, "eu", max_results))
        sources = ["serpapi_google_shopping_eu"]

    results_nested = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for r in results_nested:
        if isinstance(r, Exception):
            logger.error(f"Search error: {r}")
            continue
        all_results.extend(r)

    # Deduplicate by normalized name
    seen = set()
    unique = []
    for r in all_results:
        key = re.sub(r'[^a-z0-9]', '', r.product_name.lower())[:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    # Sort by price
    unique.sort(key=lambda x: x.price)
    return unique[:max_results], sources


# ═══════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "name": "HYPE Intelligence API",
        "version": "4.0.0",
        "status": "running",
        "supported_regions": ["us", "tr", "eu"],
        "strategy": "serpapi_google_shopping + amazon_direct",
        "serpapi_key_set": bool(SERPAPI_KEY),
    }


@app.get("/api/search", response_model=SearchResponse)
async def search_products(
    q: str = Query(..., min_length=1),
    region: str = Query("us"),
    max_results: int = Query(15, ge=1, le=30),
):
    if region not in ["us", "tr", "eu"]:
        raise HTTPException(400, "Unsupported region. Use: us, tr, eu")

    start = time.time()
    results, sources = await search_region(q, region, max_results)
    elapsed = int((time.time() - start) * 1000)
    platforms_found = list(set(r.platform for r in results))

    return SearchResponse(
        query=q,
        region=region,
        platforms_searched=platforms_found,
        results=results,
        total_results=len(results),
        search_time_ms=elapsed,
        sources_used=sources,
    )


@app.get("/api/debug")
async def debug_search(
    q: str = Query("iPhone 16 Pro"),
    region: str = Query("us"),
):
    debug_info = {"query": q, "region": region, "results": {}, "errors": {}}

    # Test SerpAPI
    try:
        serp = await search_serpapi_shopping(q, region, 5)
        debug_info["results"]["serpapi"] = {
            "count": len(serp),
            "items": [{"name": r.product_name, "price": r.price, "platform": r.platform, "seller": r.seller, "url": r.url} for r in serp]
        }
    except Exception as e:
        debug_info["errors"]["serpapi"] = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"

    # Test Amazon (US only)
    if region == "us":
        try:
            az = await search_amazon_us(q, 3)
            debug_info["results"]["amazon_us"] = {
                "count": len(az),
                "items": [{"name": r.product_name, "price": r.price, "url": r.url} for r in az]
            }
        except Exception as e:
            debug_info["errors"]["amazon_us"] = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"

    return debug_info


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat(), "version": "4.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
