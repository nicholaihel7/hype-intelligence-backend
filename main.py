"""
HYPE Intelligence — Scraper Backend v3.0
=========================================
Smart scraping: JSON-LD first, HTML fallback.
Google Shopping (US/EU) + Akakce/Cimri (TR) + Amazon direct.

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
import traceback
import logging
from datetime import datetime

# Setup logging so we can see errors in Railway
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("hype")

app = FastAPI(title="HYPE Intelligence API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
#  STEALTH BROWSER ENGINE
# ═══════════════════════════════════════════

async def create_stealth_browser(playwright, locale="en-US", timezone="America/New_York"):
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
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
        has_touch=False,
        is_mobile=False,
        color_scheme="light",
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9,en;q=0.8",
            "DNT": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
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
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'tr'] });
        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) => (
            params.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(params)
        );
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';
            if (parameter === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter.call(this, parameter);
        };
    """)

    page = await context.new_page()
    return browser, page


async def stealth_navigate(page, url, wait_for=None, timeout=25000):
    await asyncio.sleep(random.uniform(0.5, 1.5))
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    status = response.status if response else "no response"
    logger.info(f"Navigation to {url[:80]}... status={status}")
    await asyncio.sleep(random.uniform(1.5, 3.0))

    if wait_for:
        try:
            await page.wait_for_selector(wait_for, timeout=8000)
            logger.info(f"Found selector: {wait_for}")
        except:
            logger.warning(f"Selector not found: {wait_for}, continuing...")
            await asyncio.sleep(2)

    await page.evaluate("""
        async () => {
            await new Promise(r => setTimeout(r, 300));
            window.scrollBy(0, Math.floor(Math.random() * 400) + 200);
            await new Promise(r => setTimeout(r, 500));
            window.scrollBy(0, Math.floor(Math.random() * 300) + 100);
        }
    """)
    await asyncio.sleep(random.uniform(0.5, 1.0))

    html = await page.content()
    logger.info(f"Page HTML length: {len(html)} chars")
    return html


# ═══════════════════════════════════════════
#  SMART PARSER — JSON-LD FIRST
# ═══════════════════════════════════════════

def extract_json_ld(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.select('script[type="application/ld+json"]')
    all_data = []
    for script in scripts:
        try:
            text = script.string
            if not text:
                continue
            data = json.loads(text)
            if isinstance(data, list):
                all_data.extend(data)
            else:
                all_data.append(data)
        except:
            continue
    logger.info(f"Found {len(all_data)} JSON-LD blocks")
    return all_data


def extract_products_from_jsonld(json_ld_items, platform, platform_name, currency, source):
    results = []
    for item in json_ld_items:
        try:
            item_type = item.get("@type", "")
            if item_type == "ItemList":
                for elem in item.get("itemListElement", []):
                    product = elem if elem.get("@type") == "Product" else elem.get("item", {})
                    result = _parse_jsonld_product(product, platform, platform_name, currency, source)
                    if result:
                        results.append(result)
            elif item_type == "Product":
                result = _parse_jsonld_product(item, platform, platform_name, currency, source)
                if result:
                    results.append(result)
            elif isinstance(item, list):
                for sub in item:
                    if isinstance(sub, dict) and sub.get("@type") == "Product":
                        result = _parse_jsonld_product(sub, platform, platform_name, currency, source)
                        if result:
                            results.append(result)
        except:
            continue
    logger.info(f"Extracted {len(results)} products from JSON-LD")
    return results


def _parse_jsonld_product(product, platform, platform_name, currency, source):
    if not product or not isinstance(product, dict):
        return None
    name = product.get("name", "")
    if not name or len(name) < 3:
        return None

    price = None
    seller = None
    url = product.get("url", "")
    in_stock = True

    offers = product.get("offers", {})
    if isinstance(offers, list):
        best = None
        for offer in offers:
            p = _get_offer_price(offer)
            if p and (best is None or p < best):
                best = p
                seller = offer.get("seller", {}).get("name") if isinstance(offer.get("seller"), dict) else offer.get("seller")
                url = offer.get("url", url)
                avail = offer.get("availability", "")
                in_stock = "OutOfStock" not in avail if avail else True
        price = best
    elif isinstance(offers, dict):
        if offers.get("@type") == "AggregateOffer":
            price = _safe_float(offers.get("lowPrice"))
            if not price:
                price = _safe_float(offers.get("price"))
        else:
            price = _get_offer_price(offers)
            seller = offers.get("seller", {}).get("name") if isinstance(offers.get("seller"), dict) else offers.get("seller")
            url = offers.get("url", url)
            avail = offers.get("availability", "")
            in_stock = "OutOfStock" not in avail if avail else True

    if not price or price <= 0:
        return None

    rating = None
    agg_rating = product.get("aggregateRating", {})
    if agg_rating:
        rating = _safe_float(agg_rating.get("ratingValue"))

    review_count = None
    if agg_rating:
        review_count = _safe_int(agg_rating.get("reviewCount") or agg_rating.get("ratingCount"))

    image = product.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")

    return PriceResult(
        platform=platform,
        platform_name=platform_name,
        product_name=name,
        price=price,
        currency=currency,
        url=url,
        seller=seller,
        rating=rating,
        review_count=review_count,
        image_url=image if image else None,
        in_stock=in_stock,
        scraped_at=datetime.utcnow().isoformat(),
        source=source,
    )


def _get_offer_price(offer):
    if not isinstance(offer, dict):
        return None
    return _safe_float(offer.get("price")) or _safe_float(offer.get("lowPrice"))


def _safe_float(val):
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.replace(",", "").replace(" ", "")
        return float(val)
    except:
        return None


def _safe_int(val):
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "")))
    except:
        return None


# ═══════════════════════════════════════════
#  PRICE UTILS
# ═══════════════════════════════════════════

def _extract_price_from_text(text, currency="$"):
    if not text:
        return None
    patterns = [
        r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*\$',
        r'€\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)',
        r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*€',
        r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL',
        r'₺\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            price_str = m.group(1)
            if currency in ["€", "₺", "TL", "TRY"]:
                price_str = price_str.replace(".", "").replace(",", ".")
            else:
                price_str = price_str.replace(",", "")
            try:
                return float(price_str)
            except:
                continue
    return None


def _parse_turkish_price(text):
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


def _identify_platform(seller, region):
    if not seller:
        return "unknown", "Unknown"
    s = seller.lower()
    mappings = {
        "amazon": ("amazon_us", "Amazon"),
        "walmart": ("walmart", "Walmart"),
        "best buy": ("bestbuy", "Best Buy"),
        "bestbuy": ("bestbuy", "Best Buy"),
        "target": ("target", "Target"),
        "ebay": ("ebay", "eBay"),
        "newegg": ("newegg", "Newegg"),
        "b&h": ("bh", "B&H Photo"),
        "trendyol": ("trendyol", "Trendyol"),
        "hepsiburada": ("hepsiburada", "Hepsiburada"),
        "n11": ("n11", "n11"),
        "mediamarkt": ("mediamarkt", "MediaMarkt"),
        "saturn": ("saturn", "Saturn"),
    }
    for key, (pid, pname) in mappings.items():
        if key in s:
            return pid, pname
    return "other", seller


# ═══════════════════════════════════════════
#  GOOGLE SHOPPING SCRAPER
# ═══════════════════════════════════════════

async def search_google_shopping(query, region="us", max_results=10):
    from playwright.async_api import async_playwright
    from bs4 import BeautifulSoup

    config = {
        "us": {"domain": "www.google.com", "gl": "us", "hl": "en", "currency": "$", "locale": "en-US", "tz": "America/New_York"},
        "eu": {"domain": "www.google.de", "gl": "de", "hl": "de", "currency": "€", "locale": "de-DE", "tz": "Europe/Berlin"},
        "tr": {"domain": "www.google.com.tr", "gl": "tr", "hl": "tr", "currency": "₺", "locale": "tr-TR", "tz": "Europe/Istanbul"},
    }
    cfg = config.get(region, config["us"])

    try:
        async with async_playwright() as p:
            browser, page = await create_stealth_browser(p, locale=cfg["locale"], timezone=cfg["tz"])
            url = f"https://{cfg['domain']}/search?q={query.replace(' ', '+')}&tbm=shop&gl={cfg['gl']}&hl={cfg['hl']}"
            logger.info(f"[Google Shopping] Navigating to: {url[:100]}")

            html = await stealth_navigate(page, url, wait_for=".sh-dgr__grid-result")

            # Save a snippet for debug
            snippet = html[:500] if html else "EMPTY"
            logger.info(f"[Google Shopping] HTML snippet: {snippet[:200]}")

            await browser.close()

            # Strategy 1: JSON-LD
            json_ld = extract_json_ld(html)
            results = extract_products_from_jsonld(json_ld, "google_shopping", "Google Shopping", cfg["currency"], "google_shopping")
            logger.info(f"[Google Shopping] JSON-LD results: {len(results)}")

            # Strategy 2: HTML parsing
            if len(results) < max_results:
                html_results = _parse_google_shopping_html(html, cfg["currency"], max_results - len(results))
                logger.info(f"[Google Shopping] HTML parse results: {len(html_results)}")
                results.extend(html_results)

            # Strategy 3: Script data extraction
            if len(results) < 3:
                script_results = _parse_google_shopping_scripts(html, cfg["currency"], max_results)
                logger.info(f"[Google Shopping] Script parse results: {len(script_results)}")
                existing_names = {r.product_name.lower() for r in results}
                for sr in script_results:
                    if sr.product_name.lower() not in existing_names:
                        results.append(sr)
                        existing_names.add(sr.product_name.lower())

            logger.info(f"[Google Shopping] Total results: {len(results)}")
            return results[:max_results]

    except Exception as e:
        logger.error(f"[Google Shopping] Error: {e}\n{traceback.format_exc()}")
        return []


def _parse_google_shopping_html(html, currency, max_results):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    results = []

    items = soup.select(".sh-dgr__grid-result") or soup.select(".sh-dlr__list-result") or soup.select("[data-docid]")
    logger.info(f"[GS HTML] Found {len(items)} product items")

    for item in items:
        if len(results) >= max_results:
            break
        try:
            title_el = item.select_one("h3") or item.select_one(".tAxDx") or item.select_one("[role='heading']")
            if not title_el:
                continue
            name = title_el.get_text(strip=True)
            if len(name) < 3:
                continue

            price = None
            price_el = item.select_one("[aria-label*='$']") or item.select_one("[aria-label*='€']") or item.select_one("[aria-label*='TL']")
            if price_el:
                price = _extract_price_from_text(price_el.get("aria-label", ""), currency)
            if not price:
                for el in item.select("b, span.a8Pemb, span.HRLxBb"):
                    text = el.get_text(strip=True)
                    p = _extract_price_from_text(text, currency)
                    if p:
                        price = p
                        break
            if not price:
                all_text = item.get_text()
                price = _extract_price_from_text(all_text, currency)
            if not price:
                continue

            seller = None
            seller_el = item.select_one(".aULzUe") or item.select_one(".IuHnof")
            if seller_el:
                seller = seller_el.get_text(strip=True)

            link = item.select_one("a[href*='shopping']") or item.select_one("a[href*='url']") or item.select_one("a")
            url = ""
            if link:
                href = link.get("href", "")
                if href.startswith("/"):
                    url = f"https://www.google.com{href}"
                else:
                    url = href

            img = None
            img_el = item.select_one("img:not([width='1'])")
            if img_el:
                img = img_el.get("src") or img_el.get("data-src")

            platform_id, platform_name = _identify_platform(seller, "us")

            results.append(PriceResult(
                platform=platform_id,
                platform_name=platform_name,
                product_name=name,
                price=price,
                currency=currency,
                url=url,
                seller=seller,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
                source="google_shopping",
            ))
        except:
            continue
    return results


def _parse_google_shopping_scripts(html, currency, max_results):
    results = []
    price_pattern = r'"price":\s*"?(\d+\.?\d*)"?'
    title_pattern = r'"title":\s*"([^"]+)"'
    script_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)

    for block in script_blocks:
        if '"price"' not in block or '"title"' not in block:
            continue
        titles = re.findall(title_pattern, block)
        prices = re.findall(price_pattern, block)
        for i in range(min(len(titles), len(prices), max_results)):
            try:
                price = float(prices[i])
                if price <= 0 or price > 100000:
                    continue
                results.append(PriceResult(
                    platform="google_shopping",
                    platform_name="Google Shopping",
                    product_name=titles[i],
                    price=price,
                    currency=currency,
                    url="",
                    scraped_at=datetime.utcnow().isoformat(),
                    source="google_shopping",
                ))
            except:
                continue
    return results


# ═══════════════════════════════════════════
#  AKAKCE SCRAPER
# ═══════════════════════════════════════════

async def search_akakce(query, max_results=10):
    from playwright.async_api import async_playwright
    from bs4 import BeautifulSoup

    try:
        async with async_playwright() as p:
            browser, page = await create_stealth_browser(p, locale="tr-TR", timezone="Europe/Istanbul")
            url = f"https://www.akakce.com/arama/?q={query.replace(' ', '+')}"
            logger.info(f"[Akakce] Navigating to: {url}")

            html = await stealth_navigate(page, url, wait_for=".p")
            logger.info(f"[Akakce] HTML length: {len(html)}")

            # Check for CAPTCHA or block page
            if "captcha" in html.lower() or "robot" in html.lower():
                logger.warning("[Akakce] Possible CAPTCHA detected")

            await browser.close()

            # Strategy 1: JSON-LD
            json_ld = extract_json_ld(html)
            results = extract_products_from_jsonld(json_ld, "akakce", "Akakçe", "TRY", "akakce")
            logger.info(f"[Akakce] JSON-LD results: {len(results)}")

            # Strategy 2: HTML parse
            if len(results) < max_results:
                html_results = _parse_akakce_html(html, max_results - len(results))
                logger.info(f"[Akakce] HTML parse results: {len(html_results)}")
                results.extend(html_results)

            return results[:max_results]

    except Exception as e:
        logger.error(f"[Akakce] Error: {e}\n{traceback.format_exc()}")
        return []


def _parse_akakce_html(html, max_results):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Try multiple selectors — Akakce changes these
    items = (
        soup.select("li.p") or
        soup.select("[class*='prd']") or
        soup.select(".search-result-item") or
        soup.select("div.p") or
        soup.select("[data-pr]")
    )
    logger.info(f"[Akakce HTML] Found {len(items)} items")

    for item in items:
        if len(results) >= max_results:
            break
        try:
            # Title — try many selectors
            title_el = (
                item.select_one("h3 a") or
                item.select_one(".pn a") or
                item.select_one("a.pn_v8") or
                item.select_one("a[title]") or
                item.select_one("a span")
            )
            if not title_el:
                continue
            name = title_el.get("title") or title_el.get_text(strip=True)
            if len(name) < 3:
                continue

            # Price — try many patterns
            price = None
            price_el = (
                item.select_one(".pb_v8") or
                item.select_one(".pt_v8") or
                item.select_one("[class*='price']") or
                item.select_one("[class*='fyt']")
            )
            if price_el:
                price = _parse_turkish_price(price_el.get_text())
            if not price:
                all_text = item.get_text()
                price = _extract_price_from_text(all_text, "TRY")
            if not price:
                continue

            # URL
            href = title_el.get("href", "")
            url = f"https://www.akakce.com{href}" if href.startswith("/") else href

            # Image
            img = None
            img_el = item.select_one("img")
            if img_el:
                img = img_el.get("src") or img_el.get("data-src")
                if img and img.startswith("//"):
                    img = "https:" + img

            results.append(PriceResult(
                platform="akakce",
                platform_name="Akakçe",
                product_name=name,
                price=price,
                currency="TRY",
                url=url,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
                source="akakce",
            ))
        except:
            continue
    return results


# ═══════════════════════════════════════════
#  CIMRI SCRAPER
# ═══════════════════════════════════════════

async def search_cimri(query, max_results=10):
    from playwright.async_api import async_playwright
    from bs4 import BeautifulSoup

    try:
        async with async_playwright() as p:
            browser, page = await create_stealth_browser(p, locale="tr-TR", timezone="Europe/Istanbul")
            url = f"https://www.cimri.com/arama?q={query.replace(' ', '+')}"
            logger.info(f"[Cimri] Navigating to: {url}")

            html = await stealth_navigate(page, url, wait_for="[class*='product']")
            logger.info(f"[Cimri] HTML length: {len(html)}")

            await browser.close()

            # Strategy 1: JSON-LD
            json_ld = extract_json_ld(html)
            results = extract_products_from_jsonld(json_ld, "cimri", "Cimri", "TRY", "cimri")
            logger.info(f"[Cimri] JSON-LD results: {len(results)}")

            # Strategy 2: HTML parse
            if len(results) < max_results:
                html_results = _parse_cimri_html(html, max_results - len(results))
                logger.info(f"[Cimri] HTML parse results: {len(html_results)}")
                results.extend(html_results)

            return results[:max_results]

    except Exception as e:
        logger.error(f"[Cimri] Error: {e}\n{traceback.format_exc()}")
        return []


def _parse_cimri_html(html, max_results):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    results = []

    items = (
        soup.select("[class*='ProductCard']") or
        soup.select("[class*='product-card']") or
        soup.select(".s-product-card") or
        soup.select("[data-productid]")
    )
    logger.info(f"[Cimri HTML] Found {len(items)} items")

    for item in items:
        if len(results) >= max_results:
            break
        try:
            title_el = item.select_one("h3") or item.select_one("[class*='ProductName']") or item.select_one("[class*='product-name']") or item.select_one("a[title]")
            if not title_el:
                continue
            name = title_el.get("title") or title_el.get_text(strip=True)
            if len(name) < 3:
                continue

            price = None
            price_el = item.select_one("[class*='Price']") or item.select_one("[class*='price']")
            if price_el:
                price = _parse_turkish_price(price_el.get_text())
            if not price:
                all_text = item.get_text()
                price = _extract_price_from_text(all_text, "TRY")
            if not price:
                continue

            link = item.select_one("a")
            href = link.get("href", "") if link else ""
            url = f"https://www.cimri.com{href}" if href.startswith("/") else href

            img = None
            img_el = item.select_one("img")
            if img_el:
                img = img_el.get("src") or img_el.get("data-src")

            results.append(PriceResult(
                platform="cimri",
                platform_name="Cimri",
                product_name=name,
                price=price,
                currency="TRY",
                url=url,
                image_url=img,
                scraped_at=datetime.utcnow().isoformat(),
                source="cimri",
            ))
        except:
            continue
    return results


# ═══════════════════════════════════════════
#  AMAZON DIRECT (proven working)
# ═══════════════════════════════════════════

async def search_amazon_us(query, max_results=5):
    from playwright.async_api import async_playwright
    from bs4 import BeautifulSoup

    try:
        async with async_playwright() as p:
            browser, page = await create_stealth_browser(p)
            url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
            logger.info(f"[Amazon US] Navigating to: {url[:80]}")

            html = await stealth_navigate(page, url, '[data-component-type="s-search-result"]')
            logger.info(f"[Amazon US] HTML length: {len(html)}")

            await browser.close()

            # JSON-LD first
            json_ld = extract_json_ld(html)
            results = extract_products_from_jsonld(json_ld, "amazon_us", "Amazon US", "$", "direct")
            logger.info(f"[Amazon US] JSON-LD results: {len(results)}")

            # HTML fallback
            if len(results) < max_results:
                html_results = _parse_amazon_html(html, max_results - len(results), "$", "amazon_us", "Amazon US")
                logger.info(f"[Amazon US] HTML parse results: {len(html_results)}")
                results.extend(html_results)

            return results[:max_results]
    except Exception as e:
        logger.error(f"[Amazon US] Error: {e}\n{traceback.format_exc()}")
        return []


def _parse_amazon_html(html, max_results, currency, platform_id, platform_name):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    results = []
    items = soup.select('[data-component-type="s-search-result"]')
    logger.info(f"[Amazon HTML] Found {len(items)} items")

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
                price = _extract_price_from_text(offscreen.get_text(), currency)
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
                base = "https://www.amazon.com" if platform_id == "amazon_us" else "https://www.amazon.de"
                url = f"{base}{href}" if href.startswith("/") else href

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
#  SEARCH ORCHESTRATOR
# ═══════════════════════════════════════════

async def search_region(query, region, max_results=10):
    tasks = []
    sources = []

    if region == "us":
        tasks.append(search_google_shopping(query, "us", max_results))
        tasks.append(search_amazon_us(query, max_results))
        sources = ["google_shopping", "amazon_us"]
    elif region == "tr":
        tasks.append(search_akakce(query, max_results))
        tasks.append(search_cimri(query, max_results))
        sources = ["akakce", "cimri"]
    elif region == "eu":
        tasks.append(search_google_shopping(query, "eu", max_results))
        sources = ["google_shopping_eu"]

    results_nested = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for r in results_nested:
        if isinstance(r, Exception):
            logger.error(f"Search error: {r}")
            continue
        all_results.extend(r)

    # Deduplicate
    seen = set()
    unique = []
    for r in all_results:
        key = re.sub(r'[^a-z0-9]', '', r.product_name.lower())[:50]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda x: x.price)
    return unique[:max_results], sources


# ═══════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "name": "HYPE Intelligence API",
        "version": "3.0.0",
        "status": "running",
        "supported_regions": ["us", "tr", "eu"],
        "strategy": "aggregator_first_direct_fallback",
        "sources": {
            "us": ["Google Shopping", "Amazon US (direct)"],
            "tr": ["Akakçe", "Cimri"],
            "eu": ["Google Shopping DE"],
        }
    }


@app.get("/api/search", response_model=SearchResponse)
async def search_products(
    q: str = Query(..., min_length=1),
    region: str = Query("us"),
    max_results: int = Query(10, ge=1, le=20),
):
    if region not in ["us", "tr", "eu"]:
        raise HTTPException(400, f"Unsupported region. Use: us, tr, eu")

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
    """Debug endpoint — shows full error details."""
    debug_info = {"query": q, "region": region, "results": {}, "errors": {}}

    if region == "us":
        try:
            gs = await search_google_shopping(q, "us", 3)
            debug_info["results"]["google_shopping"] = {
                "count": len(gs),
                "items": [{"name": r.product_name, "price": r.price, "platform": r.platform} for r in gs]
            }
        except Exception as e:
            debug_info["errors"]["google_shopping"] = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"

        try:
            az = await search_amazon_us(q, 3)
            debug_info["results"]["amazon_us"] = {
                "count": len(az),
                "items": [{"name": r.product_name, "price": r.price} for r in az]
            }
        except Exception as e:
            debug_info["errors"]["amazon_us"] = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"

    elif region == "tr":
        try:
            ak = await search_akakce(q, 3)
            debug_info["results"]["akakce"] = {
                "count": len(ak),
                "items": [{"name": r.product_name, "price": r.price} for r in ak]
            }
        except Exception as e:
            debug_info["errors"]["akakce"] = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"

        try:
            ci = await search_cimri(q, 3)
            debug_info["results"]["cimri"] = {
                "count": len(ci),
                "items": [{"name": r.product_name, "price": r.price} for r in ci]
            }
        except Exception as e:
            debug_info["errors"]["cimri"] = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"

    return debug_info


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat(), "version": "3.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
