"""
parse_mindbody_html.py — Parse Mindbody classic client HTML schedule pages.

Architecture discovered:
- URL: https://clients.mindbodyonline.com/classic/mainclass?studioid={SITE_ID}
- Schedule is server-side rendered HTML (NOT a JSON API)
- Date navigation uses POST with date parameter
- Week view available
- HTML contains class rows with: time, name, teacher, location, duration

This scraper:
1. Loads the schedule page for a given studio
2. Parses the HTML table
3. Returns normalized slot records
"""

import json
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

# Require requests + bs4
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing dependencies...")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "requests", "beautifulsoup4"], capture_output=True)
    import requests
    from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def fetch_schedule_html(site_id: str, date: str = "", view: str = "week") -> str:
    """
    Fetch schedule HTML for a given site.
    date format: "3/28/2026"
    view: "day" or "week"
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    base_url = f"https://clients.mindbodyonline.com/classic/mainclass?studioid={site_id}"

    # First GET to establish session cookies
    resp = session.get(base_url, timeout=30)
    resp.raise_for_status()

    if not date:
        # Use today
        date = datetime.now().strftime("%-m/%-d/%Y").replace("-", "/")
        # Windows-compatible: no %-
        now = datetime.now()
        date = f"{now.month}/{now.day}/{now.year}"

    # POST to navigate to specific date
    post_url = f"https://clients.mindbodyonline.com/classic/home?studioid={site_id}"
    post_data = {
        "tg": "",
        "vt": "",
        "lvl": "",
        "stype": "",
        "qParam": "",
        "view": view,
        "trn": "0",
        "page": "",
        "catid": "",
        "prodid": "",
        "prodGroupId": "",
        "date": date,
        "classid": "0",
        "sSU": "",
        "optForwardingLink": "",
        "singleUseToken": "",
        "oAuthToken": "",
        "launchGUID": "",
        "launchUID": "",
        "launchPWDChange": "",
    }

    post_headers = {
        **HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://clients.mindbodyonline.com",
        "Referer": base_url,
    }
    session.headers.update(post_headers)

    resp = session.post(post_url, data=post_data, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_schedule_html(html: str, site_id: str, city: str = "", studio_name: str = "") -> list[dict]:
    """Parse class schedule rows from Mindbody classic HTML."""
    soup = BeautifulSoup(html, "html.parser")
    slots = []

    # The schedule renders as rows with class info
    # Look for the main class table
    # Class rows typically have: time, class name, teacher, location, duration

    # Method 1: Look for table rows with class schedule data
    # Mindbody uses specific CSS classes for rows

    # Find the schedule table - look for rows with time patterns
    now = datetime.now(timezone.utc)
    scraped_at = now.isoformat()

    # Try to find class rows by looking for time patterns in table cells
    rows = soup.find_all("tr")

    current_date_str = ""
    slots_found = 0

    for row in rows:
        cells = row.find_all("td")
        if not cells:
            continue

        row_text = row.get_text(" ", strip=True)

        # Date header rows (e.g., "Sun March 22, 2026")
        date_match = re.search(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+\w+\s+\d+,\s+\d{4}', row_text)
        if date_match:
            current_date_str = date_match.group(0)
            continue

        # Class rows have time like "8:00 am EDT"
        time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', row_text, re.IGNORECASE)
        if not time_match or not current_date_str:
            continue

        # Parse the cells
        # Typical order: time | class name | teacher | location | duration
        cell_texts = [c.get_text(" ", strip=True) for c in cells]

        if len(cell_texts) < 3:
            continue

        # Find time cell
        time_str = ""
        class_name = ""
        teacher = ""
        location = ""
        duration_str = ""

        for i, ct in enumerate(cell_texts):
            t_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', ct, re.IGNORECASE)
            if t_match and not time_str:
                time_str = t_match.group(1)
                # Remaining cells follow
                remaining = cell_texts[i+1:]
                if remaining:
                    class_name = remaining[0] if len(remaining) > 0 else ""
                    teacher = remaining[1] if len(remaining) > 1 else ""
                    location = remaining[2] if len(remaining) > 2 else ""
                    duration_str = remaining[3] if len(remaining) > 3 else ""
                break

        if not time_str or not class_name:
            continue

        # Skip if class_name looks like junk
        if len(class_name) < 3 or class_name.isdigit():
            continue

        # Parse start time
        try:
            date_clean = re.sub(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+', '', current_date_str)
            start_dt = datetime.strptime(f"{date_clean} {time_str.upper().replace(' ', '')}", "%B %d, %Y %I:%M%p")
            start_iso = start_dt.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            start_iso = f"{current_date_str} {time_str}"

        # Parse duration
        duration_minutes = None
        dur_match = re.search(r'(\d+)\s*min', duration_str, re.IGNORECASE)
        if dur_match:
            duration_minutes = int(dur_match.group(1))

        # Compute hours_until_start
        try:
            start_naive = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            hours_until = (start_naive - now).total_seconds() / 3600
        except Exception:
            hours_until = 999

        # Generate slot_id
        slot_id_raw = f"mindbody_{site_id}_{class_name}_{start_iso}"
        slot_id = "mb_" + hashlib.sha256(slot_id_raw.encode()).hexdigest()[:16]

        slot = {
            "slot_id": slot_id,
            "platform": "mindbody",
            "business_id": site_id,
            "business_name": studio_name or f"Mindbody Studio {site_id}",
            "category": "wellness",
            "service_name": class_name,
            "start_time": start_iso,
            "end_time": "",
            "duration_minutes": duration_minutes,
            "price": None,
            "currency": "USD",
            "location_city": city,
            "location_state": "",
            "location_country": "US",
            "hours_until_start": round(hours_until, 2),
            "booking_url": f"https://clients.mindbodyonline.com/classic/mainclass?studioid={site_id}",
            "scraped_at": scraped_at,
            "data_source": "scrape",
            "confidence": "medium",
            "teacher": teacher,
            "location_room": location,
        }

        slots.append(slot)
        slots_found += 1

    print(f"  Parsed {slots_found} class slots from HTML")
    return slots


def scrape_studio(site_id: str, city: str = "", studio_name: str = "") -> list[dict]:
    """Scrape a single studio's schedule."""
    now = datetime.now()
    date_str = f"{now.month}/{now.day}/{now.year}"

    print(f"\nFetching schedule for site {site_id} ({studio_name or 'unknown'}) - date {date_str}")

    html = fetch_schedule_html(site_id, date=date_str, view="week")
    print(f"  Got {len(html)} bytes of HTML")

    return parse_schedule_html(html, site_id, city, studio_name)


# Known Mindbody studios to test with
TEST_STUDIOS = [
    ("18692", "New York", "Stellar Bodies"),
    ("152065", "New York", "NY Pilates"),
    ("5000", "Los Angeles", ""),
    ("42579", "Chicago", ""),
]


def main():
    import os
    os.makedirs(".tmp", exist_ok=True)

    all_slots = []

    for site_id, city, name in TEST_STUDIOS[:2]:  # Test first 2
        try:
            slots = scrape_studio(site_id, city, name)
            all_slots.extend(slots)

            if slots:
                print(f"\n  Sample slots from {name or site_id}:")
                for s in slots[:3]:
                    print(f"    {s['start_time'][:16]} | {s['service_name']} | {s['teacher']}")
        except Exception as e:
            print(f"  Error for {site_id}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n\nTotal slots: {len(all_slots)}")

    # Filter to <=72h
    future_slots = [s for s in all_slots if 0 <= s.get("hours_until_start", 999) <= 72]
    print(f"Slots within 72h: {len(future_slots)}")

    with open(".tmp/mindbody_html_slots.json", "w", encoding="utf-8") as f:
        json.dump(all_slots, f, indent=2)
    print("Saved to .tmp/mindbody_html_slots.json")

    if future_slots:
        print("\nUpcoming slots (<=72h):")
        for s in future_slots[:10]:
            print(f"  {s['hours_until_start']:.1f}h | {s['service_name']} @ {s['business_name']} | {s['start_time'][:16]}")


if __name__ == "__main__":
    main()
