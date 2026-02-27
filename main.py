"""
HYPE Intelligence — Scraper Backend v5.0
=========================================
Multi-engine SerpAPI: Amazon + Walmart + eBay + Google Shopping
All results return REAL product URLs — no Google redirects.

uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from urllib.parse import urlparse, parse_qs
import asyncio
import re
import time
import traceback
import logging
import os
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("hype")

app = FastAPI(title="HYPE Intelligence API", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    source: str = "serpapi"

class SearchResponse(BaseModel):
    query: str
    region: str
    platforms_searched: list[str]
    results: list[PriceResult]
    total_results: int
    search_time_ms: int
    sources_used: list[str]


# ═══════════════════════════════════════════
#  SERPAPI HELPER
# ═══════════════════════════════════════════

async def serpapi_request(params: dict) -> dict:
    """Single SerpAPI request with error handling."""
    import httpx
    params["api_key"] = SERPAPI_KEY
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get("https://serpapi.com/search.json", params=params)
            if response.status_code != 200:
                logger.error(f"[SerpAPI] HTTP {response.status_code}: {response.text[:200]}")
                return {}
            data = response.json()
            if "error" in data:
                logger.error(f"[SerpAPI] Error: {data['error']}")
                return {}
            return data
    except Exception as e:
        logger.error(f"[SerpAPI] Request failed: {e}")
        return {}


# ═══════════════════════════════════════════
#  AMAZON (SerpAPI engine=amazon)
# ═══════════════════════════════════════════

async def search_amazon(query: str, max_results: int = 5) -> list[PriceResult]:
    """
    SerpAPI Amazon Search — returns real Amazon product URLs.
    Response: organic_results[].link = "https://www.amazon.com/dp/B0..."
    """
    logger.info(f"[Amazon] Searching: {query}")
    data = await serpapi_request({
        "engine": "amazon",
        "k": query,
        "amazon_domain": "amazon.com",
        "language": "en_US",
    })

    results = []
    for item in data.get("organic_results", [])[:max_results]:
        try:
            name = item.get("title", "")
            if not name or len(name) < 5:
                continue

            # Skip sponsored
            if item.get("sponsored"):
                continue

            # Price
            price = None
            if item.get("extracted_price") is not None:
                price = float(item["extracted_price"])
            elif item.get("price"):
                price = _extract_any_price(item["price"])
            if not price or price <= 0:
                continue

            # URL — real Amazon product page
            url = item.get("link", "")
            if not url:
                continue

            results.append(PriceResult(
                platform="amazon",
                platform_name="Amazon",
                product_name=name,
                price=price,
                currency="$",
                url=url,
                seller="Amazon",
                rating=_safe_float(item.get("rating")),
                review_count=_safe_int(item.get("reviews")),
                image_url=item.get("thumbnail"),
                scraped_at=datetime.utcnow().isoformat(),
                source="serpapi_amazon",
            ))
        except Exception as e:
            logger.warning(f"[Amazon] Parse error: {e}")
            continue

    logger.info(f"[Amazon] Got {len(results)} results")
    return results


# ═══════════════════════════════════════════
#  WALMART (SerpAPI engine=walmart)
# ═══════════════════════════════════════════

async def search_walmart(query: str, max_results: int = 5) -> list[PriceResult]:
    """
    SerpAPI Walmart Search — returns real Walmart product URLs.
    Response: organic_results[].product_page_url = "https://www.walmart.com/ip/..."
    """
    logger.info(f"[Walmart] Searching: {query}")
    data = await serpapi_request({
        "engine": "walmart",
        "query": query,
    })

    results = []
    for item in data.get("organic_results", [])[:max_results]:
        try:
            name = item.get("title", "")
            if not name or len(name) < 5:
                continue

            # Sponsored check
            if item.get("sponsored"):
                continue

            # Price
            price = None
            if item.get("primary_offer", {}).get("offer_price") is not None:
                price = float(item["primary_offer"]["offer_price"])
            elif item.get("price") is not None:
                price = _extract_any_price(str(item["price"]))
            if not price or price <= 0:
                continue

            # URL — real Walmart product page
            url = item.get("product_page_url", "") or item.get("link", "")
            if not url:
                continue
            # Ensure full URL
            if url.startswith("/"):
                url = f"https://www.walmart.com{url}"

            results.append(PriceResult(
                platform="walmart",
                platform_name="Walmart",
                product_name=name,
                price=price,
                currency="$",
                url=url,
                seller="Walmart",
                rating=_safe_float(item.get("rating")),
                review_count=_safe_int(item.get("reviews")),
                image_url=item.get("thumbnail"),
                scraped_at=datetime.utcnow().isoformat(),
                source="serpapi_walmart",
            ))
        except Exception as e:
            logger.warning(f"[Walmart] Parse error: {e}")
            continue

    logger.info(f"[Walmart] Got {len(results)} results")
    return results


# ═══════════════════════════════════════════
#  EBAY (SerpAPI engine=ebay)
# ═══════════════════════════════════════════

async def search_ebay(query: str, max_results: int = 5) -> list[PriceResult]:
    """
    SerpAPI eBay Search — returns real eBay listing URLs.
    Response: organic_results[].link = "https://www.ebay.com/itm/..."
    """
    logger.info(f"[eBay] Searching: {query}")
    data = await serpapi_request({
        "engine": "ebay",
        "_nkw": query,
        "ebay_domain": "ebay.com",
    })

    results = []
    for item in data.get("organic_results", [])[:max_results]:
        try:
            name = item.get("title", "")
            if not name or len(name) < 5:
                continue

            # Price
            price = None
            if item.get("price"):
                if isinstance(item["price"], dict):
                    # eBay can return {"raw": "$25.99", "extracted": 25.99}
                    price = _safe_float(item["price"].get("extracted"))
                    if not price:
                        price = _extract_any_price(item["price"].get("raw", ""))
                else:
                    price = _extract_any_price(str(item["price"]))
            if not price or price <= 0:
                continue

            # URL — real eBay listing page
            url = item.get("link", "")
            if not url:
                continue

            results.append(PriceResult(
                platform="ebay",
                platform_name="eBay",
                product_name=name,
                price=price,
                currency="$",
                url=url,
                seller=item.get("seller", {}).get("name", "eBay Seller") if isinstance(item.get("seller"), dict) else "eBay",
                rating=_safe_float(item.get("reviews", {}).get("rating")) if isinstance(item.get("reviews"), dict) else None,
                review_count=_safe_int(item.get("reviews", {}).get("count")) if isinstance(item.get("reviews"), dict) else None,
                image_url=item.get("thumbnail"),
                scraped_at=datetime.utcnow().isoformat(),
                source="serpapi_ebay",
            ))
        except Exception as e:
            logger.warning(f"[eBay] Parse error: {e}")
            continue

    logger.info(f"[eBay] Got {len(results)} results")
    return results


# ═══════════════════════════════════════════
#  GOOGLE SHOPPING (SerpAPI engine=google_shopping)
# ═══════════════════════════════════════════

async def search_google_shopping(query: str, region: str = "us", max_results: int = 5) -> list[PriceResult]:
    """
    SerpAPI Google Shopping — aggregator results from many stores.
    URLs are Google redirects, so we extract real URLs where possible.
    """
    config = {
        "us": {"gl": "us", "hl": "en", "currency": "$", "location": "United States"},
        "eu": {"gl": "de", "hl": "de", "currency": "€", "location": "Germany"},
        "tr": {"gl": "tr", "hl": "tr", "currency": "₺", "location": "Turkey"},
    }
    cfg = config.get(region, config["us"])

    logger.info(f"[Google Shopping] Searching: {query} | region={region}")
    data = await serpapi_request({
        "engine": "google_shopping",
        "q": query,
        "gl": cfg["gl"],
        "hl": cfg["hl"],
        "location": cfg["location"],
        "num": max_results,
    })

    results = []
    for item in data.get("shopping_results", [])[:max_results]:
        try:
            name = item.get("title", "")
            if not name:
                continue

            # Price
            price = None
            if item.get("extracted_price") is not None:
                price = float(item["extracted_price"])
            else:
                price = _extract_any_price(item.get("price", ""))
            if not price or price <= 0:
                continue

            # Seller
            seller = item.get("source", "") or item.get("seller", "")
            platform_id, platform_name = _identify_platform(seller, region)

            # URL — try to extract real URL from Google redirect
            raw_url = item.get("link") or item.get("product_link") or ""
            url = _extract_real_url(raw_url)

            results.append(PriceResult(
                platform=platform_id,
                platform_name=platform_name,
                product_name=name,
                price=price,
                currency=cfg["currency"],
                url=url,
                seller=seller,
                rating=_safe_float(item.get("rating")),
                review_count=_safe_int(item.get("reviews")),
                image_url=item.get("thumbnail") or item.get("image"),
                scraped_at=datetime.utcnow().isoformat(),
                source="serpapi_google_shopping",
            ))
        except Exception as e:
            logger.warning(f"[Google Shopping] Parse error: {e}")
            continue

    logger.info(f"[Google Shopping] Got {len(results)} results")
    return results


# ═══════════════════════════════════════════
#  UTILS
# ═══════════════════════════════════════════

def _extract_any_price(text) -> Optional[float]:
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


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except:
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except:
        return None


def _extract_real_url(raw_url: str) -> str:
    """Extract real seller URL from Google redirect URLs."""
    if not raw_url:
        return ""
    if "google.com" not in raw_url:
        return raw_url
    try:
        parsed = urlparse(raw_url)
        params = parse_qs(parsed.query)
        for key in ["url", "adurl", "q", "dest"]:
            if key in params and params[key]:
                candidate = params[key][0]
                if candidate.startswith("http"):
                    return candidate
    except:
        pass
    return raw_url


def _identify_platform(seller: str, region: str) -> tuple[str, str]:
    if not seller:
        return "unknown", "Unknown"
    s = seller.lower()
    mappings = {
        "amazon": ("amazon", "Amazon"),
        "walmart": ("walmart", "Walmart"),
        "best buy": ("bestbuy", "Best Buy"),
        "bestbuy": ("bestbuy", "Best Buy"),
        "target": ("target", "Target"),
        "ebay": ("ebay", "eBay"),
        "newegg": ("newegg", "Newegg"),
        "b&h": ("bh", "B&H Photo"),
        "apple": ("apple", "Apple"),
        "nike": ("nike", "Nike"),
        "adidas": ("adidas", "Adidas"),
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

async def search_region(query: str, region: str, max_results: int = 15):
    """
    Run all relevant engines in parallel.
    US: Amazon + Walmart + eBay + Google Shopping (4 credits)
    EU: Google Shopping (1 credit) — platform APIs are US-only
    TR: Google Shopping (1 credit)
    """
    per_engine = max(3, max_results // 4)

    if region == "us":
        tasks = [
            search_amazon(query, per_engine),
            search_walmart(query, per_engine),
            search_ebay(query, per_engine),
            search_google_shopping(query, "us", per_engine),
        ]
        sources = ["amazon", "walmart", "ebay", "google_shopping"]

    elif region == "eu":
        tasks = [
            search_google_shopping(query, "eu", max_results),
        ]
        sources = ["google_shopping_eu"]

    elif region == "tr":
        tasks = [
            search_google_shopping(query, "tr", max_results),
        ]
        sources = ["google_shopping_tr"]

    else:
        return [], []

    results_nested = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    active_sources = []
    for i, r in enumerate(results_nested):
        if isinstance(r, Exception):
            logger.error(f"Search error [{sources[i]}]: {r}")
            continue
        if r:
            all_results.extend(r)
            active_sources.append(sources[i])

    # Deduplicate by normalized product name
    seen = set()
    unique = []
    for r in all_results:
        key = re.sub(r'[^a-z0-9]', '', r.product_name.lower())[:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    # Sort by price
    unique.sort(key=lambda x: x.price)
    return unique[:max_results], active_sources


# ═══════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "name": "HYPE Intelligence API",
        "version": "5.0.0",
        "status": "running",
        "supported_regions": ["us", "tr", "eu"],
        "strategy": "serpapi_multi_engine: amazon + walmart + ebay + google_shopping",
        "credits_per_us_search": 4,
        "credits_per_eu_tr_search": 1,
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
    """Debug endpoint — shows raw results from each engine with URLs."""
    debug_info = {"query": q, "region": region, "engines": {}, "errors": {}}

    engines = {
        "amazon": search_amazon(q, 3),
        "walmart": search_walmart(q, 3),
        "ebay": search_ebay(q, 3),
        "google_shopping": search_google_shopping(q, region, 3),
    }

    for name, coro in engines.items():
        try:
            results = await coro
            debug_info["engines"][name] = {
                "count": len(results),
                "items": [
                    {
                        "name": r.product_name[:80],
                        "price": r.price,
                        "currency": r.currency,
                        "url": r.url,
                        "platform": r.platform_name,
                    }
                    for r in results
                ],
            }
        except Exception as e:
            debug_info["errors"][name] = f"{type(e).__name__}: {str(e)}"

    return debug_info


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat(), "version": "5.0.0"}


@app.get("/api/credits")
async def credits_info():
    """Show credit usage info."""
    return {
        "plan": "free",
        "monthly_limit": 250,
        "cost_per_search": {
            "us": "4 credits (amazon + walmart + ebay + google_shopping)",
            "eu": "1 credit (google_shopping)",
            "tr": "1 credit (google_shopping)",
        },
        "estimated_searches": {
            "us_only": "~62/month",
            "eu_or_tr_only": "~250/month",
            "mixed": "depends on usage",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
