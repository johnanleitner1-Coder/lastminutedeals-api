"""
intercept_mindbody_deep.py — Deep intercept with full response capture from Mindbody classic client.

The classic client is jQuery/ASP.NET. Schedule data likely loads via POST AJAX.
This script:
1. Loads the classic schedule page
2. Waits for AJAX calls
3. Captures full response bodies from any schedule/class endpoint
4. Also tries navigating the week to trigger more API calls
"""

import json
import time
import sys
from playwright.sync_api import sync_playwright

SITE_ID = "18692"  # Stellar Bodies NYC — a real Mindbody studio


def main():
    print("Deep Mindbody intercept starting...")

    captured_responses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # Capture ALL responses, store the important ones
        def on_response(response):
            url = response.url
            status = response.status
            # Focus on Mindbody-hosted endpoints that return data
            if "mindbodyonline.com" in url and status == 200:
                # Skip static files
                if any(url.endswith(ext) for ext in [".js", ".css", ".png", ".gif", ".jpg", ".ico", ".woff", ".woff2"]):
                    return
                if "/a/scripts/" in url or "/a/styles/" in url or "static.mindbody" in url:
                    return
                try:
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type or "html" in content_type or "text" in content_type:
                        body = response.text()
                        captured_responses.append({
                            "url": url,
                            "status": status,
                            "content_type": content_type,
                            "body_length": len(body),
                            "body_preview": body[:1000],
                            "body_full": body,  # Keep full for analysis
                        })
                        print(f"  [captured] {url[:80]} ({len(body)} bytes)")
                except Exception as e:
                    pass

        page.on("response", on_response)

        url = f"https://clients.mindbodyonline.com/classic/mainclass?studioid={SITE_ID}"
        print(f"\nLoading: {url}")

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"Navigation error: {e}")

        # Wait for AJAX calls to complete
        print("Waiting for AJAX loads...")
        time.sleep(8)

        # Look at the page content
        content = page.content()
        print(f"\nPage content length: {len(content)}")

        # Try to find class data in the HTML
        if "class" in content.lower() and ("schedule" in content.lower() or "time" in content.lower()):
            print("Page contains schedule-related content")

        # Try clicking "next week" to trigger more API calls
        try:
            # Look for navigation buttons
            next_btn = page.query_selector("a.next, .next-week, [data-action='next'], .navigate-right")
            if next_btn:
                print("\nFound navigation button, clicking...")
                next_btn.click()
                time.sleep(5)
        except Exception as e:
            pass

        # Try triggering a date change via JavaScript
        try:
            # The classic client uses a form POST to load schedule
            result = page.evaluate("""
                () => {
                    // Find class schedule table
                    const tables = document.querySelectorAll('table');
                    const rows = document.querySelectorAll('.classRow, .hc_class, tr[class*="class"]');
                    return {
                        tables: tables.length,
                        classRows: rows.length,
                        bodyText: document.body.innerText.substring(0, 2000),
                    };
                }
            """)
            print(f"\nPage analysis:")
            print(f"  Tables: {result['tables']}")
            print(f"  Class rows: {result['classRows']}")
            print(f"  Body text preview:")
            print(f"  {result['bodyText'][:500]}")
        except Exception as e:
            print(f"JS eval error: {e}")

        browser.close()

    print(f"\n\nCaptured {len(captured_responses)} responses")
    print("\n=== Analyzing captured responses ===")

    for r in captured_responses:
        print(f"\n[{r['url'][:80]}]")
        print(f"  Status: {r['status']} | Content-Type: {r['content_type']}")
        print(f"  Size: {r['body_length']} bytes")
        print(f"  Preview: {r['body_preview'][:300]}")

    # Save all captured data
    import os
    os.makedirs(".tmp", exist_ok=True)

    # Save detailed dump
    with open(".tmp/mindbody_deep_intercept.json", "w", encoding="utf-8") as f:
        json.dump([
            {k: v for k, v in r.items() if k != "body_full"}
            for r in captured_responses
        ], f, indent=2)
    print("\nSaved to .tmp/mindbody_deep_intercept.json")

    # Look for any JSON responses that contain class/schedule data
    print("\n=== Looking for class data in responses ===")
    for r in captured_responses:
        body = r.get("body_full", "")
        if any(kw in body.lower() for kw in ["classtime", "start_time", "class_name", "servicename", "classname"]):
            print(f"\n*** CLASS DATA FOUND in: {r['url']}")
            print(f"  Body: {body[:1000]}")


if __name__ == "__main__":
    main()
