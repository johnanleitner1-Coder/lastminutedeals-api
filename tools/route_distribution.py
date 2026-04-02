"""
route_distribution.py — Map each deal to its distribution channels.

Takes a slot and returns the full list of channels to post to, based on:
  - Slot's city (matched against channel_directory.json)
  - Slot's category (category-specific subreddits appended)
  - National channels (always included)

Usage (as library):
    from tools.route_distribution import get_channels_for_slot
    channels = get_channels_for_slot(slot)
    # channels = {
    #   "subreddits":       ["nyc", "newyorkcity", "deals", "massage"],
    #   "twitter_hashtags": ["NYC", "NewYork", "LastMinute"],
    #   "telegram_channels": []
    # }

Usage (CLI — for inspection):
    python tools/route_distribution.py --slot-id <id>
    python tools/route_distribution.py --city "New York" --category wellness
"""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DIRECTORY_FILE = Path(__file__).parent / "channel_directory.json"
DATA_FILE      = Path(".tmp/aggregated_slots.json")


def load_directory() -> dict:
    if not DIRECTORY_FILE.exists():
        return {}
    return json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))


def get_channels_for_slot(slot: dict) -> dict:
    """
    Returns channels to post this deal to.

    Returns:
        {
          "subreddits":       [...],
          "twitter_hashtags": [...],
          "telegram_channels": [...]
        }
    """
    directory = load_directory()
    city      = (slot.get("location_city") or "").lower().strip()
    category  = (slot.get("category") or "").lower().strip()

    subreddits       = []
    twitter_hashtags = []
    telegram_channels= []

    # City-specific channels
    city_entry = directory.get(city, {})
    subreddits        += city_entry.get("subreddits", [])
    twitter_hashtags  += city_entry.get("twitter_hashtags", [])
    telegram_channels += city_entry.get("telegram_channels", [])

    # Category-specific subreddits
    cat_subreddits = directory.get("_category_subreddits", {})
    subreddits += cat_subreddits.get(category, [])

    # National channels (always included)
    national = directory.get("_national", {})
    for r in national.get("subreddits", []):
        if r not in subreddits:
            subreddits.append(r)
    for t in national.get("twitter_hashtags", []):
        if t not in twitter_hashtags:
            twitter_hashtags.append(t)
    for ch in national.get("telegram_channels", []):
        if ch not in telegram_channels:
            telegram_channels.append(ch)

    return {
        "subreddits":        subreddits,
        "twitter_hashtags":  twitter_hashtags,
        "telegram_channels": telegram_channels,
    }


def get_channels_for_city_category(city: str, category: str) -> dict:
    """Convenience wrapper for city + category strings."""
    return get_channels_for_slot({"location_city": city, "category": category})


def main():
    parser = argparse.ArgumentParser(description="Show distribution channels for a deal")
    parser.add_argument("--slot-id",  help="Look up a specific slot by ID")
    parser.add_argument("--city",     help="City name")
    parser.add_argument("--category", help="Category name")
    args = parser.parse_args()

    if args.slot_id:
        if not DATA_FILE.exists():
            print(f"No slot data at {DATA_FILE}")
            sys.exit(1)
        slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        slot  = next((s for s in slots if s.get("slot_id") == args.slot_id), None)
        if not slot:
            print(f"Slot not found: {args.slot_id}")
            sys.exit(1)
        channels = get_channels_for_slot(slot)
        print(f"Slot: {slot.get('service_name')} | {slot.get('location_city')} | {slot.get('category')}")
    elif args.city or args.category:
        channels = get_channels_for_city_category(args.city or "", args.category or "")
    else:
        parser.print_help()
        sys.exit(1)

    print(f"Subreddits:        {channels['subreddits']}")
    print(f"Twitter hashtags:  {channels['twitter_hashtags']}")
    print(f"Telegram channels: {channels['telegram_channels']}")


if __name__ == "__main__":
    main()
