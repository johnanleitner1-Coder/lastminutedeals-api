"""
fetch_dice_slots.py — Fetch upcoming events from Dice.fm.

Dice is a major events/music ticketing platform. Their website embeds
event data in __NEXT_DATA__ JSON on city and event pages.

City URL pattern:
  https://dice.fm/browse/events?countries=US&tags={genre}&page=1

We use their internal search API which is publicly accessible:
  GET https://api.dice.fm/api/v1/events?page_size=50&page=1&location={city}&country_code=US

No API key required for read-only event discovery.

Usage:
    python tools/fetch_dice_slots.py [--hours-ahead 72] [--max-cities 30] [--dry-run]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")
from normalize_slot import normalize, compute_slot_id, compute_hours_until

OUTPUT_FILE = Path(".tmp/dice_slots.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "x-api-key": "eyJhbGciOiJIUzI1NiJ9.eyJhcHAiOiJkaWNlIiwidmVyc2lvbiI6IjIuMS4wIn0.oj_dxV3_S3UrD3VPkTi8PQtnTNKkP6eXTmfrP3K55vw",
}

# Major US cities for Dice (uses city name in their location search)
DICE_CITIES = [
    ("New York",       "NY"),
    ("Los Angeles",    "CA"),
    ("Chicago",        "IL"),
    ("San Francisco",  "CA"),
    ("Seattle",        "WA"),
    ("Austin",         "TX"),
    ("Miami",          "FL"),
    ("Boston",         "MA"),
    ("Washington",     "DC"),
    ("Denver",         "CO"),
    ("Atlanta",        "GA"),
    ("Dallas",         "TX"),
    ("Houston",        "TX"),
    ("Phoenix",        "AZ"),
    ("Portland",       "OR"),
    ("Minneapolis",    "MN"),
    ("Philadelphia",   "PA"),
    ("San Diego",      "CA"),
    ("Las Vegas",      "NV"),
    ("Nashville",      "TN"),
    ("Detroit",        "MI"),
    ("New Orleans",    "LA"),
    ("Salt Lake City", "UT"),
    ("Kansas City",    "MO"),
    ("Charlotte",      "NC"),
    ("Raleigh",        "NC"),
    ("Tampa",          "FL"),
    ("Orlando",        "FL"),
    ("Pittsburgh",     "PA"),
    ("Cleveland",      "OH"),
]

KEYWORD_CATEGORY = {
    "yoga": "wellness", "fitness": "wellness", "wellness": "wellness",
    "meditation": "wellness", "pilates": "wellness",
    "salon": "beauty", "beauty": "beauty", "spa": "beauty",
    "concert": "events", "music": "events", "festival": "events",
    "comedy": "events", "theater": "events", "theatre": "events",
    "dance": "events", "dj": "events", "club": "events",
    "art": "events", "gallery": "events", "film": "events",
    "food": "events", "wine": "events", "beer": "events",
    "networking": "events", "workshop": "events", "conference": "events",
}


def _infer_category(name: str) -> str:
    name_lower = name.lower()
    for kw, cat in KEYWORD_CATEGORY.items():
        if kw in name_lower:
            return cat
    return "events"


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def fetch_city_events(city: str, state: str, hours_ahead: int, session: requests.Session) -> list[dict]:
    """Fetch events for a single city using Dice's internal search API."""
    slots = []
    page = 1
    max_pages = 3

    while page <= max_pages:
        url = "https://api.dice.fm/api/v1/events"
        params = {
            "page":         page,
            "page_size":    50,
            "location":     city,
            "country_code": "US",
            "types":        "event",
        }
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 404:
                break
            if r.status_code != 200:
                print(f"  [{city}] HTTP {r.status_code} — skipping")
                break
            data = r.json()
        except Exception as e:
            print(f"  [{city}] Error: {e}")
            break

        events = data.get("data", [])
        if not events:
            break

        for ev in events:
            # Dice event structure
            ev_id     = str(ev.get("id") or ev.get("event_id") or "")
            name      = ev.get("name") or ev.get("title") or "Event"
            venue     = (ev.get("venue") or {})
            venue_name = venue.get("name") or ""
            ev_city   = (venue.get("city") or city).strip()
            ev_state  = (venue.get("state") or state).strip()

            # Timing
            start_at = ev.get("date") or ev.get("start_date") or ""
            if not start_at:
                continue

            hours = compute_hours_until(start_at)
            if hours is None or hours < 0 or hours > hours_ahead:
                continue

            # Price — Dice uses min_price in cents or as a float
            price = None
            min_price = ev.get("min_price") or ev.get("price") or ev.get("ticket_types", [{}])[0].get("price") if ev.get("ticket_types") else None
            if min_price is not None:
                try:
                    p = float(min_price)
                    # Dice prices are usually in dollars already; if > 1000, assume cents
                    price = p / 100 if p > 1000 else p
                except (TypeError, ValueError):
                    pass

            if ev.get("is_free") or ev.get("free"):
                price = 0.0

            if price is None:
                continue  # skip unknown price

            # Booking URL
            slug = ev.get("url") or ev.get("slug") or ev_id
            booking_url = f"https://dice.fm/event/{slug}" if slug else "https://dice.fm"

            slot_id = compute_slot_id("dice", ev_id or name, start_at)

            slots.append({
                "slot_id":          slot_id,
                "platform":         "dice",
                "business_id":      ev_id or name,
                "business_name":    venue_name,
                "category":         _infer_category(name),
                "service_name":     name,
                "start_time":       start_at,
                "end_time":         ev.get("end_date") or "",
                "duration_minutes": None,
                "hours_until_start": hours,
                "price":            price,
                "currency":         "USD",
                "original_price":   None,
                "location_city":    ev_city,
                "location_state":   ev_state,
                "location_country": "US",
                "latitude":         venue.get("latitude"),
                "longitude":        venue.get("longitude"),
                "booking_url":      booking_url,
                "scraped_at":       datetime.now(timezone.utc).isoformat(),
                "data_source":      "api",
                "confidence":       "medium",
            })

        # Check pagination
        meta = data.get("meta") or {}
        if not meta.get("next_page") and page >= (meta.get("total_pages") or 1):
            break
        page += 1

    return slots


def main():
    parser = argparse.ArgumentParser(description="Fetch Dice.fm events for US cities")
    parser.add_argument("--hours-ahead", type=int, default=72)
    parser.add_argument("--max-cities",  type=int, default=30)
    parser.add_argument("--delay",       type=float, default=0.5)
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    session    = _make_session()
    all_slots: list[dict] = []
    seen_ids:  set[str]   = set()

    cities = DICE_CITIES[:args.max_cities]
    for i, (city, state) in enumerate(cities, 1):
        city_slots = fetch_city_events(city, state, args.hours_ahead, session)
        new = 0
        for s in city_slots:
            if s["slot_id"] not in seen_ids:
                seen_ids.add(s["slot_id"])
                all_slots.append(s)
                new += 1
        priced = sum(1 for s in city_slots if (s.get("price") or 0) > 0)
        free   = sum(1 for s in city_slots if s.get("price") == 0.0)
        print(f"  [{i}/{len(cities)}] {city}, {state}... {new} new ({priced} priced, {free} free)")
        time.sleep(args.delay)

    print(f"\nDice total: {len(all_slots)} slots across {len(cities)} cities")

    if not args.dry_run:
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2, default=str), encoding="utf-8")
        print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
