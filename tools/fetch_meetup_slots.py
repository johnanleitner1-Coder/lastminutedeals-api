"""
fetch_meetup_slots.py -- Fetch upcoming Meetup events (no API key required).

Scrapes Meetup's city search page and parses the __NEXT_DATA__ / __APOLLO_STATE__
JSON blob embedded in every page response. No API key or account needed.

Meetup event objects live inside __APOLLO_STATE__ under keys like "Event:xxxxxxxx".
Each event has: id, title, dateTime, endTime, eventUrl, venue (inline), group (__ref),
rsvpState, maxTickets, feeSettings (price), topics, etc.

Output: .tmp/meetup_slots.json

Usage:
    python tools/fetch_meetup_slots.py [--hours-ahead 72] [--max-cities 30] [--max-pages 3]
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from tools.normalize_slot import normalize, is_within_window  # noqa: E402

OUTPUT_FILE = Path(".tmp/meetup_slots.json")

# Top 50 US metros with Meetup city slug format
CITIES = [
    {"city": "New York",       "state": "NY", "slug": "us--ny--new-york"},
    {"city": "Los Angeles",    "state": "CA", "slug": "us--ca--los-angeles"},
    {"city": "Chicago",        "state": "IL", "slug": "us--il--chicago"},
    {"city": "Houston",        "state": "TX", "slug": "us--tx--houston"},
    {"city": "Phoenix",        "state": "AZ", "slug": "us--az--phoenix"},
    {"city": "Philadelphia",   "state": "PA", "slug": "us--pa--philadelphia"},
    {"city": "San Antonio",    "state": "TX", "slug": "us--tx--san-antonio"},
    {"city": "San Diego",      "state": "CA", "slug": "us--ca--san-diego"},
    {"city": "Dallas",         "state": "TX", "slug": "us--tx--dallas"},
    {"city": "San Jose",       "state": "CA", "slug": "us--ca--san-jose"},
    {"city": "Austin",         "state": "TX", "slug": "us--tx--austin"},
    {"city": "Jacksonville",   "state": "FL", "slug": "us--fl--jacksonville"},
    {"city": "Columbus",       "state": "OH", "slug": "us--oh--columbus"},
    {"city": "Charlotte",      "state": "NC", "slug": "us--nc--charlotte"},
    {"city": "San Francisco",  "state": "CA", "slug": "us--ca--san-francisco"},
    {"city": "Indianapolis",   "state": "IN", "slug": "us--in--indianapolis"},
    {"city": "Seattle",        "state": "WA", "slug": "us--wa--seattle"},
    {"city": "Denver",         "state": "CO", "slug": "us--co--denver"},
    {"city": "Washington",     "state": "DC", "slug": "us--dc--washington"},
    {"city": "Nashville",      "state": "TN", "slug": "us--tn--nashville"},
    {"city": "Boston",         "state": "MA", "slug": "us--ma--boston"},
    {"city": "Portland",       "state": "OR", "slug": "us--or--portland"},
    {"city": "Las Vegas",      "state": "NV", "slug": "us--nv--las-vegas"},
    {"city": "Memphis",        "state": "TN", "slug": "us--tn--memphis"},
    {"city": "Baltimore",      "state": "MD", "slug": "us--md--baltimore"},
    {"city": "Milwaukee",      "state": "WI", "slug": "us--wi--milwaukee"},
    {"city": "Atlanta",        "state": "GA", "slug": "us--ga--atlanta"},
    {"city": "Kansas City",    "state": "MO", "slug": "us--mo--kansas-city"},
    {"city": "Raleigh",        "state": "NC", "slug": "us--nc--raleigh"},
    {"city": "Minneapolis",    "state": "MN", "slug": "us--mn--minneapolis"},
    {"city": "Tampa",          "state": "FL", "slug": "us--fl--tampa"},
    {"city": "New Orleans",    "state": "LA", "slug": "us--la--new-orleans"},
    {"city": "Cleveland",      "state": "OH", "slug": "us--oh--cleveland"},
    {"city": "Sacramento",     "state": "CA", "slug": "us--ca--sacramento"},
    {"city": "Pittsburgh",     "state": "PA", "slug": "us--pa--pittsburgh"},
    {"city": "Orlando",        "state": "FL", "slug": "us--fl--orlando"},
    {"city": "Cincinnati",     "state": "OH", "slug": "us--oh--cincinnati"},
    {"city": "Salt Lake City", "state": "UT", "slug": "us--ut--salt-lake-city"},
    {"city": "Detroit",        "state": "MI", "slug": "us--mi--detroit"},
    {"city": "Miami",          "state": "FL", "slug": "us--fl--miami"},
    {"city": "Louisville",     "state": "KY", "slug": "us--ky--louisville"},
    {"city": "Omaha",          "state": "NE", "slug": "us--ne--omaha"},
    {"city": "Oklahoma City",  "state": "OK", "slug": "us--ok--oklahoma-city"},
    {"city": "Albuquerque",    "state": "NM", "slug": "us--nm--albuquerque"},
    {"city": "Tucson",         "state": "AZ", "slug": "us--az--tucson"},
    {"city": "Fresno",         "state": "CA", "slug": "us--ca--fresno"},
    {"city": "Richmond",       "state": "VA", "slug": "us--va--richmond"},
    {"city": "Hartford",       "state": "CT", "slug": "us--ct--hartford"},
    {"city": "Birmingham",     "state": "AL", "slug": "us--al--birmingham"},
    {"city": "Buffalo",        "state": "NY", "slug": "us--ny--buffalo"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_URL = "https://www.meetup.com/find/"


def _resolve_ref(apollo: dict, obj_or_ref) -> dict:
    """Resolve an Apollo __ref pointer or return the object as-is if inline."""
    if isinstance(obj_or_ref, dict):
        ref = obj_or_ref.get("__ref")
        if ref:
            return apollo.get(ref, {})
        return obj_or_ref
    return {}


def _infer_category(topic_keys: list, title: str) -> str:
    """Map Meetup topic keys / title keywords to our canonical categories."""
    combined = " ".join(topic_keys + [title]).lower()
    if any(k in combined for k in ["yoga", "fitness", "run", "cycling", "swim",
                                    "meditation", "wellness", "pilates", "workout",
                                    "gym", "zumba", "hiking", "outdoors", "dance"]):
        return "wellness"
    if any(k in combined for k in ["beauty", "makeup", "hair", "skincare", "nail"]):
        return "beauty"
    if any(k in combined for k in ["professional", "career", "entrepreneur",
                                    "business", "startup", "networking",
                                    "coding", "tech", "developer", "ai",
                                    "photography", "writing"]):
        return "professional_services"
    return "events"


def _parse_apollo_state(next_data: dict, city: dict, hours_ahead: int) -> list[dict]:
    """
    Extract event slots from Meetup's __APOLLO_STATE__ GraphQL cache.
    Event keys look like "Event:313713085".
    Venue is inline dict; Group is a __ref.
    """
    props  = next_data.get("props", {}).get("pageProps", {})
    apollo = props.get("__APOLLO_STATE__", {})
    if not apollo:
        return []

    now   = datetime.now(timezone.utc)
    slots = []

    for key, obj in apollo.items():
        if not key.startswith("Event:") or not isinstance(obj, dict):
            continue

        event_id  = obj.get("id") or key.replace("Event:", "")
        title     = obj.get("title") or obj.get("name", "")
        event_url = obj.get("eventUrl") or obj.get("url") or ""
        date_time = obj.get("dateTime") or obj.get("dateTimeStr") or ""
        end_time  = obj.get("endTime") or obj.get("endTimeStr") or ""

        if not date_time:
            continue

        # Parse start time -- Meetup uses local ISO with offset
        # e.g. "2026-03-25T18:30:00-04:00"
        try:
            if isinstance(date_time, (int, float)):
                start_dt = datetime.fromtimestamp(date_time / 1000, tz=timezone.utc)
            else:
                start_dt = datetime.fromisoformat(str(date_time))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            start_iso = start_dt.astimezone(timezone.utc).isoformat()
        except Exception:
            continue

        hours_delta = (start_dt - now).total_seconds() / 3600
        if hours_delta < 0 or hours_delta > hours_ahead:
            continue

        # End time
        end_iso = None
        if end_time:
            try:
                if isinstance(end_time, (int, float)):
                    end_iso = datetime.fromtimestamp(end_time / 1000, tz=timezone.utc).isoformat()
                else:
                    end_dt = datetime.fromisoformat(str(end_time))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    end_iso = end_dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass

        # Venue -- inline dict with __typename, name, address, city, state
        venue = _resolve_ref(apollo, obj.get("venue") or {})
        venue_name  = venue.get("name") or ""
        venue_city  = venue.get("city") or city["city"]
        venue_state = venue.get("state") or city["state"]
        lat = venue.get("lat") or venue.get("latitude")
        lng = venue.get("lon") or venue.get("lng") or venue.get("longitude")

        # Group (organizer) -- __ref pointing to Group:xxxxx in apollo cache
        group = _resolve_ref(apollo, obj.get("group") or {})
        group_name = group.get("name") or group.get("urlname") or "Meetup"
        group_id   = str(group.get("id") or group.get("urlname") or event_id)

        # Price -- feeSettings is null for free events
        fee   = obj.get("feeSettings")
        price = None
        if isinstance(fee, dict) and fee.get("amount") is not None:
            try:
                price = float(fee["amount"])
            except (TypeError, ValueError):
                pass

        # Topics -- may be edge list or direct list
        topics_val = obj.get("topics") or {}
        topic_keys = []
        if isinstance(topics_val, dict):
            edges = topics_val.get("edges") or topics_val.get("nodes") or []
            for edge in edges:
                node = _resolve_ref(apollo, edge.get("node") or edge)
                topic_keys.append(node.get("urlKey") or node.get("name") or "")
        elif isinstance(topics_val, list):
            for t in topics_val:
                if isinstance(t, dict):
                    topic_keys.append(t.get("urlKey") or t.get("name") or "")
                elif isinstance(t, str):
                    topic_keys.append(t)

        category = _infer_category(topic_keys, title)

        raw = {
            "business_id":    group_id,
            "business_name":  group_name,
            "category":       category,
            "service_name":   title,
            "start_time":     start_iso,
            "end_time":       end_iso,
            "price":          price,
            "currency":       "USD",
            "location_city":  venue_city or city["city"],
            "location_state": venue_state or city["state"],
            "latitude":       float(lat) if lat else None,
            "longitude":      float(lng) if lng else None,
            "booking_url":    event_url,
            "data_source":    "scrape",
            "confidence":     "medium",
        }

        slot = normalize(raw, "meetup")
        if is_within_window(slot, hours_ahead):
            slots.append(slot)

    return slots


def fetch_city_events(city: dict, hours_ahead: int, max_pages: int,
                      session: requests.Session) -> list[dict]:
    """Fetch events for one city, paginating through max_pages pages."""
    now     = datetime.now(timezone.utc)
    end_dt  = now + timedelta(hours=hours_ahead)
    start_s = now.strftime("%Y-%m-%dT%H:%M:%S")
    end_s   = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    slots: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        params: dict = {
            "location":       city["slug"],
            "source":         "EVENTS",
            "distance":       "tenMiles",
            "startDateRange": start_s,
            "endDateRange":   end_s,
        }
        if page > 1:
            params["page"] = page

        try:
            resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
            if resp.status_code == 429:
                print(f"    Rate limited on page {page}; stopping city.")
                break
            if resp.status_code != 200:
                break

            match = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>\s*(\{.+?)\s*</script>',
                resp.text, re.DOTALL
            )
            if not match:
                break

            try:
                next_data = json.loads(match.group(1))
            except json.JSONDecodeError:
                break

            page_slots = _parse_apollo_state(next_data, city, hours_ahead)
            new = [s for s in page_slots if s["slot_id"] not in seen_ids]
            if not new:
                break  # no new events on this page

            seen_ids.update(s["slot_id"] for s in new)
            slots.extend(new)

        except Exception as exc:
            print(f"    Page {page} error: {exc}")
            break

        time.sleep(1.5)  # polite crawl delay

    return slots


def main():
    parser = argparse.ArgumentParser(description="Fetch Meetup event slots")
    parser.add_argument("--hours-ahead", type=int, default=72)
    parser.add_argument("--max-cities",  type=int, default=30)
    parser.add_argument("--max-pages",   type=int, default=3)
    args = parser.parse_args()

    OUTPUT_FILE.parent.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    cities = CITIES[:args.max_cities]
    print(f"Meetup: fetching events for {len(cities)} cities "
          f"({args.hours_ahead}h window, {args.max_pages} pages/city)...")

    all_slots: list[dict] = []
    seen_ids:  set[str]   = set()

    for city in cities:
        tag = f"{city['city']}, {city['state']}"
        try:
            slots = fetch_city_events(city, args.hours_ahead, args.max_pages, session)
            new   = [s for s in slots if s["slot_id"] not in seen_ids]
            if new:
                seen_ids.update(s["slot_id"] for s in new)
                all_slots.extend(new)
                print(f"  {tag}: {len(new)} events")
            else:
                print(f"  {tag}: 0 events")
        except Exception as exc:
            print(f"  {tag}: ERROR - {exc}")

        time.sleep(1.0)

    OUTPUT_FILE.write_text(json.dumps(all_slots, indent=2), encoding="utf-8")
    print(f"\nMeetup: {len(all_slots)} total slots -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
