"""
fetch_airbnb_ical_slots.py — Find open Airbnb listing slots within 72 hours.

Two-phase approach:
  Phase 1 (seed): Scrape Airbnb search results to harvest listing IDs for each city.
                  Writes to tools/seeds/airbnb_listing_ids.json.
                  Run this phase once (or weekly to refresh the seed list).

  Phase 2 (slots): For each known listing ID, fetch its public iCal feed and
                   invert the occupied blocks to find open windows.
                   This is the phase that runs every 4 hours.

iCal feeds are publicly accessible at:
  https://www.airbnb.com/calendar/ical/{listing_id}.ics

No API key, no registration. These are intended for calendar sync.

Usage:
    # Seed listing IDs for a city (run once or weekly):
    python tools/fetch_airbnb_ical_slots.py --mode seed --max-cities 20

    # Fetch open slots from known listings (run every 4 hours):
    python tools/fetch_airbnb_ical_slots.py --mode slots --hours-ahead 72
"""

import argparse
import json
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import time
import random
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from normalize_slot import normalize, validate_schema, is_within_window

OUTPUT_FILE  = Path(".tmp/airbnb_slots.json")
SEED_FILE    = Path("tools/seeds/airbnb_listing_ids.json")
CITIES_FILE  = Path("tools/seeds/cities.json")

ICAL_URL     = "https://www.airbnb.com/calendar/ical/{listing_id}.ics"
SEARCH_URL   = "https://www.airbnb.com/s/{city}--{state}/homes"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Phase 1: Seed listing IDs ─────────────────────────────────────────────────

def harvest_listing_ids_for_city(city: dict, session: requests.Session) -> list[dict]:
    """Scrape Airbnb search results to get listing IDs for a city."""
    city_name = city["city"].replace(" ", "-")
    state     = city["state"]
    url       = SEARCH_URL.format(city=city_name, state=state)

    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        html = resp.text

        # Extract listing IDs from various URL patterns in the HTML
        patterns = [
            r'"/rooms/(\d+)"',
            r'listing_id["\s:]+(\d+)',
            r'/rooms/(\d+)\?',
            r'"id":"(\d{8,})"',
        ]

        ids = set()
        for pattern in patterns:
            matches = re.findall(pattern, html)
            for m in matches:
                if len(m) >= 7:  # Airbnb listing IDs are typically 7+ digits
                    ids.add(m)

        return [
            {
                "listing_id": lid,
                "city": city["city"],
                "state": city["state"],
                "lat": city.get("lat"),
                "lng": city.get("lng"),
            }
            for lid in ids
        ]

    except Exception as e:
        print(f"    Seed error for {city['city']}: {e}")
        return []


def seed_listings(max_cities: int = 0) -> None:
    """Phase 1: harvest listing IDs across cities and write to seed file."""
    cities = json.loads(CITIES_FILE.read_text())
    if max_cities > 0:
        cities = cities[:max_cities]

    # Load existing seeds to avoid duplicates
    existing: dict[str, dict] = {}
    if SEED_FILE.exists():
        for entry in json.loads(SEED_FILE.read_text()):
            existing[entry["listing_id"]] = entry

    session   = requests.Session()
    new_found = 0

    print(f"Seeding Airbnb listing IDs from {len(cities)} cities...")
    for i, city in enumerate(cities):
        print(f"  [{i+1}/{len(cities)}] {city['city']}, {city['state']}...", end=" ", flush=True)
        listings = harvest_listing_ids_for_city(city, session)
        added = 0
        for listing in listings:
            if listing["listing_id"] not in existing:
                existing[listing["listing_id"]] = listing
                added += 1
                new_found += 1
        print(f"{len(listings)} found, {added} new")
        time.sleep(random.uniform(1.5, 3.0))  # polite delay

    SEED_FILE.parent.mkdir(exist_ok=True)
    SEED_FILE.write_text(
        json.dumps(list(existing.values()), indent=2),
        encoding="utf-8"
    )
    print(f"\nSeed complete: {new_found} new listings added. Total: {len(existing)}")
    print(f"Seed file: {SEED_FILE}")


# ── Phase 2: Fetch open slots from iCal feeds ─────────────────────────────────

def parse_ical_busy_periods(ical_text: str) -> list[tuple[date, date]]:
    """
    Parse VEVENT blocks from iCal text.
    Returns list of (start_date, end_date) for each occupied period.
    """
    busy = []
    in_vevent = False
    dtstart = dtend = None

    for line in ical_text.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            in_vevent = True
            dtstart = dtend = None
        elif line == "END:VEVENT":
            if in_vevent and dtstart and dtend:
                busy.append((dtstart, dtend))
            in_vevent = False
        elif in_vevent:
            if line.startswith("DTSTART"):
                val = line.split(":")[-1].strip()
                try:
                    dtstart = datetime.strptime(val[:8], "%Y%m%d").date()
                except ValueError:
                    pass
            elif line.startswith("DTEND"):
                val = line.split(":")[-1].strip()
                try:
                    dtend = datetime.strptime(val[:8], "%Y%m%d").date()
                except ValueError:
                    pass

    return busy


def find_open_windows(
    busy_periods: list[tuple[date, date]],
    search_start: date,
    search_end: date,
) -> list[tuple[date, date]]:
    """
    Given a list of occupied date ranges, find open (unblocked) date ranges
    within [search_start, search_end].
    """
    # Build a set of all blocked dates
    blocked = set()
    for start, end in busy_periods:
        d = start
        while d < end:
            blocked.add(d)
            d += timedelta(days=1)

    # Find contiguous open windows
    open_windows = []
    window_start = None

    d = search_start
    while d <= search_end:
        if d not in blocked:
            if window_start is None:
                window_start = d
        else:
            if window_start is not None:
                open_windows.append((window_start, d))
                window_start = None
        d += timedelta(days=1)

    if window_start is not None:
        open_windows.append((window_start, search_end))

    return open_windows


def fetch_slots_for_listing(listing: dict, hours_ahead: int, session: requests.Session) -> list[dict]:
    """Fetch iCal for one listing and return open slot records."""
    listing_id = listing["listing_id"]
    url        = ICAL_URL.format(listing_id=listing_id)

    try:
        resp = session.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return []

        ical_text = resp.text
        if "BEGIN:VCALENDAR" not in ical_text:
            return []

        now         = datetime.now(timezone.utc)
        search_start = now.date()
        search_end   = (now + timedelta(hours=hours_ahead)).date()

        busy    = parse_ical_busy_periods(ical_text)
        windows = find_open_windows(busy, search_start, search_end)

        slots = []
        for win_start, win_end in windows:
            nights    = (win_end - win_start).days
            if nights < 1:
                continue

            start_dt  = datetime.combine(win_start, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt    = datetime.combine(win_end,   datetime.min.time()).replace(tzinfo=timezone.utc)

            raw = {
                "business_id":    f"airbnb::{listing_id}",
                "business_name":  f"Airbnb Listing in {listing.get('city', 'Unknown')}",
                "category":       "hospitality",
                "service_name":   f"{nights}-night stay available",
                "start_time":     start_dt.isoformat(),
                "end_time":       end_dt.isoformat(),
                "duration_minutes": nights * 24 * 60,
                "price":          None,   # iCal doesn't include price
                "currency":       "USD",
                "location_city":  listing.get("city", ""),
                "location_state": listing.get("state", ""),
                "location_country": "US",
                "latitude":       listing.get("lat"),
                "longitude":      listing.get("lng"),
                "booking_url":    f"https://www.airbnb.com/rooms/{listing_id}",
                "data_source":    "ical",
                "confidence":     "high",
            }

            slot = normalize(raw, "airbnb")
            if is_within_window(slot, hours_ahead):
                valid, _ = validate_schema(slot)
                if valid:
                    slots.append(slot)

        return slots

    except Exception:
        return []


def fetch_open_slots(hours_ahead: int, max_listings: int = 0) -> None:
    """Phase 2: fetch open slots from all known listing IDs."""
    if not SEED_FILE.exists():
        print(f"No seed file found at {SEED_FILE}.")
        print("Run with --mode seed first to harvest listing IDs.")
        print("Writing empty output file.")
        OUTPUT_FILE.write_text("[]", encoding="utf-8")
        return

    listings = json.loads(SEED_FILE.read_text())
    if max_listings > 0:
        listings = listings[:max_listings]

    print(f"Fetching Airbnb iCal slots from {len(listings)} listings...")

    session    = requests.Session()
    all_slots  = []
    success    = 0
    errors     = 0

    for i, listing in enumerate(listings):
        slots = fetch_slots_for_listing(listing, hours_ahead, session)
        all_slots.extend(slots)
        if slots:
            success += 1
        else:
            errors += 1

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(listings)} listings processed, {len(all_slots)} open slots so far")

        # Polite delay every 10 requests
        if i % 10 == 9:
            time.sleep(random.uniform(0.5, 1.5))

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2, default=str), encoding="utf-8")

    print(f"\nAirbnb iCal fetch complete")
    print(f"  Listings checked : {len(listings)}")
    print(f"  With open slots  : {success}")
    print(f"  Total slots      : {len(all_slots)}")
    print(f"  Output           : {OUTPUT_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Fetch open Airbnb slots via iCal")
    parser.add_argument("--mode", choices=["seed", "slots"], default="slots",
                        help="'seed' = harvest listing IDs; 'slots' = fetch open windows")
    parser.add_argument("--hours-ahead", type=int, default=72)
    parser.add_argument("--max-cities",   type=int, default=0, help="For seed mode")
    parser.add_argument("--max-listings", type=int, default=0, help="For slots mode")
    args = parser.parse_args()

    if args.mode == "seed":
        seed_listings(args.max_cities)
    else:
        fetch_open_slots(args.hours_ahead, args.max_listings)


if __name__ == "__main__":
    main()
