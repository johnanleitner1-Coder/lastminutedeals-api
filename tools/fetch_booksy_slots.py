"""
fetch_booksy_slots.py — Fetch last-minute appointment slots from Booksy.

Booksy is a beauty/wellness booking platform. Their public search API is
accessible without authentication. We search by city and scrape available
slots within the time window.

API used:
  GET https://booksy.com/api/us/v2/business/search
  GET https://us.booksy.com/api/us/v2/business/{id}/staff_members_slots

Usage:
    python tools/fetch_booksy_slots.py [--hours-ahead 72] [--max-cities 30]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")
from normalize_slot import compute_slot_id, compute_hours_until

OUTPUT_FILE = Path(".tmp/booksy_slots.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://booksy.com",
    "Referer": "https://booksy.com/",
    "x-app-id": "1",
    "x-app-platform": "web",
}

# Top US cities for beauty/salon searches
BOOKSY_CITIES = [
    ("New York",       "NY",  40.7128, -74.0060),
    ("Los Angeles",    "CA",  34.0522, -118.2437),
    ("Chicago",        "IL",  41.8781, -87.6298),
    ("Houston",        "TX",  29.7604, -95.3698),
    ("Phoenix",        "AZ",  33.4484, -112.0740),
    ("Philadelphia",   "PA",  39.9526, -75.1652),
    ("San Antonio",    "TX",  29.4241, -98.4936),
    ("San Diego",      "CA",  32.7157, -117.1611),
    ("Dallas",         "TX",  32.7767, -96.7970),
    ("San Jose",       "CA",  37.3382, -121.8863),
    ("Austin",         "TX",  30.2672, -97.7431),
    ("Jacksonville",   "FL",  30.3322, -81.6557),
    ("Fort Worth",     "TX",  32.7555, -97.3308),
    ("Columbus",       "OH",  39.9612, -82.9988),
    ("Charlotte",      "NC",  35.2271, -80.8431),
    ("Indianapolis",   "IN",  39.7684, -86.1581),
    ("San Francisco",  "CA",  37.7749, -122.4194),
    ("Seattle",        "WA",  47.6062, -122.3321),
    ("Denver",         "CO",  39.7392, -104.9903),
    ("Nashville",      "TN",  36.1627, -86.7816),
    ("Oklahoma City",  "OK",  35.4676, -97.5164),
    ("El Paso",        "TX",  31.7619, -106.4850),
    ("Washington",     "DC",  38.9072, -77.0369),
    ("Las Vegas",      "NV",  36.1699, -115.1398),
    ("Louisville",     "KY",  38.2527, -85.7585),
    ("Memphis",        "TN",  35.1495, -90.0490),
    ("Portland",       "OR",  45.5051, -122.6750),
    ("Oklahoma City",  "OK",  35.4676, -97.5164),
    ("Baltimore",      "MD",  39.2904, -76.6122),
    ("Milwaukee",      "WI",  43.0389, -87.9065),
]

# Booksy service categories to search
BOOKSY_CATEGORIES = [
    ("Hair", "beauty"),
    ("Nails", "beauty"),
    ("Massage", "wellness"),
    ("Skin Care", "beauty"),
    ("Eyebrows & Lashes", "beauty"),
    ("Barbershop", "beauty"),
    ("Makeup", "beauty"),
    ("Spa", "wellness"),
]


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def search_businesses(city: str, lat: float, lng: float, category: str, session: requests.Session) -> list[dict]:
    """Search Booksy for businesses in a city by category."""
    url = "https://us.booksy.com/api/us/v2/business/search"
    params = {
        "q":          category,
        "lat":        lat,
        "lon":        lng,
        "radius":     15,        # km radius
        "page":       1,
        "per_page":   20,
        "popularity": "true",
    }
    try:
        r = session.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("businesses", []) or []
    except Exception:
        return []


def fetch_business_slots(
    biz_id: int,
    biz_name: str,
    category: str,
    cat_label: str,
    city: str,
    state: str,
    hours_ahead: int,
    session: requests.Session,
) -> list[dict]:
    """Fetch available appointment slots for a business."""
    now_utc = datetime.now(timezone.utc)
    slots = []

    # Check slots for next 3 days (covers 72h window)
    for day_offset in range(3):
        date = (now_utc + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        url = f"https://us.booksy.com/api/us/v2/business/{biz_id}/staff_members_slots"
        params = {"date": date}
        try:
            r = session.get(url, params=params, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue

        # data structure: {"slots": [{"staff_members": [{"slots": [...]}]}]}
        all_staff_slots = data.get("slots") or []
        for staff_entry in all_staff_slots:
            staff_name = staff_entry.get("staff_member", {}).get("name", "")
            service_slots = staff_entry.get("slots") or []
            for slot_entry in service_slots:
                start_at = slot_entry.get("start_time") or slot_entry.get("datetime") or ""
                if not start_at:
                    continue
                # Ensure ISO format with timezone
                if "T" not in start_at:
                    start_at = f"{date}T{start_at}:00"
                if not start_at.endswith("Z") and "+" not in start_at[-6:]:
                    start_at += "Z"

                hours = compute_hours_until(start_at)
                if hours is None or hours < 0 or hours > hours_ahead:
                    continue

                # Service name
                service = slot_entry.get("service_name") or slot_entry.get("service") or category
                duration = slot_entry.get("duration") or slot_entry.get("duration_minutes")
                price = slot_entry.get("price") or slot_entry.get("price_from")
                if price is not None:
                    try:
                        price = float(price)
                    except (TypeError, ValueError):
                        price = None

                slot_id = compute_slot_id("booksy", str(biz_id), start_at)
                booking_url = f"https://booksy.com/us-en/biz/{biz_id}"

                slots.append({
                    "slot_id":          slot_id,
                    "platform":         "booksy",
                    "business_id":      str(biz_id),
                    "business_name":    biz_name,
                    "category":         cat_label,
                    "service_name":     service,
                    "start_time":       start_at,
                    "end_time":         "",
                    "duration_minutes": duration,
                    "hours_until_start": hours,
                    "price":            price,
                    "currency":         "USD",
                    "original_price":   None,
                    "location_city":    city,
                    "location_state":   state,
                    "location_country": "US",
                    "latitude":         None,
                    "longitude":        None,
                    "booking_url":      booking_url,
                    "scraped_at":       datetime.now(timezone.utc).isoformat(),
                    "data_source":      "api",
                    "confidence":       "high",
                })

    return slots


def main():
    parser = argparse.ArgumentParser(description="Fetch Booksy last-minute beauty/wellness slots")
    parser.add_argument("--hours-ahead", type=int, default=72)
    parser.add_argument("--max-cities",  type=int, default=30)
    parser.add_argument("--delay",       type=float, default=0.5)
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    session   = _make_session()
    all_slots: list[dict] = []
    seen_ids:  set[str]   = set()

    cities = BOOKSY_CITIES[:args.max_cities]

    for i, (city, state, lat, lng) in enumerate(cities, 1):
        city_new = 0
        for service_name, service_cat in BOOKSY_CATEGORIES[:4]:   # top 4 categories per city
            businesses = search_businesses(city, lat, lng, service_name, session)
            for biz in businesses[:5]:  # top 5 businesses per category
                biz_id   = biz.get("id")
                biz_name = biz.get("name") or ""
                if not biz_id:
                    continue
                biz_slots = fetch_business_slots(
                    biz_id, biz_name, service_name, service_cat,
                    city, state, args.hours_ahead, session,
                )
                for s in biz_slots:
                    if s["slot_id"] not in seen_ids:
                        seen_ids.add(s["slot_id"])
                        all_slots.append(s)
                        city_new += 1
                time.sleep(args.delay)

        print(f"  [{i}/{len(cities)}] {city}, {state}... {city_new} slots")

    priced = sum(1 for s in all_slots if s.get("price") is not None and s["price"] > 0)
    print(f"\nBooksy total: {len(all_slots)} slots ({priced} with prices)")

    if not args.dry_run:
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2, default=str), encoding="utf-8")
        print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
