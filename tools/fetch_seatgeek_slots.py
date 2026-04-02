"""
fetch_seatgeek_slots.py — Fetch upcoming SeatGeek events for US cities.

Uses the SeatGeek public API v2 (free tier, no OAuth required beyond client_id).
Covers concerts, sports, theater, comedy, and more — all with live ticket prices.

Prerequisites:
    1. Register for a free account at platform.seatgeek.com
    2. Create an app to receive a client_id
    3. Add to .env:  SEATGEEK_CLIENT_ID=<your_client_id>

Output: .tmp/seatgeek_slots.json

Usage:
    python tools/fetch_seatgeek_slots.py [--hours-ahead 72] [--max-cities 30]
"""

import sys

sys.path.insert(0, ".")

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

from tools.normalize_slot import normalize, is_within_window  # noqa: E402

OUTPUT_FILE = Path(".tmp/seatgeek_slots.json")
API_BASE    = "https://api.seatgeek.com/2/events"

# SeatGeek event type -> our normalized category
TYPE_MAP = {
    "concert":          "events",
    "sports":           "events",
    "theater":          "events",
    "comedy":           "events",
    "classical":        "events",
    "dance_performance_tour": "events",
    "cirque_du_soleil": "events",
    "family":           "events",
    "opera":            "events",
    "film":             "events",
    "festival":         "events",
    "wrestling":        "events",
    "hockey":           "events",
    "basketball":       "events",
    "baseball":         "events",
    "football":         "events",
    "soccer":           "events",
    "golf":             "events",
    "tennis":           "events",
    "auto_racing":      "events",
    "boxing":           "events",
    "mma":              "events",
    "rodeo":            "events",
    "roller_derby":     "events",
    "horse_racing":     "events",
    "volleyball":       "events",
    "lacrosse":         "events",
    "rugby":            "events",
    "yoga":             "wellness",
    "fitness":          "wellness",
}

CITIES = [
    {"city": "New York",       "state": "New York",       "stateCode": "NY"},
    {"city": "Los Angeles",    "state": "California",     "stateCode": "CA"},
    {"city": "Chicago",        "state": "Illinois",       "stateCode": "IL"},
    {"city": "Houston",        "state": "Texas",          "stateCode": "TX"},
    {"city": "Phoenix",        "state": "Arizona",        "stateCode": "AZ"},
    {"city": "Philadelphia",   "state": "Pennsylvania",   "stateCode": "PA"},
    {"city": "San Antonio",    "state": "Texas",          "stateCode": "TX"},
    {"city": "San Diego",      "state": "California",     "stateCode": "CA"},
    {"city": "Dallas",         "state": "Texas",          "stateCode": "TX"},
    {"city": "San Jose",       "state": "California",     "stateCode": "CA"},
    {"city": "Austin",         "state": "Texas",          "stateCode": "TX"},
    {"city": "Jacksonville",   "state": "Florida",        "stateCode": "FL"},
    {"city": "Columbus",       "state": "Ohio",           "stateCode": "OH"},
    {"city": "Charlotte",      "state": "North Carolina", "stateCode": "NC"},
    {"city": "San Francisco",  "state": "California",     "stateCode": "CA"},
    {"city": "Indianapolis",   "state": "Indiana",        "stateCode": "IN"},
    {"city": "Seattle",        "state": "Washington",     "stateCode": "WA"},
    {"city": "Denver",         "state": "Colorado",       "stateCode": "CO"},
    {"city": "Washington",     "state": "District of Columbia", "stateCode": "DC"},
    {"city": "Nashville",      "state": "Tennessee",      "stateCode": "TN"},
    {"city": "Boston",         "state": "Massachusetts",  "stateCode": "MA"},
    {"city": "Portland",       "state": "Oregon",         "stateCode": "OR"},
    {"city": "Las Vegas",      "state": "Nevada",         "stateCode": "NV"},
    {"city": "Memphis",        "state": "Tennessee",      "stateCode": "TN"},
    {"city": "Baltimore",      "state": "Maryland",       "stateCode": "MD"},
    {"city": "Milwaukee",      "state": "Wisconsin",      "stateCode": "WI"},
    {"city": "Atlanta",        "state": "Georgia",        "stateCode": "GA"},
    {"city": "Kansas City",    "state": "Missouri",       "stateCode": "MO"},
    {"city": "Raleigh",        "state": "North Carolina", "stateCode": "NC"},
    {"city": "Minneapolis",    "state": "Minnesota",      "stateCode": "MN"},
    {"city": "Tampa",          "state": "Florida",        "stateCode": "FL"},
    {"city": "New Orleans",    "state": "Louisiana",      "stateCode": "LA"},
    {"city": "Cleveland",      "state": "Ohio",           "stateCode": "OH"},
    {"city": "Sacramento",     "state": "California",     "stateCode": "CA"},
    {"city": "Pittsburgh",     "state": "Pennsylvania",   "stateCode": "PA"},
    {"city": "Orlando",        "state": "Florida",        "stateCode": "FL"},
    {"city": "Cincinnati",     "state": "Ohio",           "stateCode": "OH"},
    {"city": "Miami",          "state": "Florida",        "stateCode": "FL"},
    {"city": "Detroit",        "state": "Michigan",       "stateCode": "MI"},
    {"city": "Louisville",     "state": "Kentucky",       "stateCode": "KY"},
    {"city": "Omaha",          "state": "Nebraska",       "stateCode": "NE"},
    {"city": "Oklahoma City",  "state": "Oklahoma",       "stateCode": "OK"},
    {"city": "Salt Lake City", "state": "Utah",           "stateCode": "UT"},
    {"city": "Hartford",       "state": "Connecticut",    "stateCode": "CT"},
    {"city": "Richmond",       "state": "Virginia",       "stateCode": "VA"},
    {"city": "Birmingham",     "state": "Alabama",        "stateCode": "AL"},
    {"city": "Buffalo",        "state": "New York",       "stateCode": "NY"},
    {"city": "Tucson",         "state": "Arizona",        "stateCode": "AZ"},
    {"city": "Fresno",         "state": "California",     "stateCode": "CA"},
    {"city": "Albuquerque",    "state": "New Mexico",     "stateCode": "NM"},
]


def fetch_city_events(
    city: dict,
    hours_ahead: int,
    client_id: str,
    session: requests.Session,
) -> list[dict]:
    """Fetch up to 500 upcoming events for one city from SeatGeek API."""
    now    = datetime.now(timezone.utc)
    end_dt = now + timedelta(hours=hours_ahead)

    # SeatGeek expects ISO 8601 UTC strings (with or without Z)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    params = {
        "client_id":        client_id,
        "per_page":         500,
        "sort":             "datetime_local.asc",
        "datetime_utc.gte": now_str,
        "datetime_utc.lte": end_str,
        "venue.city":       city["city"],
        "venue.state_code": city["stateCode"],
    }

    try:
        resp = session.get(API_BASE, params=params, timeout=20)
    except requests.RequestException as exc:
        print(f"    Request error: {exc}")
        return []

    if resp.status_code == 401:
        print("    ERROR: Invalid SeatGeek client_id. Check SEATGEEK_CLIENT_ID in .env")
        return []
    if resp.status_code == 429:
        print("    Rate limited by SeatGeek — sleeping 10s")
        time.sleep(10)
        return []
    if resp.status_code != 200:
        print(f"    HTTP {resp.status_code}: {resp.text[:200]}")
        return []

    try:
        data = resp.json()
    except Exception:
        return []

    events = data.get("events", [])

    slots  = []
    for event in events:
        # ── Timing ───────────────────────────────────────────────────────────
        datetime_utc = event.get("datetime_utc", "")
        if not datetime_utc:
            continue

        # Normalize to a proper ISO string with UTC offset
        try:
            if datetime_utc.endswith("Z"):
                dt_parsed = datetime_utc[:-1] + "+00:00"
            elif "+" not in datetime_utc and datetime_utc.count("-") < 3:
                # Plain datetime string assumed UTC
                dt_parsed = datetime_utc + "+00:00"
            else:
                dt_parsed = datetime_utc
            start_dt  = datetime.fromisoformat(dt_parsed)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            start_iso = start_dt.isoformat()
        except Exception:
            continue

        hours_delta = (start_dt - now).total_seconds() / 3600
        if hours_delta < 0 or hours_delta > hours_ahead:
            continue

        # ── Venue ─────────────────────────────────────────────────────────────
        venue = event.get("venue") or {}
        venue_id    = venue.get("id", "")
        venue_name  = venue.get("name", "")
        venue_city  = venue.get("city") or city["city"]
        venue_state = venue.get("state") or city["state"]

        location    = venue.get("location") or {}
        try:
            lat = float(location["lat"])  if location.get("lat")  is not None else None
        except (TypeError, ValueError):
            lat = None
        try:
            lon = float(location["lon"])  if location.get("lon")  is not None else None
        except (TypeError, ValueError):
            lon = None

        # ── Category ──────────────────────────────────────────────────────────
        event_type = (event.get("type") or "").lower()
        category   = TYPE_MAP.get(event_type, "events")

        # ── Pricing ───────────────────────────────────────────────────────────
        stats          = event.get("stats") or {}
        lowest_price   = stats.get("lowest_price")
        average_price  = stats.get("average_price")
        try:
            price = float(lowest_price) if lowest_price is not None else None
        except (TypeError, ValueError):
            price = None
        try:
            original_price = float(average_price) if average_price is not None else None
        except (TypeError, ValueError):
            original_price = None

        # ── Build raw record and normalize ───────────────────────────────────
        raw = {
            "business_id":    str(venue_id) if venue_id else str(event.get("id", "")),
            "business_name":  venue_name or event.get("title", ""),
            "category":       category,
            "service_name":   event.get("title", ""),
            "start_time":     start_iso,
            "price":          price,
            "original_price": original_price,
            "currency":       "USD",
            "location_city":  venue_city,
            "location_state": venue_state,
            "latitude":       lat,
            "longitude":      lon,
            "booking_url":    event.get("url", ""),
            "data_source":    "api",
            "confidence":     "high",
        }

        slot = normalize(raw, "seatgeek")
        if is_within_window(slot, hours_ahead):
            slots.append(slot)

    return slots


def main():
    parser = argparse.ArgumentParser(description="Fetch SeatGeek event slots")
    parser.add_argument("--hours-ahead", type=int, default=72,
                        help="Look-ahead window in hours (default: 72)")
    parser.add_argument("--max-cities",  type=int, default=30,
                        help="Number of cities to query (default: 30, max: 50)")
    args = parser.parse_args()

    client_id = os.getenv("SEATGEEK_CLIENT_ID", "").strip()
    if not client_id:
        print("SeatGeek: SEATGEEK_CLIENT_ID not set in .env — skipping.")
        print("  1. Register a free app at: platform.seatgeek.com")
        print("  2. Copy your client_id")
        print("  3. Add to .env:  SEATGEEK_CLIENT_ID=<your_client_id>")
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        OUTPUT_FILE.write_text("[]", encoding="utf-8")
        return

    OUTPUT_FILE.parent.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "LastMinuteDeals/1.0",
        "Accept":     "application/json",
    })

    cities = CITIES[:args.max_cities]
    print(f"SeatGeek: fetching events for {len(cities)} cities "
          f"({args.hours_ahead}h window)...")

    all_slots: list[dict] = []
    seen_ids:  set[str]   = set()

    for city in cities:
        tag = f"{city['city']}, {city['stateCode']}"
        try:
            slots = fetch_city_events(city, args.hours_ahead, client_id, session)
            new   = [s for s in slots if s["slot_id"] not in seen_ids]
            if new:
                seen_ids.update(s["slot_id"] for s in new)
                all_slots.extend(new)
            print(f"  SeatGeek: {tag}: {len(new)} events")
        except Exception as exc:
            print(f"  SeatGeek: {tag}: ERROR - {exc}")

        time.sleep(0.2)

    OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2), encoding="utf-8")
    print(f"\nSeatGeek: {len(all_slots)} total slots -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
