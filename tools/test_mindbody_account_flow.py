"""
Test the full Mindbody account creation + booking flow.

Goal: find out exactly where the wall is.
  - Can we create an account without email verification?
  - After creating + logging in, can we reach the booking/payment page?
  - What does Mindbody ask for at each step?

Uses a real-format test email (won't receive it, but maps the flow).
"""
import time
import sys
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SITE_ID = "18692"  # Stellar Bodies NYC

# Test customer info
TS = int(time.time())
TEST_EMAIL = f"testbooking{TS}@mailinator.com"  # Mailinator = public inbox we can check
TEST_PASSWORD = "TestPass123!"
TEST_FIRST = "Test"
TEST_LAST = "Booking"
TEST_PHONE = "5551234567"
TEST_DOB = "01/15/1990"

# The class we want to book (from our schedule scrape)
# classId=954, date=3/29/2026, location=3 (Stellar Bodies NYC)
CLASS_ID = "954"
CLASS_DATE = "3/29/2026"
CLASS_LOC = "3"

SCHEDULE_URL = f"https://clients.mindbodyonline.com/classic/mainclass?studioid={SITE_ID}"
CREATE_ACCT_URL = f"https://clients.mindbodyonline.com/ASP/su2.asp?studioid={SITE_ID}"
BOOKING_URL = f"https://clients.mindbodyonline.com/ASP/res_a.asp?tg=22&classId={CLASS_ID}&classDate={CLASS_DATE}&clsLoc={CLASS_LOC}&studioid={SITE_ID}"

Path(".tmp").mkdir(exist_ok=True)


def screenshot(page, name):
    path = f".tmp/mb_test_{name}.png"
    try:
        page.screenshot(path=path, full_page=True)
        print(f"  [screenshot: {path}]")
    except Exception:
        pass


def page_summary(page):
    url = page.url
    title = page.title()
    try:
        text = page.evaluate("() => document.body.innerText")
    except Exception:
        text = ""
    return url, title, text


def main():
    print("=" * 65)
    print(f"Mindbody Account + Booking Flow Test")
    print(f"Test email: {TEST_EMAIL}")
    print(f"Target class: #{CLASS_ID} on {CLASS_DATE}")
    print("=" * 65)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        # ── Step 1: Load schedule (establish session) ──────────────────────
        print("\n[Step 1] Load studio schedule page...")
        page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        url, title, text = page_summary(page)
        print(f"  URL: {url}")
        print(f"  Title: {title}")
        screenshot(page, "01_schedule")

        # ── Step 2: Navigate to Create Account ────────────────────────────
        print("\n[Step 2] Navigate to Create Account page...")
        page.goto(CREATE_ACCT_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        url, title, text = page_summary(page)
        print(f"  URL: {url}")
        print(f"  Title: {title}")
        print(f"  Page text snippet: {text[:500]}")
        screenshot(page, "02_create_account")

        # Find account creation form
        form_inputs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input:not([type=hidden])'))
                .map(i => ({name: i.name, id: i.id, type: i.type, placeholder: i.placeholder}))
                .slice(0, 20);
        }""")
        print(f"  Form inputs: {form_inputs}")

        # ── Step 3: Fill in account creation form ─────────────────────────
        print("\n[Step 3] Filling account creation form...")
        try:
            # Try common field names for Mindbody registration
            field_fills = [
                (["requiredtxtFirstName", "txtFirstName", "firstName", "first_name"], TEST_FIRST),
                (["requiredtxtLastName", "txtLastName", "lastName", "last_name"], TEST_LAST),
                (["requiredtxtEmail_Address", "txtEmail", "email", "email_address"], TEST_EMAIL),
                (["requiredtxtPassword", "txtPassword", "password"], TEST_PASSWORD),
                (["txtBirthDate", "birthdate", "dob", "date_of_birth", "txtDOB"], TEST_DOB),
                (["txtMobilePhone", "txtPhone", "phone", "mobile_phone"], TEST_PHONE),
            ]

            filled = []
            for field_names, value in field_fills:
                for fname in field_names:
                    el = page.query_selector(f"input[name='{fname}'], input[id='{fname}']")
                    if el and el.is_visible():
                        el.fill(value)
                        filled.append(fname)
                        print(f"  Filled {fname} = {value[:20]}")
                        break

            print(f"  Fields filled: {filled}")

            # Look for submit button
            submit_btn = page.query_selector(
                "input[type='submit'], button[type='submit'], input[name*='btn'], input[value*='Next'], input[value*='Sign Up']"
            )
            if submit_btn:
                btn_val = submit_btn.get_attribute("value") or submit_btn.inner_text()
                print(f"  Submit button: '{btn_val}'")
                screenshot(page, "03_filled_form")

                print("  Clicking submit...")
                submit_btn.click()
                time.sleep(4)

                url, title, text = page_summary(page)
                print(f"  After submit URL: {url}")
                print(f"  After submit title: {title}")
                print(f"  After submit text: {text[:800]}")
                screenshot(page, "04_after_submit")
            else:
                print("  No submit button found!")
                screenshot(page, "03_no_submit")

        except Exception as e:
            print(f"  Error in form fill: {e}")
            import traceback
            traceback.print_exc()
            screenshot(page, "03_error")

        # ── Step 4: Check what happened ───────────────────────────────────
        print("\n[Step 4] Analyzing result...")
        url, title, text = page_summary(page)
        text_lower = text.lower()

        if any(phrase in text_lower for phrase in [
            "verify", "confirmation email", "check your email",
            "activate", "email sent", "please check"
        ]):
            print("  *** EMAIL VERIFICATION REQUIRED - same wall as Eventbrite ***")
            print(f"  Message: {text[:400]}")
        elif any(phrase in text_lower for phrase in [
            "sign in", "log in", "login", "password"
        ]) and "consumermyinfo" in url:
            print("  *** Redirected to login - account may have been created ***")
        elif any(phrase in text_lower for phrase in [
            "welcome", "account created", "profile", "my account", "dashboard"
        ]):
            print("  *** ACCOUNT CREATED SUCCESSFULLY without email verification! ***")
        else:
            print(f"  *** Unknown state — URL: {url}")
            print(f"  Text: {text[:600]}")

        # ── Step 5: Try to log in immediately ────────────────────────────
        print("\n[Step 5] Attempting login with new credentials...")
        page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # Navigate to booking URL to trigger login
        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        url, title, text = page_summary(page)
        print(f"  URL after going to booking: {url}")

        # Fill login form
        try:
            # Username/email
            un = page.query_selector("input[name='requiredtxtUserName'], input[name='txtUserName'], input[type='email']")
            pw = page.query_selector("input[name='requiredtxtPassword'], input[name='txtPassword'], input[type='password']")

            if un and pw:
                un.fill(TEST_EMAIL)
                pw.fill(TEST_PASSWORD)
                print(f"  Filled login form")

                login_btn = page.query_selector("input[name='btnSignUp2'], input[type='submit'], button[type='submit']")
                if login_btn:
                    login_btn.click()
                    time.sleep(4)

                    url, title, text = page_summary(page)
                    print(f"  After login URL: {url}")
                    print(f"  After login text: {text[:800]}")
                    screenshot(page, "05_after_login")

                    text_lower = text.lower()
                    if "verify" in text_lower or "confirm" in text_lower:
                        print("\n  *** VERDICT: EMAIL VERIFICATION REQUIRED before login ***")
                    elif "invalid" in text_lower or "incorrect" in text_lower or "error" in text_lower:
                        print("\n  *** VERDICT: Login failed - account not created or wrong creds ***")
                    elif "res_a" in url or "checkout" in url or "payment" in url or "credit card" in text_lower:
                        print("\n  *** VERDICT: REACHED BOOKING/PAYMENT PAGE - no email verification needed! ***")
                    elif "consumermyinfo" in url or "myinfo" in url:
                        print("\n  *** VERDICT: Logged in! Now at account page ***")
                        # Try to navigate to booking
                        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(4)
                        url2, title2, text2 = page_summary(page)
                        print(f"  Booking URL after login: {url2}")
                        print(f"  Booking text: {text2[:800]}")
                        screenshot(page, "06_booking_after_login")
                    else:
                        print(f"\n  *** VERDICT: Unknown state at {url}")
            else:
                print(f"  Login form not found (un={un is not None}, pw={pw is not None})")
                screenshot(page, "05_no_login_form")
        except Exception as e:
            print(f"  Login error: {e}")

        browser.close()

    print("\n" + "=" * 65)
    print("Screenshots saved to .tmp/mb_test_*.png")


if __name__ == "__main__":
    main()
