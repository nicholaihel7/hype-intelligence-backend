# HYPE Intelligence — Backend

Real-time price intelligence API with multi-platform scraping.

## Supported Platforms

### US
- Amazon US
- Walmart  
- Best Buy

### Turkey
- Trendyol
- Hepsiburada

### Europe
- Amazon DE

## Quick Start (Local)

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload --port 8000
```

## API Usage

```bash
# Search across all US platforms
curl "http://localhost:8000/api/search?q=iPhone+16+Pro&region=us"

# Search specific platforms
curl "http://localhost:8000/api/search?q=iPhone+16+Pro&region=us&platforms=amazon_us,walmart"

# Search Turkey
curl "http://localhost:8000/api/search?q=iPhone+16+Pro&region=tr"

# List platforms
curl "http://localhost:8000/api/platforms"
```

## Deploy to Railway

1. Push this folder to a GitHub repo
2. Connect repo to Railway
3. Railway auto-detects the Dockerfile
4. Set port to 8000
5. Done

## Architecture

```
Request → FastAPI → Scraper Registry → [Platform Scrapers] → Parsed Results
                                            ↓
                                    Strategy 1: httpx + BeautifulSoup (fast)
                                    Strategy 2: Playwright (fallback for JS)
```

Each scraper runs concurrently via `asyncio.gather()`.
Average response time: 2-5 seconds for all platforms.

## Adding New Platforms

1. Create a new class extending `BaseScraper`
2. Implement `search(query, max_results)` method
3. Add to `SCRAPERS` registry
4. Done — the API automatically includes it
