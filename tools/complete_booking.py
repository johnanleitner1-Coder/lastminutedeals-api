"""
complete_booking.py — Playwright booking fulfillment engine for LastMinuteDeals.

Called by run_api_server.py after a Stripe payment is authorized.
Navigates to the source platform, fills in customer details, and completes
the actual reservation. Returns a confirmation string on success or raises
a typed exception so the caller can cancel the payment hold.

Supported platforms:
  eventbrite    — ticket registration form
  mindbody      — wellness/fitness booking checkout
  luma          — lu.ma event RSVP
  meetup        — Meetup RSVP flow
  ticketmaster  — ticket checkout (bot-detection flagged; partial automation)
  <other>       — GenericBooker fallback (never raises; flags for manual review)

Usage (programmatic):
    from tools.complete_booking import complete_booking
    confirmation = complete_booking(
        slot_id="abc123",
        customer={"name": "Jane Smith", "email": "jane@example.com", "phone": "+15550001234"},
        platform="eventbrite",
        booking_url="https://www.eventbrite.com/e/...",
    )

Usage (CLI for testing):
    python tools/complete_booking.py \\
        --slot-id abc123 \\
        --name "Jane Smith" \\
        --email jane@example.com \\
        --phone +15550001234 \\
        --platform eventbrite \\
        --url https://www.eventbrite.com/e/...

DEPENDENCY: pip install playwright && playwright install chromium
"""

import argparse
import json
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

sys.stdout.reconfigure(encoding="utf-8")

# ── Playwright availability guard ─────────────────────────────────────────────

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False


# ── Custom exception hierarchy ────────────────────────────────────────────────

class BookingError(Exception):
    """Base class for all booking failures."""


class BookingUnavailableError(BookingError):
    """Slot is sold out or no longer exists on the platform."""


class BookingAuthRequired(BookingError):
    """Platform requires an account login — cannot automate."""


class BookingTimeoutError(BookingError):
    """Page did not load or a required action timed out."""


class BookingUnknownError(BookingError):
    """Unexpected failure; a screenshot has been saved to .tmp/booking_failures/."""


# ── Helpers ───────────────────────────────────────────────────────────────────

_FAILURE_DIR = Path(".tmp") / "booking_failures"

# Full anti-detection script — removes webdriver flag, fakes plugins/chrome/languages.
# Required for Mindbody OIDC; harmless for all other platforms.
_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {0: {type:'application/x-google-chrome-pdf', suffixes:'pdf', description:'Portable Document Format', enabledPlugin: Plugin}, description:'Chrome PDF Plugin', filename:'internal-pdf-viewer', length:1, name:'Chrome PDF Plugin'},
        {0: {type:'application/pdf', suffixes:'pdf', description:'', enabledPlugin: Plugin}, description:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', length:1, name:'Chrome PDF Viewer'},
    ]
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = {
    app: { isInstalled: false },
    webstore: { onInstallStageChanged: {}, onDownloadProgress: {} },
    runtime: {
        PlatformOs: {MAC:'mac', WIN:'win', ANDROID:'android', CROS:'cros', LINUX:'linux', OPENBSD:'openbsd'},
        PlatformArch: {ARM:'arm', X86_32:'x86-32', X86_64:'x86-64'},
        RequestUpdateCheckStatus: {THROTTLED:'throttled', NO_UPDATE:'no_update', UPDATE_AVAILABLE:'update_available'},
        OnInstalledReason: {INSTALL:'install', UPDATE:'update', CHROME_UPDATE:'chrome_update', SHARED_MODULE_UPDATE:'shared_module_update'},
        OnRestartRequiredReason: {APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic'},
    },
};
try {
    const origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() {
            const win = origContentWindow.get.call(this);
            if (win) { try { Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined }); } catch(e) {} }
            return win;
        }
    });
} catch(e) {}
try {
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );
} catch(e) {}
"""

_COOKIE_SELECTORS = [
    'button:has-text("Accept")',
    'button:has-text("Accept All")',
    'button:has-text("Accept Cookies")',
    'button:has-text("I Accept")',
    'button:has-text("OK")',
    'button:has-text("Agree")',
    '[id*="cookie"] button',
    '[class*="cookie"] button',
    '[aria-label*="cookie" i] button',
]


def _dismiss_cookies(page: "Page", timeout: int = 4000) -> None:
    """Attempt to click a cookie-consent button if one is visible."""
    for sel in _COOKIE_SELECTORS:
        try:
            page.click(sel, timeout=timeout)
            print(f"[complete_booking] Dismissed cookie banner: {sel}")
            return
        except Exception:
            continue


def _save_failure_screenshot(page: "Page", slot_id: str) -> str:
    """Take a screenshot and save it to .tmp/booking_failures/. Returns the path."""
    _FAILURE_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", slot_id)[:24]
    path = _FAILURE_DIR / f"failure_{safe_id}_{ts}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"[complete_booking] Failure screenshot saved: {path}")
    except Exception as exc:
        print(f"[complete_booking] Could not save screenshot: {exc}")
    return str(path)


_SUCCESS_DIR = Path(".tmp") / "booking_confirmations"

def _save_success_screenshot(page: "Page", slot_id: str) -> str:
    """Take a screenshot of the confirmation page and save it. Returns the path."""
    _SUCCESS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", slot_id)[:24]
    path = _SUCCESS_DIR / f"confirm_{safe_id}_{ts}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"[complete_booking] Confirmation screenshot saved: {path}")
    except Exception as exc:
        print(f"[complete_booking] Could not save confirmation screenshot: {exc}")
    return str(path)


def _make_context(playwright, headless: bool, timeout: int):
    """Launch a Chromium browser context with a realistic user-agent."""
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="America/New_York",
    )
    # Full anti-detection: removes webdriver flag, adds window.chrome, fakes plugins
    context.add_init_script(_STEALTH_INIT_JS)
    return browser, context


# ── Abstract base ─────────────────────────────────────────────────────────────

class BasePlatformBooker(ABC):
    """
    Abstract base for platform-specific booking automation.

    Subclasses implement `complete()`. The `run()` wrapper handles the browser
    lifecycle, cookie banners, error screenshots, and exception translation.
    """

    def __init__(
        self,
        slot_id: str,
        customer: dict,
        booking_url: str,
        headless: bool = True,
        timeout_ms: int = 30_000,
    ):
        self.slot_id = slot_id
        self.customer = customer
        self.booking_url = booking_url
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.name = customer.get("name", "")
        self.email = customer.get("email", "")
        self.phone = customer.get("phone", "")

    @abstractmethod
    def complete(self, page: "Page") -> str:
        """
        Execute the platform-specific booking flow.

        Args:
            page: Playwright Page object (browser already open, no URL loaded yet)

        Returns:
            Confirmation string (reference number, URL, or descriptive token)

        Raises:
            BookingUnavailableError, BookingAuthRequired, BookingTimeoutError,
            BookingUnknownError, or any Exception (caller wraps into BookingUnknownError)
        """

    def run(self) -> str:
        """
        Open a browser, run `complete()`, handle errors, close browser.
        This is what the dispatcher calls.
        """
        if not PLAYWRIGHT_AVAILABLE:
            return "Manual fulfillment required — install playwright"

        platform_name = self.__class__.__name__
        print(f"[complete_booking] [{platform_name}] slot={self.slot_id}")
        print(f"[complete_booking] [{platform_name}] customer={self.name} <{self.email}>")
        print(f"[complete_booking] [{platform_name}] url={self.booking_url}")

        with sync_playwright() as p:
            browser, context = _make_context(p, self.headless, self.timeout_ms)
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            if STEALTH_AVAILABLE:
                stealth_sync(page)
            try:
                result = self.complete(page)
                print(f"[complete_booking] [{platform_name}] confirmed: {result}")
                _save_success_screenshot(page, self.slot_id)
                return result
            except (BookingUnavailableError, BookingAuthRequired, BookingTimeoutError):
                _save_failure_screenshot(page, self.slot_id)
                raise
            except BookingUnknownError:
                raise
            except Exception as exc:
                screenshot = _save_failure_screenshot(page, self.slot_id)
                raise BookingUnknownError(
                    f"Unexpected failure on {platform_name}: {exc} | screenshot: {screenshot}"
                ) from exc
            finally:
                try:
                    browser.close()
                except Exception:
                    pass


# ── Platform implementations ──────────────────────────────────────────────────

class EventbriteBooker(BasePlatformBooker):
    """Complete an Eventbrite ticket registration."""

    def complete(self, page: "Page") -> str:
        print("[complete_booking] [Eventbrite] Navigating to event page...")
        try:
            page.goto(self.booking_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise BookingTimeoutError(f"Failed to load Eventbrite page: {exc}") from exc

        page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        _dismiss_cookies(page)

        # Detect sold-out state before attempting registration
        sold_out_indicators = [
            'text="Sold Out"',
            'text="This event is sold out"',
            '[class*="sold-out"]',
            'button[disabled]:has-text("Get Tickets")',
        ]
        for sel in sold_out_indicators:
            try:
                if page.is_visible(sel, timeout=2000):
                    raise BookingUnavailableError("Eventbrite event is sold out")
            except BookingUnavailableError:
                raise
            except Exception:
                pass

        # Detect login-required wall
        login_indicators = [
            'text="Log in to register"',
            'text="Sign in to get tickets"',
        ]
        for sel in login_indicators:
            try:
                if page.is_visible(sel, timeout=2000):
                    raise BookingAuthRequired("Eventbrite requires login to register")
            except BookingAuthRequired:
                raise
            except Exception:
                pass

        # Click "Get Tickets" / "Register"
        print("[complete_booking] [Eventbrite] Looking for ticket button...")
        ticket_button_selectors = [
            'button:has-text("Get Tickets")',
            'button:has-text("Register")',
            'a:has-text("Get Tickets")',
            'a:has-text("Register")',
            '[data-automation="ticket-quantity-increase"]',
        ]
        clicked = False
        for sel in ticket_button_selectors:
            try:
                page.click(sel, timeout=5000)
                clicked = True
                print(f"[complete_booking] [Eventbrite] Clicked: {sel}")
                break
            except Exception:
                continue

        if not clicked:
            print("[complete_booking] [Eventbrite] No ticket button found; attempting direct form fill")

        # Wait for form or next step to appear
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # Increase ticket quantity if stepper is present
        try:
            page.click('[data-automation="ticket-quantity-increase"]', timeout=3000)
        except Exception:
            pass

        # Click "Checkout" / "Register" to proceed to form
        for sel in ['button:has-text("Checkout")', 'a:has-text("Checkout")']:
            try:
                page.click(sel, timeout=4000)
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                break
            except Exception:
                continue

        # Fill attendee form fields
        print("[complete_booking] [Eventbrite] Filling attendee details...")
        first_name = self.name.split()[0] if self.name else ""
        last_name = self.name.split()[-1] if len(self.name.split()) > 1 else first_name

        form_fields = [
            (['input[name="first-name"]', 'input[placeholder*="First" i]', 'input[id*="first" i]'], first_name),
            (['input[name="last-name"]',  'input[placeholder*="Last" i]',  'input[id*="last" i]'],  last_name),
            (['input[name="email"]', 'input[type="email"]', 'input[placeholder*="email" i]'], self.email),
            (['input[name="phone"]', 'input[type="tel"]',   'input[placeholder*="phone" i]'], self.phone),
        ]
        for selectors, value in form_fields:
            if not value:
                continue
            for sel in selectors:
                try:
                    page.fill(sel, value, timeout=4000)
                    break
                except Exception:
                    continue

        # Confirm/agree to terms if checkbox present
        for sel in ['input[type="checkbox"][name*="agree" i]', 'input[type="checkbox"][id*="terms" i]']:
            try:
                page.check(sel, timeout=3000)
            except Exception:
                pass

        # Submit the order
        print("[complete_booking] [Eventbrite] Submitting registration...")
        submit_selectors = [
            'button:has-text("Place Order")',
            'button:has-text("Complete Registration")',
            'button:has-text("Register")',
            'button:has-text("Submit")',
            'button[type="submit"]',
        ]
        for sel in submit_selectors:
            try:
                page.click(sel, timeout=5000)
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                break
            except Exception:
                continue

        # Extract confirmation
        current_url = page.url
        print(f"[complete_booking] [Eventbrite] Post-submit URL: {current_url}")

        order_match = re.search(r"order[s]?[/=](\w+)", current_url)
        if order_match:
            return f"EB-{order_match.group(1)}"

        # Try to extract order number from page content
        try:
            content = page.content()
            page_match = re.search(r"Order\s+#?(\w{6,})", content)
            if page_match:
                return f"EB-{page_match.group(1)}"
        except Exception:
            pass

        if any(kw in current_url for kw in ("confirmation", "success", "order", "thank")):
            return f"confirmed:{current_url}"

        # Check for sold-out after submit (race condition)
        try:
            if page.is_visible('text="Sold Out"', timeout=2000):
                raise BookingUnavailableError("Eventbrite event became sold out during checkout")
        except BookingUnavailableError:
            raise
        except Exception:
            pass

        return "eventbrite:registration_submitted"


class MindbodyBooker(BasePlatformBooker):
    """
    Complete a Mindbody booking using the agency account + "Reserve for Someone Else".

    The agency Mindbody account logs in via OIDC (stealth headless), navigates to the
    per-class booking URL, and uses the "enroll others" form to book in the customer's
    name. Payment uses the agency's stored card or card details from .env.

    Required .env vars:
        MINDBODY_AGENCY_EMAIL       — agency account email (must be yopmail address if OTP needed)
        MINDBODY_AGENCY_PASSWORD    — agency account password
    Optional .env vars (for card payment if no stored credits):
        MINDBODY_AGENCY_CARD_NUMBER — credit card number
        MINDBODY_AGENCY_CARD_EXP    — MM/YY expiry
        MINDBODY_AGENCY_CARD_CVV    — CVV
        MINDBODY_AGENCY_CARD_ZIP    — billing zip code
    """

    def _extract_studio_id(self) -> str:
        """Pull studioid from booking_url query string."""
        m = re.search(r"studioid=(\d+)", self.booking_url)
        return m.group(1) if m else "18692"

    def _login_agency(self, page: "Page", email: str, password: str, studio_id: str) -> None:
        """
        Login to Mindbody with agency credentials via OIDC.
        Handles both password and OTP (passwordless) flows.
        """
        print(f"[MindbodyBooker] Loading studio schedule page (studioid={studio_id})...")
        page.goto(
            f"https://clients.mindbodyonline.com/classic/mainclass?studioid={studio_id}",
            wait_until="domcontentloaded", timeout=self.timeout_ms,
        )
        time.sleep(3)

        # Click "Sign in" link on the schedule page
        sign_in_clicked = False
        for sel in [
            'a:has-text("Sign in")',
            'a:has-text("Log in")',
            'a[href*="signin"]',
            'a[class*="signIn" i]',
        ]:
            try:
                page.click(sel, timeout=5000)
                sign_in_clicked = True
                print(f"[MindbodyBooker] Clicked sign-in: {sel}")
                time.sleep(5)
                break
            except Exception:
                continue

        if not sign_in_clicked:
            raise BookingAuthRequired("Mindbody: could not find Sign In link on schedule page")

        # Fill email — OIDC uses type='text', not type='email'
        email_filled = False
        for sel in ['input[type="text"]', 'input[type="email"]', '#username', '#email']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(email)
                    email_filled = True
                    print(f"[MindbodyBooker] Filled email with selector: {sel}")
                    break
            except Exception:
                continue

        if not email_filled:
            raise BookingAuthRequired("Mindbody OIDC: email input not found — OIDC may be blocking headless")

        # Click Continue
        for sel in ['button:has-text("Continue")', 'button[type="submit"]', 'input[type="submit"]']:
            try:
                page.click(sel, timeout=5000)
                time.sleep(3)
                break
            except Exception:
                continue

        # Try password field
        pw_el = page.query_selector('input[type="password"]')
        if pw_el and pw_el.is_visible():
            pw_el.fill(password)
            print("[MindbodyBooker] Filled password")
            for sel in ['button:has-text("Sign in")', 'button:has-text("Log in")', 'button[type="submit"]']:
                try:
                    page.click(sel, timeout=5000)
                    time.sleep(8)
                    break
                except Exception:
                    continue

        # Check for OTP inputs (passwordless / 2FA flow)
        otp_inputs = page.query_selector_all("input[type='tel'][maxlength='1']")
        if otp_inputs:
            print(f"[MindbodyBooker] OTP required — retrieving from yopmail for: {email}")
            otp = self._get_otp_from_yopmail(page, email)
            if not otp:
                raise BookingAuthRequired(
                    f"Mindbody OTP could not be retrieved from yopmail for {email}"
                )
            print(f"[MindbodyBooker] Got OTP: {otp}")
            self._fill_otp(page, otp)
            time.sleep(10)  # wait for auto-submit + redirect

        # Navigate back to studio page to plant the session cookie on clients.mindbodyonline.com
        print("[MindbodyBooker] Planting session cookie on studio page...")
        page.goto(
            f"https://clients.mindbodyonline.com/classic/mainclass?studioid={studio_id}",
            wait_until="domcontentloaded", timeout=self.timeout_ms,
        )
        time.sleep(3)

        # Verify login — "Sign in" link should be gone if logged in
        page_text = ""
        try:
            page_text = page.evaluate("() => document.body.innerText")
        except Exception:
            pass
        if "sign in" in page_text.lower() and "sign out" not in page_text.lower():
            raise BookingAuthRequired("Mindbody login failed — still seeing Sign In prompt after login attempt")

        print("[MindbodyBooker] Login verified")

    def _get_otp_from_yopmail(self, page: "Page", email: str) -> str | None:
        """Read the 6-digit OTP Mindbody sends to yopmail."""
        username = email.split("@")[0]
        page.goto(
            f"https://yopmail.com/en/wm?login={username}",
            wait_until="networkidle", timeout=30_000,
        )
        time.sleep(3)
        mail_frame = next(
            (f for f in page.frames if "/mail?" in f.url and username.lower() in f.url.lower()),
            None,
        )
        if not mail_frame:
            return None
        try:
            ft = mail_frame.inner_text("body")
            m = re.search(r"\b(\d{6})\b", ft)
            return m.group(1) if m else None
        except Exception:
            return None

    def _fill_otp(self, page: "Page", otp: str) -> None:
        """Fill 6 individual OTP digit inputs (Mindbody passwordless flow)."""
        inputs = page.query_selector_all("input[type='tel'][maxlength='1']")
        for i, digit in enumerate(otp[:6]):
            if i < len(inputs):
                inputs[i].fill(digit)
                time.sleep(0.1)

    def _handle_payment(self, page: "Page") -> None:
        """Enter card details on the Mindbody payment page."""
        card_number = os.getenv("MINDBODY_AGENCY_CARD_NUMBER", "").strip()
        card_exp    = os.getenv("MINDBODY_AGENCY_CARD_EXP", "").strip()   # MM/YY
        card_cvv    = os.getenv("MINDBODY_AGENCY_CARD_CVV", "").strip()
        card_zip    = os.getenv("MINDBODY_AGENCY_CARD_ZIP", "").strip()

        if not card_number:
            print("[MindbodyBooker] No card in .env — hoping for stored payment method")
            return

        print("[MindbodyBooker] Filling card details...")
        # Card number
        for sel in [
            'input[name*="card" i][name*="num" i]',
            'input[id*="cardnum" i]',
            'input[placeholder*="card number" i]',
            'input[name="txtCreditCard"]',
        ]:
            try:
                page.fill(sel, card_number, timeout=3000)
                break
            except Exception:
                continue

        # Expiry — split or combined
        if card_exp:
            parts = card_exp.split("/")
            month, year = (parts[0].strip() if parts else ""), (parts[1].strip() if len(parts) > 1 else "")
            for sel, val in [
                ('select[name*="exp" i][name*="month" i]', month),
                ('select[name*="exp" i][name*="year" i]',  year),
                ('input[name*="exp" i][name*="month" i]',  month),
                ('input[name*="exp" i][name*="year" i]',   year),
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        tag = page.evaluate("el => el.tagName.toLowerCase()", el)
                        if tag == "select":
                            page.select_option(sel, val, timeout=2000)
                        else:
                            page.fill(sel, val, timeout=2000)
                except Exception:
                    pass
            # Combined MM/YY field fallback
            for sel in ['input[name*="expiry" i]', 'input[placeholder*="MM/YY" i]', 'input[name*="expdate" i]']:
                try:
                    page.fill(sel, card_exp, timeout=2000)
                    break
                except Exception:
                    continue

        # CVV
        if card_cvv:
            for sel in ['input[name*="cvv" i]', 'input[name*="cvc" i]', 'input[name*="security" i]', 'input[name="txtCVV"]']:
                try:
                    page.fill(sel, card_cvv, timeout=2000)
                    break
                except Exception:
                    continue

        # Billing zip
        if card_zip:
            for sel in ['input[name*="zip" i]', 'input[name*="postal" i]', 'input[name="txtZip"]']:
                try:
                    page.fill(sel, card_zip, timeout=2000)
                    break
                except Exception:
                    continue

        # Submit payment
        for sel in [
            'input[value="Place Order"]',
            'input[value="Submit"]',
            'button:has-text("Place Order")',
            'button:has-text("Submit")',
            'input[type="submit"]',
            'button[type="submit"]',
        ]:
            try:
                page.click(sel, timeout=5000)
                print(f"[MindbodyBooker] Submitted payment: {sel}")
                time.sleep(8)
                break
            except Exception:
                continue

    def complete(self, page: "Page") -> str:
        """
        Full agency booking flow:
          1. Login with agency credentials
          2. Navigate to per-class booking URL
          3. Fill "Reserve for Someone Else" form
          4. Handle pricing page
          5. Handle payment page
          6. Return confirmation string
        """
        agency_email    = os.getenv("MINDBODY_AGENCY_EMAIL", "").strip()
        agency_password = os.getenv("MINDBODY_AGENCY_PASSWORD", "").strip()

        if not agency_email or not agency_password:
            raise BookingAuthRequired(
                "Mindbody agency credentials not configured. "
                "Set MINDBODY_AGENCY_EMAIL and MINDBODY_AGENCY_PASSWORD in .env"
            )

        studio_id = self._extract_studio_id()

        # ── Step 1: Login ─────────────────────────────────────────────────────
        self._login_agency(page, agency_email, agency_password, studio_id)

        # ── Step 2: Navigate to the specific class booking URL ─────────────────
        print(f"[MindbodyBooker] Navigating to booking URL: {self.booking_url}")
        try:
            page.goto(self.booking_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise BookingTimeoutError(f"Failed to load Mindbody booking page: {exc}") from exc
        time.sleep(5)

        # Check for sold-out
        page_text = ""
        try:
            page_text = page.evaluate("() => document.body.innerText")
        except Exception:
            pass
        for phrase in ["class is full", "fully booked", "waitlist only", "no spots available"]:
            if phrase in page_text.lower():
                raise BookingUnavailableError(f"Mindbody: class is full ({phrase})")

        # ── Step 3: "Reserve for Someone Else" form ────────────────────────────
        print(f"[MindbodyBooker] Booking for: {self.name}")

        # Fill customer name in the "Reserve for Others" text field
        reserved_for = page.query_selector("input[name='optReservedFor']")
        if reserved_for and reserved_for.is_visible():
            reserved_for.fill(self.name)
            print(f"[MindbodyBooker] Filled optReservedFor: {self.name}")

        # Check "Pay for this other client?" if present
        pay_for_other = page.query_selector("input[name='optPaidForOther']")
        if pay_for_other:
            try:
                if not pay_for_other.is_checked():
                    pay_for_other.check()
                    print("[MindbodyBooker] Checked optPaidForOther")
            except Exception:
                pass

        # Submit the "Enroll Others" form — or fall back to standard submit
        submitted = False
        for sel in [
            'input[name="SubmitEnroll2Others"]',
            'input[value*="Enroll" i]',
            'input[name*="SignUp" i]',
            'input[type="submit"]',
            'button[type="submit"]',
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    submitted = True
                    print(f"[MindbodyBooker] Submitted with: {sel}")
                    time.sleep(5)
                    break
            except Exception:
                continue

        if not submitted:
            _save_failure_screenshot(page, self.slot_id)
            raise BookingUnknownError("Mindbody: could not find submit button on reservation form")

        # ── Step 4: Pricing / shop page ────────────────────────────────────────
        current_url = page.url
        print(f"[MindbodyBooker] After submit URL: {current_url}")

        if "main_shop" in current_url or "/shop" in current_url.lower():
            print("[MindbodyBooker] On pricing page — selecting first available option...")

            # Select the first pricing radio button (single session, etc.)
            for sel in [
                'input[type="radio"][name*="pay" i]',
                'input[type="radio"]',
                '.pricingOption input[type="radio"]',
            ]:
                try:
                    radios = page.query_selector_all(sel)
                    if radios:
                        radios[0].check()
                        print(f"[MindbodyBooker] Selected pricing option via: {sel}")
                        time.sleep(1)
                        break
                except Exception:
                    continue

            # Click Checkout / Continue
            for sel in [
                'input[name="btnContinue"]',
                'input[value*="Checkout" i]',
                'input[value*="Continue" i]',
                'button:has-text("Checkout")',
                'button:has-text("Continue")',
                'input[type="submit"]',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        print(f"[MindbodyBooker] Clicked checkout: {sel}")
                        time.sleep(5)
                        break
                except Exception:
                    continue

        # ── Step 5: Payment page ───────────────────────────────────────────────
        current_url = page.url
        print(f"[MindbodyBooker] After pricing URL: {current_url}")

        if any(kw in current_url.lower() for kw in ["payment", "cc_info", "res_d", "checkout"]):
            self._handle_payment(page)

        # ── Step 6: Extract confirmation ───────────────────────────────────────
        current_url = page.url
        print(f"[MindbodyBooker] Final URL: {current_url}")

        try:
            content = page.content()
            # Confirmation / reservation number
            conf_m = re.search(
                r"(?:[Cc]onfirmation|[Rr]eservation|[Rr]eceipt)\s*(?:#|Number|No\.?|ID)?\s*:?\s*([A-Z0-9]{4,})",
                content,
            )
            if conf_m:
                return f"MB-{conf_m.group(1)}"
            # Transaction number
            txn_m = re.search(r"[Tt]ransaction\s*#?\s*:?\s*(\d{4,})", content)
            if txn_m:
                return f"MB-TXN-{txn_m.group(1)}"
        except Exception:
            pass

        if any(kw in current_url.lower() for kw in ("confirmation", "success", "receipt", "thank", "res_b")):
            return f"MB-confirmed:{current_url.split('?')[0]}"

        if "mindbodyonline.com" in current_url and "error" not in current_url.lower():
            return "mindbody:booking_submitted"

        raise BookingUnknownError(f"Mindbody booking ended at unexpected URL: {current_url}")


class LumaBooker(BasePlatformBooker):
    """Register for a Luma (lu.ma) event."""

    def complete(self, page: "Page") -> str:
        print("[complete_booking] [Luma] Navigating to event page...")
        try:
            page.goto(self.booking_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise BookingTimeoutError(f"Failed to load Luma page: {exc}") from exc

        page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        _dismiss_cookies(page)

        # Check for sold-out
        for text in ["Sold Out", "Event is full", "Registration closed", "No more spots"]:
            try:
                if page.is_visible(f'text="{text}"', timeout=2000):
                    raise BookingUnavailableError(f"Luma: {text}")
            except BookingUnavailableError:
                raise
            except Exception:
                pass

        # Click Register / RSVP button
        print("[complete_booking] [Luma] Clicking register button...")
        for sel in [
            'button:has-text("Register")',
            'button:has-text("Get Ticket")',
            'button:has-text("Get Tickets")',
            'button:has-text("RSVP")',
            'button:has-text("Attend")',
            'a:has-text("Register")',
            'a:has-text("RSVP")',
        ]:
            try:
                page.click(sel, timeout=5000)
                print(f"[complete_booking] [Luma] Clicked: {sel}")
                break
            except Exception:
                continue

        # Wait for modal / form to appear
        try:
            page.wait_for_selector('input[type="email"], input[placeholder*="email" i]', timeout=8000)
        except Exception:
            pass

        # Fill email
        print("[complete_booking] [Luma] Filling email...")
        for sel in ['input[type="email"]', 'input[placeholder*="email" i]', 'input[name="email"]']:
            try:
                page.fill(sel, self.email, timeout=4000)
                break
            except Exception:
                continue

        # Fill name (Luma sometimes asks for name separately)
        for sel in [
            'input[placeholder*="name" i]',
            'input[name="name"]',
            'input[name="full_name"]',
            'input[placeholder*="Full Name" i]',
        ]:
            try:
                page.fill(sel, self.name, timeout=3000)
                break
            except Exception:
                continue

        # Some Luma events have first/last split
        first_name = self.name.split()[0] if self.name else ""
        last_name = self.name.split()[-1] if len(self.name.split()) > 1 else first_name
        for sel, val in [
            ('input[placeholder*="First" i]', first_name),
            ('input[placeholder*="Last" i]',  last_name),
        ]:
            try:
                page.fill(sel, val, timeout=3000)
            except Exception:
                pass

        # Submit the registration
        print("[complete_booking] [Luma] Submitting registration...")
        for sel in [
            'button:has-text("Register")',
            'button:has-text("Confirm")',
            'button:has-text("Submit")',
            'button[type="submit"]',
        ]:
            try:
                page.click(sel, timeout=5000)
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                break
            except Exception:
                continue

        # Wait for confirmation message
        try:
            page.wait_for_selector(
                'text="You\'re registered", text="You\'re in", text="RSVP confirmed"',
                timeout=8000,
            )
        except Exception:
            pass

        current_url = page.url
        print(f"[complete_booking] [Luma] Post-submit URL: {current_url}")

        # Try to extract a unique confirmation token from URL or page content
        try:
            content = page.content()
        except Exception:
            content = ""

        # Luma confirmation URLs look like /events/abc-EVENT_CODE or contain a ticket token
        ticket_match = re.search(r'ticket[_-]?(?:id|code|token)["\s:=]+([a-zA-Z0-9_-]{6,})', content, re.IGNORECASE)
        if ticket_match:
            return f"LUMA-{ticket_match.group(1).upper()}"

        # Look for confirmation codes in the page JSON/data
        code_match = re.search(r'"(?:api_id|event_ticket_id|token|code)"\s*:\s*"([a-zA-Z0-9_-]{6,})"', content)
        if code_match:
            return f"LUMA-{code_match.group(1).upper()}"

        # URL-based: /checkout/SUCCESS_TOKEN or /e/EVENT_CODE/success
        url_match = re.search(r"/(?:checkout|success|confirm)/([a-zA-Z0-9_-]{6,})", current_url)
        if url_match:
            return f"LUMA-{url_match.group(1).upper()}"

        # Confirmed via page text
        confirmed_phrases = ["You're registered", "You're in", "RSVP confirmed", "See you there", "you're going"]
        if any(p.lower() in content.lower() for p in confirmed_phrases):
            # Use event slug from original URL as stable identifier
            slug_match = re.search(r"lu\.ma/([a-zA-Z0-9_-]+)", self.booking_url)
            slug = slug_match.group(1).upper() if slug_match else "CONFIRMED"
            return f"LUMA-{slug}"

        return f"luma:registered:{current_url}"


class MeetupBooker(BasePlatformBooker):
    """RSVP to a Meetup event."""

    def complete(self, page: "Page") -> str:
        print("[complete_booking] [Meetup] Navigating to event page...")
        try:
            page.goto(self.booking_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise BookingTimeoutError(f"Failed to load Meetup page: {exc}") from exc

        page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        _dismiss_cookies(page)

        # Check for event-closed / sold-out
        for text in ["Event is full", "No spots left", "Waitlist", "RSVP is closed"]:
            try:
                if page.is_visible(f'text="{text}"', timeout=2000):
                    raise BookingUnavailableError(f"Meetup: {text}")
            except BookingUnavailableError:
                raise
            except Exception:
                pass

        # Meetup frequently requires login
        for text in ["Log in", "Sign up to RSVP", "Join to attend"]:
            try:
                if page.is_visible(f'text="{text}"', timeout=2000):
                    raise BookingAuthRequired(f"Meetup requires login: {text}")
            except BookingAuthRequired:
                raise
            except Exception:
                pass

        # Click "Attend" / "RSVP" button
        print("[complete_booking] [Meetup] Clicking RSVP button...")
        rsvp_clicked = False
        for sel in [
            'button:has-text("Attend")',
            'button:has-text("RSVP")',
            'a:has-text("Attend")',
            'a:has-text("RSVP")',
            '[data-testid="rsvp-button"]',
            '[data-event-label="RSVP"]',
        ]:
            try:
                page.click(sel, timeout=5000)
                rsvp_clicked = True
                print(f"[complete_booking] [Meetup] Clicked: {sel}")
                page.wait_for_load_state("networkidle", timeout=10_000)
                break
            except Exception:
                continue

        if not rsvp_clicked:
            raise BookingAuthRequired("Meetup: no RSVP button found — likely requires login")

        # Handle any post-click modals (e.g., "Confirm your RSVP")
        for sel in [
            'button:has-text("Confirm")',
            'button:has-text("Yes, I\'ll attend")',
            'button:has-text("Submit")',
        ]:
            try:
                page.click(sel, timeout=5000)
                page.wait_for_load_state("networkidle", timeout=10_000)
                break
            except Exception:
                continue

        current_url = page.url
        print(f"[complete_booking] [Meetup] Post-RSVP URL: {current_url}")

        # Try to detect confirmation
        try:
            content = page.content()
            if any(kw in content for kw in ["You're going", "RSVP'd", "You are attending", "See you there"]):
                return "meetup:rsvp_confirmed"
        except Exception:
            pass

        if any(kw in current_url for kw in ("rsvp", "going", "confirmed", "attending")):
            return f"meetup:confirmed:{current_url}"

        return "meetup:rsvp_submitted"


class GenericBooker(BasePlatformBooker):
    """
    Fallback booker for unrecognized platforms.

    Strategy:
    1. Navigate to booking_url
    2. Look for common booking/register/checkout CTAs
    3. Try to fill any email/name form fields found
    4. Never raises — returns "Manual fulfillment needed" so payment is captured
       and the order is flagged for manual review.
    """

    def complete(self, page: "Page") -> str:
        print(f"[complete_booking] [Generic] Attempting best-effort booking at: {self.booking_url}")
        try:
            page.goto(self.booking_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 15_000))
        except Exception as exc:
            print(f"[complete_booking] [Generic] Page load issue (continuing): {exc}")

        _dismiss_cookies(page)

        # Try to find and click a primary CTA
        cta_selectors = [
            'button:has-text("Book Now")',
            'button:has-text("Register")',
            'button:has-text("Get Tickets")',
            'button:has-text("Reserve")',
            'button:has-text("Sign Up")',
            'button:has-text("Checkout")',
            'a:has-text("Book Now")',
            'a:has-text("Register")',
            'a:has-text("Get Tickets")',
        ]
        for sel in cta_selectors:
            try:
                page.click(sel, timeout=4000)
                print(f"[complete_booking] [Generic] Clicked CTA: {sel}")
                page.wait_for_load_state("networkidle", timeout=10_000)
                break
            except Exception:
                continue

        # Try to fill any visible form fields
        first_name = self.name.split()[0] if self.name else ""
        last_name = self.name.split()[-1] if len(self.name.split()) > 1 else first_name

        fill_attempts = [
            (['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]'], self.email),
            (['input[name="name"]', 'input[placeholder*="name" i]', 'input[placeholder*="Full Name" i]'], self.name),
            (['input[placeholder*="First" i]', 'input[name="firstName"]'], first_name),
            (['input[placeholder*="Last" i]',  'input[name="lastName"]'],  last_name),
            (['input[type="tel"]', 'input[name="phone"]', 'input[placeholder*="phone" i]'], self.phone),
        ]
        filled_any = False
        for selectors, value in fill_attempts:
            if not value:
                continue
            for sel in selectors:
                try:
                    page.fill(sel, value, timeout=3000)
                    filled_any = True
                    break
                except Exception:
                    continue

        if filled_any:
            # Try to submit
            for sel in ['button[type="submit"]', 'button:has-text("Submit")', 'button:has-text("Confirm")']:
                try:
                    page.click(sel, timeout=4000)
                    page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                    break
                except Exception:
                    continue

        print("[complete_booking] [Generic] Could not fully automate — flagging for manual review")
        return f"Manual fulfillment needed — {self.booking_url}"


# ── OCTO API booker (pure HTTP — no Playwright) ───────────────────────────────

class OCTOBooker(BasePlatformBooker):
    """
    Complete a booking via the OCTO standard API.

    Reads all booking parameters from booking_url (a JSON string encoded by
    fetch_octo_slots.py). Makes two OCTO API calls:
      1. POST /reservations  — creates a hold (reservation UUID returned)
      2. POST /bookings/{uuid}/confirm — confirms the hold

    Does NOT use Playwright. Overrides run() to skip the browser entirely.

    Required booking_url JSON fields:
        _type:           "octo"
        base_url:        supplier's OCTO API base URL
        api_key_env:     .env variable name holding the API key
        product_id:      OCTO product identifier
        option_id:       OCTO option identifier (usually "DEFAULT")
        availability_id: availability slot identifier from fetch
        unit_id:         unit type to book (e.g. "adult")
    """

    def complete(self, page: "Page") -> str:
        # OCTOBooker bypasses Playwright entirely — this method is never called.
        raise NotImplementedError("OCTOBooker.complete() should never be called — use run()")

    def run(self) -> str:
        """Execute OCTO reservation + confirmation via HTTP. No browser needed."""
        try:
            import requests as req
        except ImportError:
            raise BookingUnknownError(
                "OCTOBooker requires the 'requests' library. Run: pip install requests"
            )

        print(f"[OCTOBooker] slot={self.slot_id} customer={self.name} <{self.email}>")

        # ── Parse booking params from booking_url ─────────────────────────────
        try:
            params = json.loads(self.booking_url)
        except (json.JSONDecodeError, TypeError) as exc:
            raise BookingUnknownError(
                f"OCTOBooker: booking_url is not valid JSON: {exc}"
            ) from exc

        if params.get("_type") != "octo":
            raise BookingUnknownError(
                "OCTOBooker: booking_url does not contain OCTO params "
                f"(got _type={params.get('_type')!r})"
            )

        base_url       = params["base_url"].rstrip("/")
        api_key_env    = params["api_key_env"]
        product_id     = params["product_id"]
        option_id      = params["option_id"]
        availability_id = params["availability_id"]
        unit_id        = params["unit_id"]

        api_key = os.getenv(api_key_env, "").strip()
        if not api_key:
            raise BookingAuthRequired(
                f"OCTOBooker: API key not configured. Set {api_key_env} in .env"
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

        contact = {
            "fullName":     self.name,
            "emailAddress": self.email,
            "phoneNumber":  self.phone or "",
            "country":      "US",
            "locales":      ["en"],
        }

        # Reseller reference — used as voucher number by some suppliers (e.g. Zaui)
        reseller_ref = f"LMD-{self.slot_id[:12].upper()}"

        # ── Step 1: Create reservation (hold) ─────────────────────────────────
        # Try POST /reservations first (standard OCTO). Some suppliers (Zaui) only
        # support POST /bookings — fall back automatically on 400/404/405.
        print(f"[OCTOBooker] Creating reservation: product={product_id} avail={availability_id}")
        reservation_payload = {
            "productId":         product_id,
            "optionId":          option_id,
            "availabilityId":    availability_id,
            "unitItems":         [{"unitId": unit_id}],
            "resellerReference": reseller_ref,
            "contact":           contact,
        }

        try:
            resp = req.post(
                f"{base_url}/reservations",
                headers=headers,
                json=reservation_payload,
                timeout=30,
            )
        except req.RequestException as exc:
            raise BookingTimeoutError(f"OCTOBooker: reservation request failed: {exc}") from exc

        # Some suppliers (Zaui) don't implement /reservations — fall back to /bookings
        if resp.status_code in (400, 404, 405) and "invalid" in resp.text.lower():
            print(f"[OCTOBooker] /reservations not supported ({resp.status_code}) — trying POST /bookings")
            try:
                resp = req.post(
                    f"{base_url}/bookings",
                    headers=headers,
                    json=reservation_payload,
                    timeout=30,
                )
            except req.RequestException as exc:
                raise BookingTimeoutError(f"OCTOBooker: bookings request failed: {exc}") from exc

        if resp.status_code == 409:
            raise BookingUnavailableError(
                "OCTOBooker: availability slot is no longer available (409 Conflict)"
            )
        if resp.status_code == 422:
            raise BookingUnavailableError(
                f"OCTOBooker: unprocessable reservation: {resp.text[:300]}"
            )
        if not resp.ok:
            raise BookingUnknownError(
                f"OCTOBooker: reservation failed {resp.status_code}: {resp.text[:300]}"
            )

        reservation = resp.json()
        reservation_uuid = reservation.get("uuid") or reservation.get("id")
        if not reservation_uuid:
            raise BookingUnknownError(
                f"OCTOBooker: no UUID in reservation response: {resp.text[:300]}"
            )
        print(f"[OCTOBooker] Reservation created: {reservation_uuid}")

        # ── Step 2: Confirm the reservation ───────────────────────────────────
        print(f"[OCTOBooker] Confirming booking: {reservation_uuid}")
        confirm_payload = {"contact": contact, "resellerReference": reseller_ref}

        try:
            resp = req.post(
                f"{base_url}/bookings/{reservation_uuid}/confirm",
                headers=headers,
                json=confirm_payload,
                timeout=30,
            )
        except req.RequestException as exc:
            raise BookingTimeoutError(
                f"OCTOBooker: confirmation request failed: {exc}"
            ) from exc

        if not resp.ok:
            raise BookingUnknownError(
                f"OCTOBooker: confirmation failed {resp.status_code}: {resp.text[:300]}"
            )

        booking = resp.json()
        booking_uuid     = booking.get("uuid") or booking.get("id") or reservation_uuid
        booking_status   = booking.get("status", "")
        supplier_ref     = booking.get("supplierReference") or booking.get("reference") or ""

        print(f"[OCTOBooker] Confirmed: uuid={booking_uuid} status={booking_status} ref={supplier_ref}")

        if booking_status not in ("CONFIRMED", "ON_HOLD", ""):
            raise BookingUnknownError(
                f"OCTOBooker: unexpected booking status: {booking_status}"
            )

        # Return the supplier reference if available, else fall back to booking UUID
        confirmation = supplier_ref or f"OCTO-{booking_uuid}"
        print(f"[OCTOBooker] Final confirmation: {confirmation}")

        # Save JSON artifact for debugging
        try:
            _SUCCESS_DIR.mkdir(parents=True, exist_ok=True)
            ts      = int(time.time())
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", self.slot_id)[:24]
            art     = _SUCCESS_DIR / f"confirm_{safe_id}_{ts}.json"
            art.write_text(
                json.dumps({"confirmation": confirmation, "booking": booking,
                            "reservation_uuid": reservation_uuid}, indent=2),
                encoding="utf-8",
            )
            print(f"[OCTOBooker] Confirmation artifact saved: {art}")
        except Exception as exc:
            print(f"[OCTOBooker] Could not save confirmation artifact: {exc}")

        return confirmation


# ── Rezdy API booker (pure HTTP — Rezdy Agent API format) ─────────────────────

class RezdyBooker(BasePlatformBooker):
    """
    Complete a booking via the Rezdy Agent API.

    Rezdy's Agent API uses a different format from OCTO. Reads booking params
    from booking_url (JSON encoded by fetch_rezdy_slots.py).

    Required booking_url JSON fields:
        _type:            "rezdy"
        api_key_env:      .env variable name holding the Rezdy API key
        product_code:     Rezdy product code (e.g. "P12345")
        start_time_local: session start time ("YYYY-MM-DD HH:MM:SS") — must match exactly
        option_label:     price option label (e.g. "Adult")

    API docs: https://developers.rezdy.com/rezdyapi/index-agent.html
    """

    def complete(self, page: "Page") -> str:
        raise NotImplementedError("RezdyBooker.complete() should never be called — use run()")

    def run(self) -> str:
        """Execute a Rezdy booking via HTTP POST /bookings. No browser needed."""
        try:
            import requests as req
        except ImportError:
            raise BookingUnknownError(
                "RezdyBooker requires the 'requests' library. Run: pip install requests"
            )

        print(f"[RezdyBooker] slot={self.slot_id} customer={self.name} <{self.email}>")

        # ── Parse booking params ──────────────────────────────────────────────
        try:
            params = json.loads(self.booking_url)
        except (json.JSONDecodeError, TypeError) as exc:
            raise BookingUnknownError(
                f"RezdyBooker: booking_url is not valid JSON: {exc}"
            ) from exc

        if params.get("_type") != "rezdy":
            raise BookingUnknownError(
                f"RezdyBooker: booking_url has wrong _type={params.get('_type')!r}"
            )

        api_key_env     = params["api_key_env"]
        product_code    = params["product_code"]
        start_time_local = params["start_time_local"]
        option_label    = params.get("option_label", "Adult")

        api_key = os.getenv(api_key_env, "").strip()
        if not api_key:
            raise BookingAuthRequired(
                f"RezdyBooker: API key not configured. Set {api_key_env} in .env"
            )

        base_url = "https://api.rezdy.com/v1"

        # Split customer name
        name_parts = self.name.strip().split()
        first_name = name_parts[0] if name_parts else "Guest"
        last_name  = name_parts[-1] if len(name_parts) > 1 else first_name

        # ── POST /bookings ────────────────────────────────────────────────────
        print(f"[RezdyBooker] Booking: product={product_code} start={start_time_local}")
        payload = {
            "customer": {
                "firstName": first_name,
                "lastName":  last_name,
                "email":     self.email,
                "phone":     self.phone or "",
                "country":   "US",
            },
            "items": [{
                "productCode":    product_code,
                "startTimeLocal": start_time_local,
                "quantities":     [{"optionLabel": option_label, "value": 1}],
            }],
            "resellerReference": f"LMD-{self.slot_id[:8].upper()}",
            "comments":          "",
        }

        try:
            resp = req.post(
                f"{base_url}/bookings",
                params={"apiKey": api_key},
                json=payload,
                timeout=30,
            )
        except req.RequestException as exc:
            raise BookingTimeoutError(f"RezdyBooker: booking request failed: {exc}") from exc

        if resp.status_code == 409:
            raise BookingUnavailableError("RezdyBooker: session no longer available (409)")
        if resp.status_code == 422:
            raise BookingUnavailableError(
                f"RezdyBooker: unprocessable booking: {resp.text[:300]}"
            )
        if not resp.ok:
            raise BookingUnknownError(
                f"RezdyBooker: booking failed {resp.status_code}: {resp.text[:300]}"
            )

        data   = resp.json()
        status = data.get("requestStatus", "")
        if status != "SUCCESS":
            raise BookingUnknownError(
                f"RezdyBooker: unexpected status={status!r}: {resp.text[:300]}"
            )

        booking      = data.get("booking") or {}
        order_number = booking.get("orderNumber")
        if not order_number:
            raise BookingUnknownError(
                f"RezdyBooker: no orderNumber in response: {resp.text[:300]}"
            )

        confirmation = f"REZDY-{order_number}"
        print(f"[RezdyBooker] Confirmed: {confirmation}")

        # Save JSON artifact for debugging
        try:
            _SUCCESS_DIR.mkdir(parents=True, exist_ok=True)
            ts      = int(time.time())
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", self.slot_id)[:24]
            art     = _SUCCESS_DIR / f"confirm_{safe_id}_{ts}.json"
            art.write_text(
                json.dumps({"confirmation": confirmation, "booking": booking,
                            "raw_response": data}, indent=2),
                encoding="utf-8",
            )
            print(f"[RezdyBooker] Confirmation artifact saved: {art}")
        except Exception as exc:
            print(f"[RezdyBooker] Could not save confirmation artifact: {exc}")

        return confirmation


# ── Platform registry ─────────────────────────────────────────────────────────

PLATFORM_MAP: dict[str, type[BasePlatformBooker]] = {
    "eventbrite":   EventbriteBooker,
    "mindbody":     MindbodyBooker,
    "luma":         LumaBooker,
    "meetup":       MeetupBooker,
    "ticketmaster": GenericBooker,   # TM has very aggressive bot detection; generic best-effort
    # ── OCTO platforms (pure HTTP, OCTO standard API) ─────────────────────────
    "octo":        OCTOBooker,
    "ventrata":    OCTOBooker,
    "bokun":       OCTOBooker,
    "xola":        OCTOBooker,
    "peek":        OCTOBooker,
    "zaui":        OCTOBooker,
    "checkfront":  OCTOBooker,
    # ── Rezdy Agent API (own format, not OCTO) ────────────────────────────────
    "rezdy":       RezdyBooker,
}


# ── Public dispatcher ─────────────────────────────────────────────────────────

def complete_booking(
    slot_id: str,
    customer: dict,      # {name, email, phone}
    platform: str,       # "eventbrite", "mindbody", "luma", "meetup", etc.
    booking_url: str,    # the original booking URL from the slot
    headless: bool = True,
    timeout_ms: int = 30_000,
) -> str:
    """
    Execute the booking on the source platform.

    Args:
        slot_id:     Slot identifier (used for logging and failure screenshots)
        customer:    dict with keys: name, email, phone
        platform:    Platform name — must match a key in PLATFORM_MAP or falls back to GenericBooker
        booking_url: Direct URL to the booking/registration page
        headless:    Run browser headlessly (True for production)
        timeout_ms:  Per-action timeout in milliseconds (default 30 s)

    Returns:
        Confirmation string (order/reference number, URL, or descriptive token)

    Raises:
        BookingUnavailableError  — slot is sold out or no longer exists
        BookingAuthRequired      — platform requires account login
        BookingTimeoutError      — page did not load or action timed out
        BookingUnknownError      — unexpected failure (screenshot saved to .tmp/booking_failures/)
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("[complete_booking] WARNING: Playwright not installed — returning manual fulfillment token")
        return "Manual fulfillment required — install playwright"

    platform_key = platform.strip().lower()
    booker_cls = PLATFORM_MAP.get(platform_key, GenericBooker)

    print(f"[complete_booking] Dispatching to {booker_cls.__name__} for platform='{platform}'")

    booker = booker_cls(
        slot_id=slot_id,
        customer=customer,
        booking_url=booking_url,
        headless=headless,
        timeout_ms=timeout_ms,
    )
    return booker.run()


# ── CLI interface ─────────────────────────────────────────────────────────────

def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Complete a booking via Playwright automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/complete_booking.py --slot-id abc123 --name "Jane Smith" \\
      --email jane@example.com --phone +15550001234 \\
      --platform eventbrite --url https://www.eventbrite.com/e/...

  python tools/complete_booking.py --slot-id xyz999 --name "Bob Lee" \\
      --email bob@example.com --platform luma --url https://lu.ma/... --visible
""",
    )
    parser.add_argument("--slot-id",  required=True,  help="Slot identifier")
    parser.add_argument("--name",     required=True,  help="Customer full name")
    parser.add_argument("--email",    required=True,  help="Customer email")
    parser.add_argument("--phone",    default="",     help="Customer phone (optional)")
    parser.add_argument("--platform", required=True,  help="Platform name (eventbrite, luma, meetup, mindbody, ...)")
    parser.add_argument("--url",      required=True,  help="Booking URL")
    parser.add_argument("--visible",  action="store_true", help="Show browser window (disables headless mode)")
    parser.add_argument("--timeout",  type=int, default=30_000, help="Per-action timeout in milliseconds (default: 30000)")
    args = parser.parse_args()

    customer = {
        "name":  args.name,
        "email": args.email,
        "phone": args.phone,
    }

    try:
        confirmation = complete_booking(
            slot_id=args.slot_id,
            customer=customer,
            platform=args.platform,
            booking_url=args.url,
            headless=not args.visible,
            timeout_ms=args.timeout,
        )
        print(f"\nSUCCESS: {confirmation}")
        return 0
    except BookingUnavailableError as e:
        print(f"\nFAILED (unavailable): {e}")
        return 2
    except BookingAuthRequired as e:
        print(f"\nFAILED (auth required): {e}")
        return 3
    except BookingTimeoutError as e:
        print(f"\nFAILED (timeout): {e}")
        return 4
    except BookingUnknownError as e:
        print(f"\nFAILED (unknown): {e}")
        return 5
    except Exception as e:
        print(f"\nFAILED (unexpected): {e}")
        return 1


if __name__ == "__main__":
    sys.exit(_main())
