"""
Test the Mindbody booking flow to understand what's required.
Loads the 'Sign Up Now' flow for a real class without credentials.
"""
import time
from playwright.sync_api import sync_playwright
from pathlib import Path

SITE_ID = "18692"  # Stellar Bodies

def main():
    # From the HTML, the booking URL format is:
    # /ASP/res_a.asp?tg=22&classId=954&classDate=3/29/2026&clsLoc=3
    # Let's load this directly and see what we get

    booking_url = f"https://clients.mindbodyonline.com/ASP/res_a.asp?tg=22&classId=954&classDate=3/29/2026&clsLoc=3&studioid={SITE_ID}"
    schedule_url = f"https://clients.mindbodyonline.com/classic/mainclass?studioid={SITE_ID}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headed so we can see what happens
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        print(f"Step 1: Load schedule page to establish session...")
        page.goto(schedule_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

        print(f"Step 2: Navigate to booking URL directly...")
        page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        current_url = page.url
        html = page.content()
        text = page.evaluate("() => document.body.innerText")

        print(f"Current URL: {current_url}")
        print(f"Page title: {page.title()}")
        print(f"Body text (first 2000 chars):\n{text[:2000]}")

        # Save the HTML for inspection
        Path(".tmp").mkdir(exist_ok=True)
        Path(".tmp/mindbody_booking_flow.html").write_text(html, encoding="utf-8")
        print(f"\nSaved HTML to .tmp/mindbody_booking_flow.html")

        # Look for forms
        forms = page.query_selector_all("form")
        print(f"\nForms found: {len(forms)}")
        for form in forms:
            action = form.get_attribute("action") or ""
            print(f"  Form action: {action}")

        # Look for payment fields
        inputs = page.query_selector_all("input")
        print(f"\nInputs found: {len(inputs)}")
        for inp in inputs[:20]:
            name = inp.get_attribute("name") or ""
            type_ = inp.get_attribute("type") or ""
            placeholder = inp.get_attribute("placeholder") or ""
            print(f"  input[name={name}, type={type_}, placeholder={placeholder}]")

        # Screenshot
        page.screenshot(path=".tmp/mindbody_booking_flow.png", full_page=True)
        print("\nScreenshot saved to .tmp/mindbody_booking_flow.png")

        print("\nPress Enter to close...")
        input()
        browser.close()

if __name__ == "__main__":
    main()
