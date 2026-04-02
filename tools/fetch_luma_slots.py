"""
fetch_luma_slots.py — Fetch upcoming events from Luma (lu.ma).

No API key required. Parses __NEXT_DATA__ JSON embedded in Luma city pages.
Luma is a modern events platform popular for tech, startup, wellness, and
social events. Prices are in cents; free events are flagged with is_free=True.

Only includes events where price is known (free or priced). Skips events
with approval-required registration and no published price.

URL pattern:
  https://lu.ma/{city_slug}   (e.g. lu.ma/nyc, lu.ma/chicago)

Usage:
    python tools/fetch_luma_slots.py [--hours-ahead 72] [--dry-run]
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")
from normalize_slot import normalize, compute_slot_id, compute_hours_until, is_within_window

OUTPUT_FILE = Path(".tmp/luma_slots.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# City slug → (city_name, state_code)
LUMA_CITIES = {
    "nyc":           ("New York",      "NY"),
    "chicago":       ("Chicago",       "IL"),
    "sf":            ("San Francisco", "CA"),
    "la":            ("Los Angeles",   "CA"),
    "seattle":       ("Seattle",       "WA"),
    "austin":        ("Austin",        "TX"),
    "boston":        ("Boston",        "MA"),
    "miami":         ("Miami",         "FL"),
    "dc":            ("Washington",    "DC"),
    "denver":        ("Denver",        "CO"),
    "atlanta":       ("Atlanta",       "GA"),
    "dallas":        ("Dallas",        "TX"),
    "houston":       ("Houston",       "TX"),
    "phoenix":       ("Phoenix",       "AZ"),
    "portland":      ("Portland",      "OR"),
    "minneapolis":   ("Minneapolis",   "MN"),
    "philadelphia":  ("Philadelphia",  "PA"),
    "san-diego":     ("San Diego",     "CA"),
    "las-vegas":     ("Las Vegas",     "NV"),
    "salt-lake-city":("Salt Lake City","UT"),
}

# Keyword→category inference for events with no explicit category
KEYWORD_CATEGORY = {
    "yoga": "wellness", "fitness": "wellness", "wellness": "wellness",
    "meditation": "wellness", "pilates": "wellness", "workout": "wellness",
    "dance": "wellness", "run": "wellness",
    "salon": "beauty", "beauty": "beauty", "spa": "beauty", "hair": "beauty",
    "networking": "events", "mixer": "events", "happy hour": "events",
    "comedy": "events", "music": "events", "concert": "events",
    "festival": "events", "party": "events", "social": "events",
    "tech": "events", "startup": "events", "ai": "events",
    "art": "events", "gallery": "events", "film": "events",
    "food": "events", "wine": "events", "beer": "events",
    "conference": "events", "workshop": "events", "seminar": "events",
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


def fetch_city(slug: str, city_name: str, state: str, hours_ahead: int, session: requests.Session) -> list[dict]:
    url = f"https://lu.ma/{slug}"
    try:
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            print(f"  [{slug}] HTTP {r.status_code} — skipping")
            return []
    except Exception as e:
        print(f"  [{slug}] Error: {e}")
        return []

    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
    if not m:
        print(f"  [{slug}] __NEXT_DATA__ not found")
        return []

    try:
        page_data = json.loads(m.group(1))
        all_events = (
            page_data["props"]["pageProps"]["initialData"]["data"]["events"]
            + page_data["props"]["pageProps"]["initialData"]["data"].get("featured_events", [])
        )
    except (KeyError, json.JSONDecodeError, TypeError) as e:
        print(f"  [{slug}] Parse error: {e}")
        return []

    slots = []
    seen_ids = set()

    for entry in all_events:
        ev = entry.get("event", {})
        ticket_info = entry.get("ticket_info") or {}

        # Determine price
        price_obj = ticket_info.get("price")
        is_free   = ticket_info.get("is_free", False)

        if price_obj and isinstance(price_obj, dict):
            price = price_obj.get("cents", 0) / 100
        elif is_free:
            price = 0.0
        else:
            # Price unknown (requires approval, no price shown) — skip
            continue

        event_id = ev.get("api_id") or entry.get("api_id", "")
        if not event_id or event_id in seen_ids:
            continue
        seen_ids.add(event_id)

        start_at = entry.get("start_at") or ev.get("start_at", "")
        end_at   = ev.get("end_at", "")
        hours    = compute_hours_until(start_at)

        if hours is None or hours < 0 or hours > hours_ahead:
            continue

        name = ev.get("name", "Event")
        slug_url = ev.get("url", "")
        booking_url = f"https://lu.ma/{slug_url}" if slug_url else url

        # Use event's geo data if available (Luma embeds full address)
        geo = ev.get("geo_address_info") or {}
        ev_city  = geo.get("city") or city_name
        ev_state = geo.get("region") or state
        coord    = ev.get("coordinate") or {}

        category = _infer_category(name)

        slot_id = compute_slot_id("luma", event_id, start_at)

        slot = {
            "slot_id":          slot_id,
            "platform":         "luma",
            "business_id":      event_id,
            "business_name":    entry.get("calendar", {}).get("name", ""),
            "category":         category,
            "service_name":     name,
            "start_time":       start_at,
            "end_time":         end_at,
            "duration_minutes": None,
            "hours_until_start": hours,
            "price":            price,
            "currency":         "USD",
            "original_price":   None,
            "location_city":    ev_city,
            "location_state":   ev_state,
            "location_country": "US",
            "latitude":         coord.get("latitude"),
            "longitude":        coord.get("longitude"),
            "booking_url":      booking_url,
            "scraped_at":       datetime.now(timezone.utc).isoformat(),
            "data_source":      "scrape",
            "confidence":       "medium",
        }
        slots.append(slot)

    return slots


def main():
    parser = argparse.ArgumentParser(description="Fetch Luma events for all supported cities")
    parser.add_argument("--hours-ahead", type=int, default=72)
    parser.add_argument("--delay",       type=float, default=0.8,
                        help="Seconds between city requests")
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    session = _make_session()
    all_slots: list[dict] = []
    seen_ids:  set[str]   = set()

    for slug, (city_name, state) in LUMA_CITIES.items():
        city_slots = fetch_city(slug, city_name, state, args.hours_ahead, session)
        new = 0
        for s in city_slots:
            if s["slot_id"] not in seen_ids:
                seen_ids.add(s["slot_id"])
                all_slots.append(s)
                new += 1
        priced = sum(1 for s in city_slots if s.get("price", 0) > 0)
        free   = sum(1 for s in city_slots if s.get("price", 0) == 0)
        print(f"  lu.ma/{slug:<16} {new:>3} new  ({priced} priced, {free} free)")
        time.sleep(args.delay)

    print(f"\nLuma total: {len(all_slots)} slots across {len(LUMA_CITIES)} cities")

    if not args.dry_run:
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2, default=str), encoding="utf-8")
        print(f"Output → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
