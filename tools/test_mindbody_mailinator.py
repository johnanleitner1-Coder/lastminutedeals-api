"""
Full Mindbody booking test via Mailinator:
1. Create Mindbody account with mailinator email
2. Check Mailinator for verification email
3. Click verification link
4. Navigate to booking and attempt checkout

This tests whether the complete booking loop is automatable.
Uses headed browser (headless=False) to bypass Mindbody's headless detection.
"""
import time
import sys
import re
sys.stdout.reconfigure(encoding="utf-8")
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from pathlib import Path

Path(".tmp").mkdir(exist_ok=True)

TS = int(time.time())
TEST_EMAIL = f"mindbodybooking{TS}@mailinator.com"
TEST_PASS  = "TestMB2026!x"
TEST_FIRST = "Jane"
TEST_LAST  = "Smith"

SITE_ID    = "18692"
CLASS_ID   = "954"
CLASS_DATE = "3/29/2026"
CLASS_LOC  = "3"

BOOKING_URL = f"https://clients.mindbodyonline.com/ASP/res_a.asp?tg=22&classId={CLASS_ID}&classDate={CLASS_DATE}&clsLoc={CLASS_LOC}&studioid={SITE_ID}"

def ss(page, name):
    path = f".tmp/mb_mail_{name}.png"
    try:
        page.screenshot(path=path, full_page=False)
        print(f"  [ss: {path}]")
    except Exception:
        pass

def log(msg):
    print(f"\n{'='*60}\n{msg}\n{'='*60}")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    )
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.new_page()

    # ── Step 1: Open Create Account flow ──────────────────────────────────
    log("Step 1: Open Mindbody schedule + Create Account")
    page.goto(f"https://clients.mindbodyonline.com/classic/mainclass?studioid={SITE_ID}",
              wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)

    create_btn = page.query_selector("a:has-text('Create account')")
    if create_btn:
        create_btn.click()
        print("Clicked 'Create account'")
    else:
        print("No 'Create account' button — trying direct URL")
        page.goto(f"https://clients.mindbodyonline.com/consumermyinfo", timeout=30000)
    time.sleep(5)
    ss(page, "01_after_create_click")

    # ── Step 2: Find and fill the email + set up account ──────────────────
    log(f"Step 2: Fill email = {TEST_EMAIL}")

    # Wait for the email input to appear (it may be in a popover/modal)
    email_input = None
    for attempt in range(10):
        email_input = page.query_selector("input[type='email'], input[placeholder*='email' i], input[placeholder*='Email' i]")
        if email_input and email_input.is_visible():
            break
        time.sleep(0.5)

    if not email_input or not email_input.is_visible():
        print("No email input visible — checking page text:")
        print(page.evaluate("() => document.body.innerText")[:800])
        ss(page, "02_no_email_input")
    else:
        email_input.fill(TEST_EMAIL)
        print(f"Filled email: {TEST_EMAIL}")
        time.sleep(0.5)

        cont = page.query_selector("button:has-text('Continue'), button[type='submit']")
        if cont:
            cont.click()
            print("Clicked Continue")
            time.sleep(4)
            ss(page, "02_after_email_submit")

            page_text = page.evaluate("() => document.body.innerText")
            print(f"After email submit:\n{page_text[:600]}")

            # ── Step 3: Fill name + password form ─────────────────────────
            log("Step 3: Fill name + password")

            # Look for name/password fields
            fname = page.query_selector("input[placeholder*='First' i], input[name*='first' i], input[id*='first' i]")
            lname = page.query_selector("input[placeholder*='Last' i], input[name*='last' i], input[id*='last' i]")
            pword = page.query_selector("input[type='password']")

            if fname:
                fname.fill(TEST_FIRST)
                print(f"Filled first name: {TEST_FIRST}")
            if lname:
                lname.fill(TEST_LAST)
                print(f"Filled last name: {TEST_LAST}")
            if pword:
                pword.fill(TEST_PASS)
                print(f"Filled password")

            # Check what we have now
            all_inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input'))
                .filter(i => i.type !== 'hidden' && i.offsetParent !== null)
                .map(i => ({type: i.type, name: i.name, placeholder: i.placeholder, id: i.id}))""")
            print(f"All visible inputs: {all_inputs}")
            ss(page, "03_form_filled")

            # Submit the form
            submit = page.query_selector("button[type='submit'], button:has-text('Create'), button:has-text('Sign up')")
            if submit:
                submit.click()
                print("Clicked submit")
                time.sleep(5)
                ss(page, "04_after_submit")

                final_text = page.evaluate("() => document.body.innerText")
                print(f"After submit:\n{final_text[:800]}")

                # ── Step 4: Check for verification requirement ─────────────
                log("Step 4: Check for email verification requirement")
                text_lower = final_text.lower()
                if any(p in text_lower for p in ["verify", "check your email", "confirmation", "confirm your email"]):
                    print("*** EMAIL VERIFICATION REQUIRED ***")
                    print("Attempting Mailinator workaround...")

                    # ── Step 5: Check Mailinator ───────────────────────────
                    inbox_url = f"https://www.mailinator.com/v4/public/inboxes.jsp?to=mindbodybooking{TS}"
                    print(f"Checking Mailinator: {inbox_url}")
                    page.goto(inbox_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    ss(page, "05_mailinator")

                    inbox_text = page.evaluate("() => document.body.innerText")
                    print(f"Mailinator inbox:\n{inbox_text[:800]}")

                    # Look for email from Mindbody
                    email_rows = page.query_selector_all("tr[class*='email'], .msg-row, .letter-icon")
                    print(f"Email rows: {len(email_rows)}")

                    if email_rows:
                        email_rows[0].click()
                        time.sleep(3)
                        ss(page, "06_mailinator_email")
                        email_text = page.evaluate("() => document.body.innerText")
                        print(f"Email content:\n{email_text[:1000]}")

                        # Find verification link
                        verify_links = page.query_selector_all("a[href*='verify'], a[href*='confirm'], a[href*='activate']")
                        if verify_links:
                            verify_url = verify_links[0].get_attribute("href")
                            print(f"Verification link: {verify_url}")
                            page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
                            time.sleep(3)
                            ss(page, "07_after_verify")
                            verify_text = page.evaluate("() => document.body.innerText")
                            print(f"After verification:\n{verify_text[:500]}")
                    else:
                        print("No emails in Mailinator inbox yet (may take a moment)")
                elif any(p in text_lower for p in ["welcome", "success", "account created", "you're in", "signed in"]):
                    print("*** ACCOUNT CREATED WITHOUT EMAIL VERIFICATION! ***")
                    print("Attempting booking immediately...")

                    # ── Step 5: Attempt booking ────────────────────────────
                    log("Step 5: Navigate to booking")
                    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    ss(page, "05_booking_page")
                    booking_text = page.evaluate("() => document.body.innerText")
                    print(f"Booking page:\n{booking_text[:1000]}")
                else:
                    print(f"Unknown state after submit")
                    print(f"URL: {page.url}")

    browser.close()

print(f"\nTest email used: {TEST_EMAIL}")
print(f"Screenshots in .tmp/mb_mail_*.png")
