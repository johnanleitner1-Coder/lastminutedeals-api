"""Test Mindbody week navigation in Playwright."""
import time
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone
from pathlib import Path

def main():
    site_id = "18692"
    url = f"https://clients.mindbodyonline.com/classic/mainclass?studioid={site_id}"
    now_utc = datetime.now(timezone.utc)
    print(f"Current UTC time: {now_utc.isoformat()}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ).new_page()

        print(f"\nLoading {url}...")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)

        # Check what week is showing
        print("\n=== Week 1 dates ===")
        dates1 = page.evaluate("""
            () => {
                const headers = document.querySelectorAll('.classSchedule-mainTable-loaded .header');
                return Array.from(headers).map(h => h.textContent.trim());
            }
        """)
        for d in dates1:
            print(f"  {d}")

        # Count class rows
        rows1 = page.evaluate("""
            () => document.querySelectorAll('.classSchedule-mainTable-loaded .row').length
        """)
        print(f"Class rows: {rows1}")

        # Try clicking the week forward button
        print("\n=== Navigating to next week ===")

        # Check if button exists
        btn = page.query_selector("#week-arrow-r")
        print(f"#week-arrow-r found: {btn is not None}")

        if btn:
            # Check if it's visible/clickable
            is_visible = btn.is_visible()
            print(f"Is visible: {is_visible}")

            try:
                btn.click()
                print("Clicked!")
                time.sleep(4)

                # Check new dates
                dates2 = page.evaluate("""
                    () => {
                        const headers = document.querySelectorAll('.classSchedule-mainTable-loaded .header');
                        return Array.from(headers).map(h => h.textContent.trim());
                    }
                """)
                print("\n=== Week 2 dates ===")
                for d in dates2:
                    print(f"  {d}")

                rows2 = page.evaluate("""
                    () => document.querySelectorAll('.classSchedule-mainTable-loaded .row').length
                """)
                print(f"Class rows: {rows2}")

                # Save next week HTML
                html2 = page.content()
                Path(".tmp/mindbody_nextweek.html").write_text(html2, encoding="utf-8")
                print("\nSaved next week HTML to .tmp/mindbody_nextweek.html")

            except Exception as e:
                print(f"Click failed: {e}")

                # Alternative: use JavaScript to trigger navigation
                print("Trying JS click...")
                try:
                    page.evaluate("document.getElementById('week-arrow-r').click()")
                    time.sleep(4)
                    dates3 = page.evaluate("""
                        () => {
                            const headers = document.querySelectorAll('.classSchedule-mainTable-loaded .header');
                            return Array.from(headers).map(h => h.textContent.trim());
                        }
                    """)
                    print("After JS click:")
                    for d in dates3:
                        print(f"  {d}")
                except Exception as e2:
                    print(f"JS click also failed: {e2}")
        else:
            # List all elements that could be navigation
            nav_info = page.evaluate("""
                () => {
                    const els = document.querySelectorAll('#week-arrow-r, .date-arrow-r, .week-arrow');
                    return Array.from(els).map(el => ({
                        id: el.id,
                        class: el.className,
                        tag: el.tagName,
                        text: el.textContent.trim(),
                        onclick: el.onclick ? 'has onclick' : 'no onclick',
                    }));
                }
            """)
            print("Navigation elements found:")
            for el in nav_info:
                print(f"  {el}")

        browser.close()

if __name__ == "__main__":
    main()
