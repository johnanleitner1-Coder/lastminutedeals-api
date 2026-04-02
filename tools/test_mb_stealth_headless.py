"""
Attempt 1: Bypass Mindbody headless detection with full stealth.

Mindbody's signin.mindbodyonline.com OIDC page detects headless Chrome and refuses
to render the email input form. This tests whether stealth + anti-detection flags
can fool it into thinking it's a real browser.

Signals Mindbody likely checks:
  - navigator.webdriver  (set to true in headless)
  - window.chrome        (missing in headless)
  - navigator.plugins    (empty in headless)
  - navigator.languages  (may differ)
  - CDP leak via error stack traces
"""

import time
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from playwright.sync_api import sync_playwright

Path(".tmp").mkdir(exist_ok=True)

TS = int(time.time())
TEST_EMAIL = f"mbtest{TS}@mailinator.com"

# Anti-detection JS injected before any page script runs
STEALTH_SCRIPT = """
// Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Fake plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {0: {type:'application/x-google-chrome-pdf', suffixes:'pdf', description:'Portable Document Format', enabledPlugin: Plugin}, description:'Chrome PDF Plugin', filename:'internal-pdf-viewer', length:1, name:'Chrome PDF Plugin'},
        {0: {type:'application/pdf', suffixes:'pdf', description:'', enabledPlugin: Plugin}, description:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', length:1, name:'Chrome PDF Viewer'},
        {0: {type:'application/x-nacl', suffixes:'', description:'Native Client Executable', enabledPlugin: Plugin}, description:'Native Client', filename:'internal-nacl-plugin', length:1, name:'Native Client'},
    ]
});

// Fake languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// Add window.chrome
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

// Fix iframe contentWindow
const origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    get: function() {
        const win = origContentWindow.get.call(this);
        if (win) {
            Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined });
        }
        return win;
    }
});

// Permissions API
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
"""


def main():
    print(f"Stealth headless test — email: {TEST_EMAIL}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--window-size=1280,900",
            ]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            permissions=["geolocation"],
            java_script_enabled=True,
            has_touch=False,
            is_mobile=False,
        )

        # Inject stealth script on every new page/frame
        ctx.add_init_script(STEALTH_SCRIPT)

        # Also try playwright_stealth if available
        page = ctx.new_page()
        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(page)
            print("playwright_stealth applied")
        except Exception as e:
            print(f"playwright_stealth unavailable: {e}")

        # ── Test 1: Can we see the OIDC form? ─────────────────────────
        print("\n[Test 1] Loading schedule + clicking Create Account...")
        page.goto("https://clients.mindbodyonline.com/classic/mainclass?studioid=18692",
                  wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        btn = page.query_selector("a:has-text('Create account')")
        if btn:
            btn.click()
            time.sleep(6)

        text = page.evaluate("() => document.body.innerText")
        print(f"Page text after click:\n{text[:400]}")

        # Check for email input
        email_inp = page.query_selector("input[type='email']")
        any_visible_input = page.query_selector("input:not([type='hidden']):not([type='button']):not([type='checkbox'])")

        print(f"\nemail input[type=email] found: {email_inp is not None}")
        print(f"any non-hidden input found: {any_visible_input is not None}")

        if any_visible_input:
            ph = any_visible_input.get_attribute("placeholder") or ""
            tp = any_visible_input.get_attribute("type") or ""
            print(f"Input: type={tp!r} placeholder={ph!r} visible={any_visible_input.is_visible()}")

        page.screenshot(path=".tmp/stealth_test1.png")

        # ── Test 2: Probe navigator.webdriver directly ─────────────────
        webdriver_val = page.evaluate("() => navigator.webdriver")
        chrome_val = page.evaluate("() => typeof window.chrome")
        plugins_len = page.evaluate("() => navigator.plugins.length")
        print(f"\nNavigator checks:")
        print(f"  navigator.webdriver = {webdriver_val}")
        print(f"  typeof window.chrome = {chrome_val}")
        print(f"  navigator.plugins.length = {plugins_len}")

        # ── Test 3: Navigate directly to signin.mindbodyonline.com ─────
        # Get the actual OIDC URL from the page source
        print("\n[Test 3] Looking for OIDC URL in page source...")
        html = page.content()
        import re
        oidc_urls = re.findall(r'https://signin\.mindbodyonline\.com/signin[^\s"\']+', html)
        print(f"OIDC URLs found in source: {len(oidc_urls)}")
        for u in oidc_urls[:2]:
            print(f"  {u[:120]}")

        if oidc_urls:
            # Navigate directly to the OIDC URL
            oidc_url = oidc_urls[0].replace("&amp;", "&")
            print(f"\n[Test 4] Navigating directly to OIDC URL...")
            page.goto(oidc_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)

            oidc_text = page.evaluate("() => document.body.innerText")
            oidc_url_now = page.url
            print(f"OIDC URL: {oidc_url_now[:80]}")
            print(f"OIDC page text:\n{oidc_text[:500]}")

            oidc_email = page.query_selector("input[type='email']")
            print(f"Email input on OIDC page: {oidc_email is not None}")
            if oidc_email:
                print("*** SUCCESS: Email input visible in headless mode! ***")
                oidc_email.fill(TEST_EMAIL)
                time.sleep(1)

                cont_btn = page.query_selector("button[type='submit']")
                if cont_btn:
                    cont_btn.click()
                    time.sleep(4)
                    next_text = page.evaluate("() => document.body.innerText")
                    print(f"After email submit:\n{next_text[:600]}")
                    page.screenshot(path=".tmp/stealth_oidc_after_email.png")
            else:
                page.screenshot(path=".tmp/stealth_oidc_page.png")
                print("Email input NOT found — OIDC still blocking headless")

        browser.close()


if __name__ == "__main__":
    main()
