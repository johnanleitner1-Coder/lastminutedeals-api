"""
fetch_ticketmaster_slots.py — Fetch upcoming Ticketmaster events.

Uses the Ticketmaster Discovery API v2 (free tier: 5,000 calls/day).
Covers concerts, sports, arts, theater, comedy, family events.

Prerequisites:
    1. Register at developer.ticketmaster.com (free)
    2. Copy your Consumer Key
    3. Add to .env: TICKETMASTER_API_KEY=<your_key>

Output: .tmp/ticketmaster_slots.json

Usage:
    python tools/fetch_ticketmaster_slots.py [--hours-ahead 72] [--max-cities 30]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from tools.normalize_slot import normalize, is_within_window  # noqa: E402

OUTPUT_FILE = Path(".tmp/ticketmaster_slots.json")
API_BASE    = "https://app.ticketmaster.com/discovery/v2"

# Ticketmaster segment -> our category
SEGMENT_MAP = {
    "Music":         "events",
    "Sports":        "events",
    "Arts & Theatre":"events",
    "Film":          "events",
    "Miscellaneous": "events",
    "Undefined":     "events",
}

# City list with Ticketmaster DMA (Designated Market Area) IDs for better results
# DMA IDs: https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/#dma
CITIES = [
    {"city": "New York",       "state": "NY", "stateCode": "NY"},
    {"city": "Los Angeles",    "state": "CA", "stateCode": "CA"},
    {"city": "Chicago",        "state": "IL", "stateCode": "IL"},
    {"city": "Houston",        "state": "TX", "stateCode": "TX"},
    {"city": "Phoenix",        "state": "AZ", "stateCode": "AZ"},
    {"city": "Philadelphia",   "state": "PA", "stateCode": "PA"},
    {"city": "San Antonio",    "state": "TX", "stateCode": "TX"},
    {"city": "San Diego",      "state": "CA", "stateCode": "CA"},
    {"city": "Dallas",         "state": "TX", "stateCode": "TX"},
    {"city": "San Jose",       "state": "CA", "stateCode": "CA"},
    {"city": "Austin",         "state": "TX", "stateCode": "TX"},
    {"city": "Jacksonville",   "state": "FL", "stateCode": "FL"},
    {"city": "Columbus",       "state": "OH", "stateCode": "OH"},
    {"city": "Charlotte",      "state": "NC", "stateCode": "NC"},
    {"city": "San Francisco",  "state": "CA", "stateCode": "CA"},
    {"city": "Indianapolis",   "state": "IN", "stateCode": "IN"},
    {"city": "Seattle",        "state": "WA", "stateCode": "WA"},
    {"city": "Denver",         "state": "CO", "stateCode": "CO"},
    {"city": "Washington",     "state": "DC", "stateCode": "DC"},
    {"city": "Nashville",      "state": "TN", "stateCode": "TN"},
    {"city": "Boston",         "state": "MA", "stateCode": "MA"},
    {"city": "Portland",       "state": "OR", "stateCode": "OR"},
    {"city": "Las Vegas",      "state": "NV", "stateCode": "NV"},
    {"city": "Memphis",        "state": "TN", "stateCode": "TN"},
    {"city": "Baltimore",      "state": "MD", "stateCode": "MD"},
    {"city": "Milwaukee",      "state": "WI", "stateCode": "WI"},
    {"city": "Atlanta",        "state": "GA", "stateCode": "GA"},
    {"city": "Kansas City",    "state": "MO", "stateCode": "MO"},
    {"city": "Raleigh",        "state": "NC", "stateCode": "NC"},
    {"city": "Minneapolis",    "state": "MN", "stateCode": "MN"},
    {"city": "Tampa",          "state": "FL", "stateCode": "FL"},
    {"city": "New Orleans",    "state": "LA", "stateCode": "LA"},
    {"city": "Cleveland",      "state": "OH", "stateCode": "OH"},
    {"city": "Sacramento",     "state": "CA", "stateCode": "CA"},
    {"city": "Pittsburgh",     "state": "PA", "stateCode": "PA"},
    {"city": "Orlando",        "state": "FL", "stateCode": "FL"},
    {"city": "Cincinnati",     "state": "OH", "stateCode": "OH"},
    {"city": "Buffalo",        "state": "NY", "stateCode": "NY"},
    {"city": "Richmond",       "state": "VA", "stateCode": "VA"},
    {"city": "Salt Lake City", "state": "UT", "stateCode": "UT"},
    {"city": "Hartford",       "state": "CT", "stateCode": "CT"},
    {"city": "Birmingham",     "state": "AL", "stateCode": "AL"},
    {"city": "Louisville",     "state": "KY", "stateCode": "KY"},
    {"city": "Oklahoma City",  "state": "OK", "stateCode": "OK"},
    {"city": "Tucson",         "state": "AZ", "stateCode": "AZ"},
    {"city": "Fresno",         "state": "CA", "stateCode": "CA"},
    {"city": "Albuquerque",    "state": "NM", "stateCode": "NM"},
    {"city": "Omaha",          "state": "NE", "stateCode": "NE"},
    {"city": "Detroit",        "state": "MI", "stateCode": "MI"},
    {"city": "Miami",          "state": "FL", "stateCode": "FL"},
]


def _parse_price(price_ranges: list) -> tuple[float | None, float | None]:
    """Extract min and original (max) price from Ticketmaster priceRanges."""
    if not price_ranges:
        return None, None
    # Prefer "standard" type; fall back to first usable range
    preferred = next((pr for pr in price_ranges if pr.get("type") == "standard"), None)
    candidates = [preferred] + [pr for pr in price_ranges if pr is not preferred] if preferred else price_ranges
    for pr in candidates:
        mn = pr.get("min")
        mx = pr.get("max")
        try:
            return (float(mn) if mn is not None else None,
                    float(mx) if mx is not None else None)
        except (TypeError, ValueError):
            pass
    return None, None


def fetch_city_events(city: dict, hours_ahead: int,
                      api_key: str, session: requests.Session) -> list[dict]:
    """Fetch up to 200 upcoming events for one city from Ticketmaster Discovery API."""
    now    = datetime.now(timezone.utc)
    end_dt = now + timedelta(hours=hours_ahead)

    # Ticketmaster wants ISO 8601 without the "+00:00" — use Z format
    start_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "apikey":           api_key,
        "city":             city["city"],
        "stateCode":        city["stateCode"],
        "countryCode":      "US",
        "startDateTime":    start_str,
        "endDateTime":      end_str,
        "size":             200,
        "sort":             "date,asc",
        "locale":           "*",
    }

    try:
        resp = session.get(f"{API_BASE}/events.json", params=params, timeout=20)
    except requests.RequestException as exc:
        print(f"    Request error: {exc}")
        return []

    if resp.status_code == 401:
        print("    ERROR: Invalid Ticketmaster API key. Set TICKETMASTER_API_KEY in .env")
        return []
    if resp.status_code == 429:
        print("    Rate limited by Ticketmaster — sleeping 5s")
        time.sleep(5)
        return []
    if resp.status_code != 200:
        print(f"    HTTP {resp.status_code}")
        return []

    try:
        data = resp.json()
    except Exception:
        return []

    embedded = data.get("_embedded", {})
    events   = embedded.get("events", [])

    slots = []
    for event in events:
        event_id   = event.get("id", "")
        name       = event.get("name", "")
        url        = event.get("url", "")

        # Dates
        dates = event.get("dates", {})
        start = dates.get("start", {})
        date_str = start.get("dateTime") or ""
        if not date_str:
            # Some events only have local date (no specific time)
            local_date = start.get("localDate", "")
            local_time = start.get("localTime", "20:00:00")  # assume evening
            if local_date:
                date_str = f"{local_date}T{local_time}Z"
            else:
                continue

        # Parse datetime
        try:
            if date_str.endswith("Z"):
                date_str_parsed = date_str[:-1] + "+00:00"
            else:
                date_str_parsed = date_str
            start_dt = datetime.fromisoformat(date_str_parsed)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            start_iso = start_dt.isoformat()
        except Exception:
            continue

        hours_delta = (start_dt - now).total_seconds() / 3600
        if hours_delta < 0 or hours_delta > hours_ahead:
            continue

        # Venue
        venues = (event.get("_embedded") or {}).get("venues", [])
        if venues:
            v = venues[0]
            venue_name  = v.get("name", "")
            venue_city  = (v.get("city") or {}).get("name") or city["city"]
            venue_state = (v.get("state") or {}).get("stateCode") or city["state"]
            lat_s = (v.get("location") or {}).get("latitude")
            lng_s = (v.get("location") or {}).get("longitude")
            try:
                lat = float(lat_s) if lat_s else None
                lng = float(lng_s) if lng_s else None
            except (TypeError, ValueError):
                lat, lng = None, None
        else:
            venue_name  = ""
            venue_city  = city["city"]
            venue_state = city["state"]
            lat = lng   = None

        # Segment / category
        classifications = event.get("classifications", [])
        segment = ""
        if classifications:
            seg_obj = (classifications[0].get("segment") or {})
            segment = seg_obj.get("name", "")
        category = SEGMENT_MAP.get(segment, "events")

        # Price
        price, original_price = _parse_price(event.get("priceRanges", []))

        # Performer / attraction
        attractions = (event.get("_embedded") or {}).get("attractions", [])
        if attractions:
            performer_name = attractions[0].get("name", "")
        else:
            performer_name = ""

        # Business name: prefer venue, fall back to organizer or performer
        if venue_name:
            biz_name = venue_name
            biz_id   = event_id  # venue has no stable Ticketmaster ID in free tier
        else:
            biz_name = performer_name or name
            biz_id   = event_id

        service_display = name
        if performer_name and performer_name.lower() not in name.lower():
            service_display = f"{name} — {performer_name}"

        raw = {
            "business_id":    biz_id,
            "business_name":  biz_name,
            "category":       category,
            "service_name":   service_display,
            "start_time":     start_iso,
            "price":          price,
            "original_price": original_price,
            "currency":       "USD",
            "location_city":  venue_city,
            "location_state": venue_state,
            "latitude":       lat,
            "longitude":      lng,
            "booking_url":    url,
            "data_source":    "api",
            "confidence":     "high",
        }

        slot = normalize(raw, "ticketmaster")
        if is_within_window(slot, hours_ahead):
            slots.append(slot)

    return slots


def main():
    parser = argparse.ArgumentParser(description="Fetch Ticketmaster event slots")
    parser.add_argument("--hours-ahead", type=int, default=72)
    parser.add_argument("--max-cities",  type=int, default=30)
    args = parser.parse_args()

    api_key = os.getenv("TICKETMASTER_API_KEY", "").strip()
    if not api_key:
        print("Ticketmaster: TICKETMASTER_API_KEY not set in .env — skipping.")
        print("  Get a free key at: developer.ticketmaster.com")
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        OUTPUT_FILE.write_text("[]", encoding="utf-8")
        return

    OUTPUT_FILE.parent.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "LastMinuteDeals/1.0",
        "Accept": "application/json",
    })

    cities = CITIES[:args.max_cities]
    print(f"Ticketmaster: fetching events for {len(cities)} cities "
          f"({args.hours_ahead}h window)...")

    all_slots: list[dict] = []
    seen_ids:  set[str]   = set()

    for city in cities:
        tag = f"{city['city']}, {city['state']}"
        try:
            slots = fetch_city_events(city, args.hours_ahead, api_key, session)
            new   = [s for s in slots if s["slot_id"] not in seen_ids]
            if new:
                seen_ids.update(s["slot_id"] for s in new)
                all_slots.extend(new)
                print(f"  {tag}: {len(new)} events")
            else:
                print(f"  {tag}: 0 events")
        except Exception as exc:
            print(f"  {tag}: ERROR - {exc}")

        # Ticketmaster free tier: 5 req/sec — be safe with 0.3s delay
        time.sleep(0.3)

    OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2), encoding="utf-8")
    print(f"\nTicketmaster: {len(all_slots)} total slots -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
