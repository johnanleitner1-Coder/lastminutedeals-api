"""
intercept_mindbody_widget.py — Intercept API calls from a real Mindbody widget booking page

Strategy: Load actual studio booking pages that use Mindbody's embedded widget.
Capture all XHR/fetch requests made by the widget to find the real availability endpoint.
"""

import json
import time
from playwright.sync_api import sync_playwright

# Known studios that use Mindbody widgets — their embedded booking pages
STUDIO_PAGES = [
    # CorePower Yoga — large chain, definitely uses Mindbody
    ("CorePower Yoga NYC", "https://www.corepoweryoga.com/yoga-schedule"),
    # SoulCycle — uses Mindbody
    ("SoulCycle", "https://www.soul-cycle.com/classes/"),
    # Exhale Spa
    ("Exhale Spa", "https://www.exhalespa.com/locations/"),
    # Try a direct Mindbody-hosted booking page
    ("MB Direct - SiteID 18692", "https://clients.mindbodyonline.com/classic/mainclass?studioid=18692"),
    ("MB Widgets Direct", "https://widgets.mindbodyonline.com/widgets/appointments/18692/load_brick_json?date=2026-03-28"),
]

# Direct Mindbody client pages — these are the classic booking interfaces
DIRECT_MB_PAGES = [
    ("MB Classic 18692", "https://clients.mindbodyonline.com/classic/mainclass?studioid=18692"),
    ("MB Classic 152065", "https://clients.mindbodyonline.com/classic/mainclass?studioid=152065"),
    ("MB Branded 18692", "https://brandedweb.mindbodyonline.com/branded-web/schedule?siteId=18692"),
    ("MB Branded Alt", "https://brandedweb.mindbodyonline.com/branded-web/booking/class?siteId=18692&date=2026-03-28"),
]


def intercept_page(url: str, label: str, timeout_ms: int = 20000) -> list[dict]:
    """Load a page and capture all network requests."""
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        def on_request(request):
            req_url = request.url
            # Filter for interesting API calls
            if any(kw in req_url for kw in [
                "mindbody", "mindbodyonline", "api", "schedule",
                "class", "appointment", "availability", "slot",
                "booking", "session", "service"
            ]):
                captured.append({
                    "method": request.method,
                    "url": req_url,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                })

        def on_response(response):
            resp_url = response.url
            if any(kw in resp_url for kw in [
                "mindbody", "mindbodyonline", "api", "schedule",
                "class", "appointment", "availability", "slot"
            ]):
                try:
                    status = response.status
                    if status == 200:
                        try:
                            body = response.json()
                            # Find the entry and update with response
                            for item in captured:
                                if item["url"] == resp_url:
                                    item["response_status"] = status
                                    item["response_sample"] = json.dumps(body)[:500]
                                    break
                            else:
                                captured.append({
                                    "method": "RESPONSE",
                                    "url": resp_url,
                                    "response_status": status,
                                    "response_sample": json.dumps(body)[:500],
                                })
                        except Exception:
                            pass
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            time.sleep(5)  # Wait for async widget loads
        except Exception as e:
            print(f"  [load error] {e}")

        browser.close()

    return captured


def main():
    print("=" * 70)
    print("Mindbody Widget API Interceptor")
    print("=" * 70)

    # First try: direct Mindbody client pages (most likely to expose the API)
    all_urls = DIRECT_MB_PAGES + STUDIO_PAGES

    for label, url in all_urls:
        print(f"\n[{label}]")
        print(f"  URL: {url}")
        reqs = intercept_page(url, label)

        if not reqs:
            print("  -> No Mindbody API calls captured")
        else:
            print(f"  -> Captured {len(reqs)} relevant requests:")
            for r in reqs:
                method = r.get("method", "")
                req_url = r.get("url", "")
                status = r.get("response_status", "?")
                sample = r.get("response_sample", "")
                post = r.get("post_data", "")

                print(f"    [{method}] {req_url[:100]}")
                if post:
                    print(f"      POST data: {str(post)[:200]}")
                if sample:
                    print(f"      Response ({status}): {sample[:200]}")

        # If we found a 200 response with class data, stop here
        working = [r for r in reqs if r.get("response_status") == 200 and r.get("response_sample")]
        if working:
            print(f"\n  *** FOUND WORKING ENDPOINTS ***")
            for r in working:
                print(f"    {r['url']}")
            # Save for analysis
            with open(".tmp/mindbody_intercepted.json", "w") as f:
                json.dump({"source": label, "url": url, "requests": reqs}, f, indent=2)
            print("\n  Saved to .tmp/mindbody_intercepted.json")
            break



if __name__ == "__main__":
    main()
