"""
intercept_fareharbor.py — Find FareHarbor availability API endpoint via Playwright.

FareHarbor powers thousands of activity/tour businesses (kayaking, escape rooms,
boat tours, hiking tours, etc.). Their embed widget loads availability from an API.
This script intercepts those calls to find the real endpoint.

Known shortnames to test:
  - nyc-ferry (NYC Ferry)
  - Various popular activity companies
"""

import json
import time
from playwright.sync_api import sync_playwright

# FareHarbor embed URL format
# The /embeds/book/{shortname}/ page shows a calendar of available activities
EMBED_BASE = "https://fareharbor.com/embeds/book"

# Known FareHarbor shortnames (popular companies)
SHORTNAMES = [
    "nyc-ferry",
    "central-park-bike-tours",
    "big-island-eco-adventures",
    "gosailing",
    "viator",      # probably wrong format
    "manhattan-kayak",
    "free-tours-by-foot",
    "escape-the-room-nyc",
    "nycgo",
    "circle-line-sightseeing",
]


def intercept_company(shortname: str, timeout_ms: int = 15000) -> list[dict]:
    """Load FareHarbor embed and capture API calls."""
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        def on_response(response):
            url = response.url
            if "fareharbor.com" in url and response.status == 200:
                # Skip static files
                if any(url.endswith(ext) for ext in [".js", ".css", ".png", ".ico", ".woff"]):
                    return
                if "/static/" in url or "/media/" in url:
                    return
                try:
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type or "html" in content_type:
                        body = response.text()
                        captured.append({
                            "url": url,
                            "status": response.status,
                            "content_type": content_type,
                            "body_preview": body[:500],
                        })
                except Exception:
                    pass

        page.on("response", on_response)

        url = f"{EMBED_BASE}/{shortname}/"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            time.sleep(5)
        except Exception as e:
            pass

        browser.close()

    return captured


def main():
    print("FareHarbor embed interceptor")
    print("=" * 60)

    for shortname in SHORTNAMES:
        print(f"\n[{shortname}]")
        responses = intercept_company(shortname)

        if not responses:
            print("  No FareHarbor API calls captured")
            continue

        print(f"  Captured {len(responses)} responses")
        for r in responses:
            url = r["url"]
            if "api" in url.lower() or "availab" in url.lower() or "item" in url.lower() or "calendar" in url.lower():
                print(f"  ** API: {url[:90]}")
                print(f"     Preview: {r['body_preview'][:200]}")
            else:
                print(f"  - {url[:80]}")

        # Check if any response contains availability/item data
        for r in responses:
            body = r["body_preview"]
            if any(k in body.lower() for k in ["availab", "item", "capacity", "booking", "date"]):
                if "json" in r["content_type"]:
                    print(f"\n  *** Found JSON with booking data:")
                    print(f"  URL: {r['url']}")
                    print(f"  Body: {body[:400]}")
                    return  # Found it


if __name__ == "__main__":
    main()
