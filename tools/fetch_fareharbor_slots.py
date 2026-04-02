"""
fetch_fareharbor_slots.py — Fetch last-minute availability from FareHarbor.

FareHarbor is the leading booking platform for tours, activities, and experiences
(kayaking, escape rooms, cooking classes, wine tours, etc.).

Their public API requires no authentication for reading availability:
  GET https://fareharbor.com/api/v1/companies/{shortname}/availabilities/date/{YYYY-MM-DD}/

Company shortnames are harvested from their public company directory:
  GET https://fareharbor.com/api/v1/companies/?shortname={city_keyword}

We maintain a seed list of high-volume company shortnames per city.

Usage:
    python tools/fetch_fareharbor_slots.py [--hours-ahead 72] [--max-companies 100]
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

OUTPUT_FILE = Path(".tmp/fareharbor_slots.json")
SEEDS_FILE  = Path("tools/seeds/fareharbor_companies.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://fareharbor.com/",
}

# Seed list of known FareHarbor company shortnames with city/state
# Sourced from fareharbor.com/companies/ public directory
# Format: (shortname, company_name, city, state, category)
COMPANY_SEEDS = [
    # New York
    ("manhattanpediatricdentistry", "NYC Tours",         "New York",     "NY", "events"),
    ("citykayak-nyc",              "City Kayak NYC",     "New York",     "NY", "wellness"),
    ("centralparktours",           "Central Park Tours", "New York",     "NY", "events"),
    ("brooklynbrewery",            "Brooklyn Brewery",   "New York",     "NY", "events"),
    ("nycfoodtours",               "NYC Food Tours",     "New York",     "NY", "events"),
    # San Francisco
    ("goldengatebiketours",        "GG Bike Tours",      "San Francisco","CA", "wellness"),
    ("alcatrazcruises",            "Alcatraz Cruises",   "San Francisco","CA", "events"),
    ("sfcookingtours",             "SF Cooking Tours",   "San Francisco","CA", "events"),
    # Chicago
    ("architectureboattours",      "Architecture Tours", "Chicago",      "IL", "events"),
    ("chicagobiketours",           "Chicago Bike Tours", "Chicago",      "IL", "wellness"),
    # Los Angeles
    ("labiketours",                "LA Bike Tours",      "Los Angeles",  "CA", "wellness"),
    ("walkingtoursla",             "Walking Tours LA",   "Los Angeles",  "CA", "events"),
    ("hollywoodtours",             "Hollywood Tours",    "Los Angeles",  "CA", "events"),
    # Miami
    ("evergladesairboattours",     "Everglades Tours",   "Miami",        "FL", "events"),
    ("southbeachbikerentals",      "SB Bike Rentals",    "Miami",        "FL", "wellness"),
    ("miamifoodtours",             "Miami Food Tours",   "Miami",        "FL", "events"),
    # New Orleans
    ("neworleanstours",            "New Orleans Tours",  "New Orleans",  "LA", "events"),
    ("nolafoodtours",              "NOLA Food Tours",    "New Orleans",  "LA", "events"),
    # Seattle
    ("seattlebiketours",           "Seattle Bike Tours", "Seattle",      "WA", "wellness"),
    ("pikeplacetours",             "Pike Place Tours",   "Seattle",      "WA", "events"),
    # Austin
    ("austinfoodtours",            "Austin Food Tours",  "Austin",       "TX", "events"),
    ("austinbiketours",            "Austin Bike Tours",  "Austin",       "TX", "wellness"),
    # Denver
    ("denverbiketours",            "Denver Bike Tours",  "Denver",       "CO", "wellness"),
    ("rockytours",                 "Rocky Mountain Tour","Denver",       "CO", "events"),
    # Nashville
    ("nashvilletours",             "Nashville Tours",    "Nashville",    "TN", "events"),
    ("nashvillefoodtours",         "Nashville Food Tour","Nashville",    "TN", "events"),
    # Washington DC
    ("dc-by-foot",                 "DC by Foot",         "Washington",   "DC", "events"),
    ("capitolcitytours",           "Capitol City Tours", "Washington",   "DC", "events"),
    # Boston
    ("bostonfoodtours",            "Boston Food Tours",  "Boston",       "MA", "events"),
    ("freedomtrail",               "Freedom Trail Tour", "Boston",       "MA", "events"),
    # San Diego
    ("sandiegobiketours",          "SD Bike Tours",      "San Diego",    "CA", "wellness"),
    ("sandiegofoodtours",          "SD Food Tours",      "San Diego",    "CA", "events"),
    # Las Vegas
    ("lasvegasfoodtours",          "LV Food Tours",      "Las Vegas",    "NV", "events"),
    ("vegasmobiltours",            "Vegas Mobile Tours", "Las Vegas",    "NV", "events"),
    # Portland
    ("portlandfoodtours",          "Portland Food Tours","Portland",     "OR", "events"),
    ("portlandbiketours",          "Portland Bike Tours","Portland",     "OR", "wellness"),
    # Philadelphia
    ("phillyfoodtours",            "Philly Food Tours",  "Philadelphia", "PA", "events"),
    ("phillytours",                "Philadelphia Tours", "Philadelphia", "PA", "events"),
    # Atlanta
    ("atlantafoodtours",           "Atlanta Food Tours", "Atlanta",      "GA", "events"),
    ("atlantabiketours",           "Atlanta Bike Tours", "Atlanta",      "GA", "wellness"),
    # Minneapolis
    ("minneapolisfoodtours",       "Minneapolis Tours",  "Minneapolis",  "MN", "events"),
    # Houston
    ("houstonfoodtours",           "Houston Food Tours", "Houston",      "TX", "events"),
    # Dallas
    ("dallasfoodtours",            "Dallas Food Tours",  "Dallas",       "TX", "events"),
    # Phoenix
    ("phoenixdesertadventures",    "Desert Adventures",  "Phoenix",      "AZ", "events"),
    ("scottsdalefoodtours",        "Scottsdale Tours",   "Phoenix",      "AZ", "events"),
    # San Antonio
    ("sanantoniofoodtours",        "SA Food Tours",      "San Antonio",  "TX", "events"),
    ("riverwalkadventures",        "River Walk Tours",   "San Antonio",  "TX", "events"),
    # Charlotte
    ("charlottefoodtours",         "Charlotte Tours",    "Charlotte",    "NC", "events"),
    # Pittsburgh
    ("pittsburghfoodtours",        "Pittsburgh Tours",   "Pittsburgh",   "PA", "events"),
    # Salt Lake City
    ("saltlakecitytours",          "SLC Tours",          "Salt Lake City","UT","events"),
]


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def fetch_company_availabilities(
    shortname: str,
    company_name: str,
    city: str,
    state: str,
    category: str,
    hours_ahead: int,
    session: requests.Session,
) -> list[dict]:
    """Fetch available items and their availabilities for a FareHarbor company."""
    # First get items (services) for this company
    items_url = f"https://fareharbor.com/api/v1/companies/{shortname}/items/"
    try:
        r = session.get(items_url, timeout=15)
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
    except Exception:
        return []

    slots = []
    now_utc = datetime.now(timezone.utc)

    for item in items[:5]:  # check top 5 services per company
        item_id   = item.get("pk")
        item_name = item.get("name") or "Activity"
        min_price = item.get("customer_prototypes", [{}])[0].get("total_including_tax") if item.get("customer_prototypes") else None

        # Check availability for next 3 days
        for day_offset in range(3):
            date = (now_utc + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            avail_url = f"https://fareharbor.com/api/v1/companies/{shortname}/items/{item_id}/availabilities/date/{date}/"
            try:
                r = session.get(avail_url, timeout=10)
                if r.status_code != 200:
                    continue
                availabilities = r.json().get("availabilities", [])
            except Exception:
                continue

            for avail in availabilities:
                # Skip if capacity is full
                capacity = avail.get("capacity", 1)
                remaining = avail.get("customer_count", 0)
                if capacity > 0 and remaining >= capacity:
                    continue

                start_at = avail.get("start_at") or ""
                if not start_at:
                    continue

                hours = compute_hours_until(start_at)
                if hours is None or hours < 0 or hours > hours_ahead:
                    continue

                # Price from availability or item
                price = None
                avail_types = avail.get("customers") or avail.get("customer_prototypes", [])
                if avail_types:
                    price_raw = avail_types[0].get("total_including_tax")
                    if price_raw is not None:
                        try:
                            price = float(price_raw) / 100  # FareHarbor prices in cents
                        except (TypeError, ValueError):
                            pass
                if price is None and min_price is not None:
                    try:
                        price = float(min_price) / 100
                    except (TypeError, ValueError):
                        pass

                if price is None:
                    continue

                duration_min = None
                end_at = avail.get("end_at") or ""
                if end_at and start_at:
                    try:
                        s_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                        e_dt = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                        duration_min = int((e_dt - s_dt).total_seconds() / 60)
                    except Exception:
                        pass

                slot_id = compute_slot_id("fareharbor", f"{shortname}-{item_id}", start_at)
                booking_url = avail.get("headline_url") or f"https://fareharbor.com/{shortname}/book/{item_id}/"

                slots.append({
                    "slot_id":          slot_id,
                    "platform":         "fareharbor",
                    "business_id":      f"{shortname}-{item_id}",
                    "business_name":    company_name,
                    "category":         category,
                    "service_name":     item_name,
                    "start_time":       start_at,
                    "end_time":         end_at,
                    "duration_minutes": duration_min,
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
    parser = argparse.ArgumentParser(description="Fetch FareHarbor last-minute activity slots")
    parser.add_argument("--hours-ahead",    type=int, default=72)
    parser.add_argument("--max-companies",  type=int, default=100)
    parser.add_argument("--delay",          type=float, default=0.6)
    parser.add_argument("--dry-run",        action="store_true")
    args = parser.parse_args()

    session   = _make_session()
    all_slots: list[dict] = []
    seen_ids:  set[str]   = set()

    companies = COMPANY_SEEDS[:args.max_companies]
    for i, (shortname, name, city, state, category) in enumerate(companies, 1):
        city_slots = fetch_company_availabilities(
            shortname, name, city, state, category, args.hours_ahead, session
        )
        new = 0
        for s in city_slots:
            if s["slot_id"] not in seen_ids:
                seen_ids.add(s["slot_id"])
                all_slots.append(s)
                new += 1
        print(f"  [{i}/{len(companies)}] {name} ({city})... {new} slots")
        time.sleep(args.delay)

    priced = sum(1 for s in all_slots if (s.get("price") or 0) > 0)
    print(f"\nFareHarbor total: {len(all_slots)} slots ({priced} with prices)")

    if not args.dry_run:
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2, default=str), encoding="utf-8")
        print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
