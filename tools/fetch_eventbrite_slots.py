"""
fetch_eventbrite_slots.py — Fetch open event slots from Eventbrite.

No API key required. Parses event data embedded in Eventbrite search pages
(__SERVER_DATA__ JSON blob), which includes all upcoming events with times,
venues, tags, and availability signals.

Iterates through cities in tools/seeds/cities.json, finds events within the
specified time window, and writes normalized output to .tmp/eventbrite_slots.json.

URL pattern:
  https://www.eventbrite.com/d/{state-slug}--{city-slug}/all-events/
  ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&page=N

Usage:
    python tools/fetch_eventbrite_slots.py [--hours-ahead 72] [--max-cities 20]
    python tools/fetch_eventbrite_slots.py --max-pages 3  # per city
"""

import argparse
import json
import re
import sys
import time
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")
from normalize_slot import normalize, validate_schema, is_within_window

OUTPUT_FILE = Path(".tmp/eventbrite_slots.json")
CITIES_FILE = Path("tools/seeds/cities.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Eventbrite tag -> our category
CATEGORY_MAP = {
    "yoga":                 "wellness",
    "fitness":              "wellness",
    "wellness":             "wellness",
    "meditation":           "wellness",
    "massage":              "wellness",
    "pilates":              "wellness",
    "running":              "wellness",
    "cycling":              "wellness",
    "crossfit":             "wellness",
    "dance":                "wellness",
    "martial arts":         "wellness",
    "health":               "wellness",
    "salon":                "beauty",
    "beauty":               "beauty",
    "spa":                  "beauty",
    "fashion":              "beauty",
    "makeup":               "beauty",
    "hair":                 "beauty",
    "community":            "events",
    "networking":           "events",
    "business":             "events",
    "food & drink":         "events",
    "food":                 "events",
    "music":                "events",
    "arts":                 "events",
    "comedy":               "events",
    "nightlife":            "events",
    "party":                "events",
    "festival":             "events",
    "conference":           "events",
    "charity":              "events",
    "family":               "events",
    "education":            "events",
    "science":              "events",
    "tech":                 "events",
    "sports":               "events",
    "hobby":                "events",
    "travel":               "events",
    "religion":             "events",
}


def _guess_category(tags: list[str]) -> str:
    tags_lower = [t.lower() for t in tags]
    for tag in tags_lower:
        for key, cat in CATEGORY_MAP.items():
            if key in tag:
                return cat
    return "events"


def _city_to_slug(city: str, state: str) -> str:
    """Convert 'New York', 'NY' -> 'ny--new-york'"""
    city_slug  = city.lower().replace(" ", "-").replace(",", "").replace(".", "")
    state_slug = state.lower()
    return f"{state_slug}--{city_slug}"


def _parse_server_data(html: str) -> dict | None:
    """Extract and parse __SERVER_DATA__ JSON blob from Eventbrite HTML."""
    match = re.search(r"window\.__SERVER_DATA__\s*=\s*(\{.+)", html)
    if not match:
        return None
    raw = match.group(1)
    depth = 0
    end = len(raw)
    for i, ch in enumerate(raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(raw[:end])
    except json.JSONDecodeError:
        return None


def _parse_event_time(event: dict) -> tuple[str | None, str | None]:
    """
    Parse start/end ISO strings from event dict.
    Eventbrite gives: start_date, start_time, end_date, end_time, timezone.
    Returns (start_iso, end_iso) in UTC.
    """
    tz_name    = event.get("timezone", "UTC")
    start_date = event.get("start_date", "")
    start_time = event.get("start_time", "00:00")
    end_date   = event.get("end_date", "")
    end_time   = event.get("end_time", "00:00")

    if not start_date:
        return None, None

    try:
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = timezone.utc

        start_naive = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
        start_local = start_naive.replace(tzinfo=tz)
        start_iso   = start_local.astimezone(timezone.utc).isoformat()

        end_iso = None
        if end_date:
            end_naive = datetime.strptime(f"{end_date} {end_time}", "%Y-%m-%d %H:%M")
            end_local = end_naive.replace(tzinfo=tz)
            end_iso   = end_local.astimezone(timezone.utc).isoformat()

        return start_iso, end_iso

    except (ValueError, Exception):
        return None, None


def _parse_price(event: dict) -> float | None:
    """Try to extract ticket price from event data."""
    # Check ticket_availability if present
    ta = event.get("ticket_availability") or {}
    if isinstance(ta, dict):
        min_price = ta.get("minimum_ticket_price")
        if isinstance(min_price, dict):
            val = min_price.get("value") or min_price.get("major_value")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass

    # Check if explicitly free
    if event.get("is_free"):
        return 0.0

    return None


def _is_available(event: dict) -> bool:
    """Check if the event is not cancelled and appears to have tickets."""
    if event.get("is_cancelled"):
        return False
    if event.get("is_protected_event"):
        return False

    urgency = event.get("urgency_signals", {})
    msgs = urgency.get("messages", []) if isinstance(urgency, dict) else []
    if "soldOut" in msgs:
        return False

    return True


def fetch_city_events(
    city: dict,
    hours_ahead: int,
    max_pages: int,
    session: requests.Session,
) -> list[dict]:
    """Fetch and parse Eventbrite events for a single city."""
    slots  = []
    slug   = _city_to_slug(city["city"], city["state"])
    now    = datetime.now(timezone.utc)
    end_dt = now + timedelta(hours=hours_ahead)

    base_url = f"https://www.eventbrite.com/d/{slug}/all-events/"
    date_params = {
        "start_date": now.strftime("%Y-%m-%d"),
        "end_date":   end_dt.strftime("%Y-%m-%d"),
    }

    for page_num in range(1, max_pages + 1):
        params = {**date_params, "page": page_num}
        try:
            resp = session.get(base_url, params=params, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"    network error p{page_num}: {e}")
            break

        if resp.status_code == 404:
            break  # City slug not found — skip silently
        if resp.status_code != 200:
            print(f"    HTTP {resp.status_code} p{page_num}")
            break

        data = _parse_server_data(resp.text)
        if not data:
            break

        events_block = data.get("search_data", {}).get("events", {})
        results      = events_block.get("results", [])
        pagination   = events_block.get("pagination", {})

        if not results:
            break

        for event in results:
            try:
                if not _is_available(event):
                    continue

                start_iso, end_iso = _parse_event_time(event)
                if not start_iso:
                    continue

                # Filter to our window
                start_dt = datetime.fromisoformat(start_iso)
                if start_dt < now or start_dt > end_dt:
                    continue

                name    = event.get("name", "Event")
                venue   = event.get("primary_venue") or {}
                address = venue.get("address") or {}

                business_name = venue.get("name") or f"Venue in {city['city']}"
                event_city    = address.get("city") or city["city"]
                event_state   = address.get("region") or city["state"]
                lat = address.get("latitude")
                lng = address.get("longitude")
                try:
                    lat = float(lat) if lat else city.get("lat")
                    lng = float(lng) if lng else city.get("lng")
                except (TypeError, ValueError):
                    lat = city.get("lat")
                    lng = city.get("lng")

                tags     = [t.get("display_name", "") for t in event.get("tags", []) if isinstance(t, dict)]
                category = _guess_category(tags)

                event_url = event.get("url") or event.get("tickets_url", "")
                event_id  = str(event.get("id") or event.get("eid") or event.get("eventbrite_event_id") or "")

                urgency     = event.get("urgency_signals", {}) or {}
                urgency_msgs = urgency.get("messages", []) if isinstance(urgency, dict) else []
                confidence   = "high" if "fewTickets" in urgency_msgs else "medium"

                # Compute duration if we have both times
                duration_min = None
                if end_iso:
                    try:
                        end_dt_ev = datetime.fromisoformat(end_iso)
                        delta = end_dt_ev - start_dt
                        duration_min = int(delta.total_seconds() / 60)
                        if duration_min <= 0:
                            duration_min = None
                    except Exception:
                        pass

                raw = {
                    "business_id":      f"eb::{event_id}",
                    "business_name":    str(business_name),
                    "category":         category,
                    "service_name":     str(name),
                    "start_time":       start_iso,
                    "end_time":         end_iso,
                    "duration_minutes": duration_min,
                    "price":            _parse_price(event),
                    "currency":         "USD",
                    "location_city":    event_city,
                    "location_state":   event_state,
                    "location_country": "US",
                    "latitude":         lat,
                    "longitude":        lng,
                    "booking_url":      event_url,
                    "data_source":      "scrape",
                    "confidence":       confidence,
                }

                slot = normalize(raw, "eventbrite")
                if is_within_window(slot, hours_ahead):
                    valid, _ = validate_schema(slot)
                    if valid:
                        slots.append(slot)

            except Exception:
                continue

        # Stop paginating if we're on the last page
        total_pages = pagination.get("page_count", 1)
        if page_num >= total_pages:
            break

        # Small delay between pages
        time.sleep(random.uniform(0.3, 0.7))

    return slots


def main():
    parser = argparse.ArgumentParser(description="Fetch Eventbrite event slots (no API key)")
    parser.add_argument("--hours-ahead", type=int,   default=72)
    parser.add_argument("--max-cities",  type=int,   default=0,   help="0 = all cities")
    parser.add_argument("--max-pages",   type=int,   default=5,   help="Pages per city (20 events/page)")
    parser.add_argument("--delay",       type=float, default=1.5, help="Seconds between cities")
    args = parser.parse_args()

    if not CITIES_FILE.exists():
        print(f"ERROR: {CITIES_FILE} not found.")
        sys.exit(1)

    cities = json.loads(CITIES_FILE.read_text())
    if args.max_cities > 0:
        cities = cities[:args.max_cities]

    print(f"Fetching Eventbrite slots | {len(cities)} cities | {args.hours_ahead}h window | {args.max_pages} pages/city")

    session    = requests.Session()
    all_slots  = []
    city_stats = []

    for i, city in enumerate(cities):
        city_name = f"{city['city']}, {city['state']}"
        print(f"  [{i+1}/{len(cities)}] {city_name}...", end=" ", flush=True)

        city_slots = fetch_city_events(city, args.hours_ahead, args.max_pages, session)
        all_slots.extend(city_slots)
        print(f"{len(city_slots)} events")
        city_stats.append({"city": city_name, "slots": len(city_slots)})

        if i < len(cities) - 1:
            jitter = random.uniform(0, args.delay * 0.5)
            time.sleep(args.delay + jitter)

    # Deduplicate by slot_id
    seen = {}
    for slot in all_slots:
        sid = slot.get("slot_id")
        if sid and sid not in seen:
            seen[sid] = slot

    deduped = list(seen.values())

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(deduped, indent=2, default=str), encoding="utf-8")

    print(f"\n" + "-" * 50)
    print(f"Eventbrite fetch complete")
    print(f"  Cities scraped : {len(city_stats)}")
    print(f"  Total slots    : {len(all_slots)}")
    print(f"  After dedup    : {len(deduped)}")
    print(f"  Output         : {OUTPUT_FILE}")

    top = sorted(city_stats, key=lambda x: x.get("slots", 0), reverse=True)[:5]
    if top:
        print(f"\n  Top cities:")
        for c in top:
            print(f"    {c['city']:<30} {c['slots']}")


if __name__ == "__main__":
    main()
