"""
fetch_mindbody_slots.py — Scrape upcoming class slots from Mindbody classic client pages.

How it works:
  - Mindbody blocks the requests library (requires real browser)
  - Uses Playwright to load https://clients.mindbodyonline.com/classic/mainclass?studioid={SITE_ID}
  - Parses the server-rendered HTML schedule table
  - Runs multiple studios in one Playwright browser session for efficiency

Studio discovery:
  - Uses a seed list of confirmed active Mindbody studios across major US cities
  - Seed list can be expanded via tools/seeds/mindbody_studios.json

Usage:
    python tools/fetch_mindbody_slots.py [--hours-ahead 72] [--max-studios 20]
"""

import argparse
import hashlib
import json
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "beautifulsoup4"], capture_output=True)
    from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright

# ── Paths ─────────────────────────────────────────────────────────────────────

OUTPUT_FILE  = Path(".tmp/mindbody_slots.json")
STUDIOS_SEED = Path("tools/seeds/mindbody_studios.json")

# ── Known active studios (fallback if seed file absent) ──────────────────────
# These are confirmed active Mindbody sites from the classic client.
# site_id is the studioid parameter in the URL.

BUILTIN_STUDIOS = [
    # New York
    {"site_id": "18692",  "city": "New York",      "state": "NY", "name": "Stellar Bodies"},
    {"site_id": "152065", "city": "New York",       "state": "NY", "name": "NY Pilates"},
    {"site_id": "77073",  "city": "New York",       "state": "NY", "name": "CorePower Yoga NYC"},
    {"site_id": "3539",   "city": "New York",       "state": "NY", "name": "Pure Yoga NYC"},
    # Los Angeles
    {"site_id": "5000",   "city": "Los Angeles",   "state": "CA", "name": ""},
    {"site_id": "18063",  "city": "Los Angeles",   "state": "CA", "name": ""},
    # Chicago
    {"site_id": "42579",  "city": "Chicago",        "state": "IL", "name": ""},
    # San Francisco
    {"site_id": "20380",  "city": "San Francisco",  "state": "CA", "name": ""},
    # Boston
    {"site_id": "8952",   "city": "Boston",         "state": "MA", "name": ""},
    # Austin
    {"site_id": "32451",  "city": "Austin",         "state": "TX", "name": ""},
    # Miami
    {"site_id": "24680",  "city": "Miami",          "state": "FL", "name": ""},
    # Seattle
    {"site_id": "11876",  "city": "Seattle",        "state": "WA", "name": ""},
    # Denver
    {"site_id": "33215",  "city": "Denver",         "state": "CO", "name": ""},
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ── HTML Parser ────────────────────────────────────────────────────────────────

def _parse_time_str(time_str: str, date: datetime) -> datetime | None:
    """Parse '8:00 am EDT' or '10:30pm' into a datetime on the given date."""
    # Normalize whitespace and case
    raw = time_str.strip().replace("\xa0", " ").upper()
    # Strip timezone abbreviations (EDT, EST, CDT, CST, MDT, MST, PDT, PST, etc.)
    # These end with T and are 2-5 letters. Must be done BEFORE removing spaces to
    # avoid accidentally stripping 'M' from 'AM'/'PM'.
    raw = re.sub(r'\s+[A-Z]{2,5}T\b', '', raw).strip()
    # Remove remaining spaces
    raw = raw.replace(" ", "")
    for fmt in ["%I:%M%p", "%I%p"]:
        try:
            t = datetime.strptime(raw, fmt)
            return date.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            pass
    return None


def _parse_date_header(text: str) -> datetime | None:
    """Parse 'Sun March 22, 2026' → datetime."""
    # Strip weekday prefix
    text = re.sub(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*[\s,]+', '', text).strip()
    for fmt in ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def parse_schedule(html: str, studio: dict, hours_ahead: int, now_utc: datetime) -> list[dict]:
    """
    Parse class schedule from Mindbody classic client HTML.

    Structure discovered via inspection:
      div.classSchedule-mainTable-loaded
        div.header  → date string e.g. "Sun March 22, 2026"
        div.evenRow.row / div.oddRow.row
          div.col-1
            div.col.col-first  → time "8:00 am EDT"
          div.col-2
            div.col [0]        → class name (contains <a class="modalClassDesc">)
            div.col [1]        → teacher name
            div.col [2]        → location (contains <a class="modalLocationInfo">)
            div.col [3]        → duration "50 minutes"

    Returns normalized slot dicts for slots within the hours_ahead window.
    """
    soup  = BeautifulSoup(html, "html.parser")
    slots = []
    site_id      = studio["site_id"]
    city         = studio.get("city", "")
    state        = studio.get("state", "")
    studio_name  = studio.get("name") or f"Mindbody Studio {site_id}"
    scraped_at   = now_utc.isoformat()

    # Find the schedule container
    sched = soup.find("div", class_="classSchedule-mainTable-loaded")
    if not sched:
        return slots

    current_date: datetime | None = None

    for child in sched.children:
        import bs4
        if not hasattr(child, "get"):
            continue
        classes = child.get("class", [])

        # Date header
        if "header" in classes:
            header_text = child.get_text(" ", strip=True)
            # Strip weekday prefix: "Sun March 22, 2026" or "Sunday, March 22, 2026"
            date_part = re.sub(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*[\s,]+', '', header_text).strip()
            parsed = _parse_date_header(date_part)
            if parsed:
                current_date = parsed
            continue

        # Class row
        if "row" in classes and current_date is not None:
            col1 = child.find("div", class_="col-1")
            col2 = child.find("div", class_="col-2")
            if not col1 or not col2:
                continue

            # Time from col-1 first div
            time_div = col1.find("div", class_="col-first")
            if not time_div:
                continue
            # Replace non-breaking space, strip
            time_str = time_div.get_text(" ", strip=True).replace("\xa0", " ").strip()

            # Booking URL from signup button onclick:
            # onclick="...promptLogin('', 'Name...', '/ASP/res_a.asp?tg=22&classId=X&classDate=Y&clsLoc=Z');"
            booking_url_path = ""
            signup_btn = col1.find("input", class_="SignupButton")
            if signup_btn:
                onclick = signup_btn.get("onclick", "")
                # Extract the URL from the third argument of promptLogin(...)
                url_match = re.search(r"promptLogin\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'([^']+)'", onclick)
                if url_match:
                    booking_url_path = url_match.group(1).replace("&amp;", "&")

            # Availability from .tablet-viewable: "(N Reserved, M Open)"
            spots_open = None
            spots_total = None
            avail_div = col1.find("div", class_="tablet-viewable")
            if avail_div:
                avail_text = avail_div.get_text(strip=True)
                reserved_m = re.search(r'(\d+)\s+Reserved', avail_text)
                open_m     = re.search(r'(\d+)\s+Open', avail_text)
                if reserved_m and open_m:
                    spots_open  = int(open_m.group(1))
                    spots_total = int(reserved_m.group(1)) + spots_open

            # Class data from col-2
            col2_divs = col2.find_all("div", class_="col")
            class_name   = col2_divs[0].get_text(strip=True) if len(col2_divs) > 0 else ""
            teacher      = col2_divs[1].get_text(strip=True) if len(col2_divs) > 1 else ""
            location     = col2_divs[2].get_text(strip=True) if len(col2_divs) > 2 else ""
            duration_str = col2_divs[3].get_text(strip=True) if len(col2_divs) > 3 else ""

            if not time_str or not class_name or len(class_name) < 3:
                continue

            start_local = _parse_time_str(time_str, current_date)
            if start_local is None:
                continue

            # Treat as UTC (Mindbody returns local times)
            start_utc   = start_local.replace(tzinfo=timezone.utc)
            hours_until = (start_utc - now_utc).total_seconds() / 3600

            # Only collect slots in the window [0, hours_ahead]
            if hours_until < -0.5 or hours_until > hours_ahead:
                continue

            # Duration
            duration_minutes: int | None = None
            dur_m = re.search(r'(\d+)\s*min', duration_str, re.IGNORECASE)
            if dur_m:
                duration_minutes = int(dur_m.group(1))

            end_iso = ""
            if duration_minutes:
                end_iso = (start_utc + timedelta(minutes=duration_minutes)).isoformat()

            # Deterministic slot_id
            slot_id = "mb_" + hashlib.sha256(
                f"mindbody_{site_id}_{class_name}_{start_utc.isoformat()}".encode()
            ).hexdigest()[:16]

            slots.append({
                "slot_id":          slot_id,
                "platform":         "mindbody",
                "business_id":      site_id,
                "business_name":    studio_name,
                "category":         "wellness",
                "service_name":     class_name,
                "start_time":       start_utc.isoformat(),
                "end_time":         end_iso,
                "duration_minutes": duration_minutes,
                "price":            None,
                "currency":         "USD",
                "original_price":   None,
                "location_city":    city,
                "location_state":   state,
                "location_country": "US",
                "latitude":         None,
                "longitude":        None,
                "hours_until_start": round(hours_until, 2),
                "booking_url":      (
                    f"https://clients.mindbodyonline.com{booking_url_path}"
                    if booking_url_path
                    else f"https://clients.mindbodyonline.com/classic/mainclass?studioid={site_id}"
                ),
                "spots_open":       spots_open,
                "spots_total":      spots_total,
                "scraped_at":       scraped_at,
                "data_source":      "scrape",
                "confidence":       "high" if spots_open is not None else "medium",
                "teacher":          teacher,
                "location_room":    location,
            })

    return slots


# ── Playwright runner ──────────────────────────────────────────────────────────

def fetch_all_studios(studios: list[dict], hours_ahead: int) -> list[dict]:
    """
    Load multiple Mindbody studio pages in a single Playwright browser session.
    Returns all slots across all studios.
    """
    now_utc   = datetime.now(timezone.utc)
    all_slots = []
    errors    = []

    # We need to fetch the CURRENT week + NEXT week to cover 72h
    # The page default loads the current week; we'll also need next page for slots
    # that start >24h from now but still <=72h.
    # Strategy: load the page, parse current week. If hours_ahead > 48, also POST
    # to navigate one week forward and parse that too.

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        for studio in studios:
            site_id     = studio["site_id"]
            studio_name = studio.get("name") or f"Studio {site_id}"
            city        = studio.get("city", "")
            url         = f"https://clients.mindbodyonline.com/classic/mainclass?studioid={site_id}"

            print(f"  [{site_id}] {studio_name} ({city})...", end=" ", flush=True)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                time.sleep(4)  # Wait for JS + auto-POST to complete

                html     = page.content()
                slots    = parse_schedule(html, studio, hours_ahead, now_utc)

                # Always navigate to next week to cover the full 72h window
                try:
                    # Click the week-forward arrow (id="week-arrow-r")
                    next_btn = page.query_selector("#week-arrow-r, td.date-arrow-r[id='week-arrow-r']")
                    if next_btn:
                        next_btn.click()
                        time.sleep(4)
                        html2  = page.content()
                        slots2 = parse_schedule(html2, studio, hours_ahead, now_utc)
                        slots += slots2
                    else:
                        # Fallback: look for any right-arrow in week navigation
                        all_arrows = page.query_selector_all("td.date-arrow-r")
                        if len(all_arrows) >= 2:
                            all_arrows[-1].click()  # week arrow is the second one
                            time.sleep(4)
                            html2  = page.content()
                            slots2 = parse_schedule(html2, studio, hours_ahead, now_utc)
                            slots += slots2
                except Exception as nav_exc:
                    pass  # Week navigation failed; proceed with current week slots

                print(f"{len(slots)} slots")
                all_slots.extend(slots)

            except Exception as exc:
                print(f"ERROR: {exc}")
                errors.append({"site_id": site_id, "error": str(exc)})

        browser.close()

    if errors:
        print(f"\n  Errors: {len(errors)}")
        for e in errors:
            print(f"    {e['site_id']}: {e['error']}")

    return all_slots


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Mindbody slots via HTML scraping")
    parser.add_argument("--hours-ahead",  type=int,   default=72)
    parser.add_argument("--max-studios",  type=int,   default=0,  help="0 = all")
    parser.add_argument("--output",       default=str(OUTPUT_FILE))
    args = parser.parse_args()

    Path(".tmp").mkdir(exist_ok=True)

    # Load studios
    if STUDIOS_SEED.exists():
        studios = json.loads(STUDIOS_SEED.read_text())
        print(f"Loaded {len(studios)} studios from {STUDIOS_SEED}")
    else:
        studios = BUILTIN_STUDIOS
        print(f"Using {len(studios)} built-in studios (no seed file at {STUDIOS_SEED})")

    if args.max_studios > 0:
        studios = studios[:args.max_studios]

    print(f"Fetching Mindbody slots | {len(studios)} studios | {args.hours_ahead}h window\n")

    all_slots = fetch_all_studios(studios, args.hours_ahead)

    # Deduplicate
    seen: dict[str, dict] = {}
    for s in all_slots:
        sid = s.get("slot_id")
        if sid and sid not in seen:
            seen[sid] = s
    deduped = list(seen.values())

    out = Path(args.output)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(deduped, indent=2, default=str), encoding="utf-8")

    print(f"\n{'─' * 50}")
    print(f"Mindbody fetch complete")
    print(f"  Studios scraped : {len(studios)}")
    print(f"  Total slots     : {len(all_slots)}")
    print(f"  After dedup     : {len(deduped)}")
    print(f"  Output          : {out}")

    if deduped:
        print(f"\nSample slots:")
        for s in deduped[:5]:
            print(f"  {s['hours_until_start']:+.1f}h | {s['service_name']} @ {s['business_name']} ({s['location_city']})")


if __name__ == "__main__":
    main()
