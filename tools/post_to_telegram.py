"""
post_to_telegram.py — Post new deals to a Telegram channel.

Posts the top N new/updated deals to a Telegram channel via Bot API.
Only posts slots that are genuinely new since the last run (tracked by
.tmp/telegram_posted_ids.json) to avoid spam.

Prerequisites:
  1. Create a bot via @BotFather on Telegram:
       /newbot -> name it "Last Minute Deals" -> copy the token
  2. Create a public channel (e.g. @lastminutedeals_us) and add the bot as admin
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=<token from BotFather>
       TELEGRAM_CHANNEL_ID=@lastminutedeals_us   (or numeric ID like -1001234567890)

Usage:
    python tools/post_to_telegram.py [--max-posts 5] [--dry-run]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

DATA_FILE    = Path(".tmp/aggregated_slots.json")
POSTED_FILE  = Path(".tmp/telegram_posted_ids.json")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Category icons for Telegram messages
CAT_ICON = {
    "wellness":              "💆",
    "beauty":                "💅",
    "hospitality":           "🏠",
    "home_services":         "🔧",
    "professional_services": "💼",
    "experiences":           "🎯",
    "events":                "🎟",
}

CAT_LABEL = {
    "wellness":              "Wellness & Fitness",
    "beauty":                "Beauty & Salon",
    "hospitality":           "Short-term Stay",
    "home_services":         "Home Services",
    "professional_services": "Professional Services",
    "experiences":           "Tour / Experience",
    "events":                "Event / Experience",
}


def tg_api(token: str, method: str, payload: dict) -> dict:
    url  = TELEGRAM_API.format(token=token, method=method)
    resp = requests.post(url, json=payload, timeout=15)
    return resp.json()


def format_slot_message(slot: dict, landing_url: str) -> str:
    """Format a single slot as a Telegram message (HTML parse mode)."""
    cat      = slot.get("category", "events")
    icon     = CAT_ICON.get(cat, "🎯")
    label    = CAT_LABEL.get(cat, cat.title())
    name     = slot.get("service_name", "Deal")
    business = slot.get("business_name", "") or ""
    if "@" in business or business.lower().startswith("for venue details"):
        business = ""
    city     = slot.get("location_city", "")
    state    = slot.get("location_state", "")
    hours    = slot.get("hours_until_start")
    price    = slot.get("our_price") or slot.get("price")

    # Format start time
    start_str = ""
    try:
        start_dt  = datetime.fromisoformat(slot["start_time"].replace("Z", "+00:00"))
        start_str = start_dt.strftime("%a %b %d at %I:%M %p UTC").replace(" 0", " ")
    except Exception:
        pass

    # Urgency line
    if hours is not None:
        if hours <= 6:
            urgency = "⚡️ <b>LAST CHANCE</b> — starts in under 6 hours!"
        elif hours <= 12:
            urgency = "🔥 <b>TODAY ONLY</b> — starts in {:.0f} hours".format(hours)
        elif hours <= 24:
            urgency = "⏰ Ends tomorrow — {:.0f}h away".format(hours)
        else:
            urgency = "📅 Available now — {:.0f}h away".format(hours)
    else:
        urgency = "📅 Available now"

    # Price line
    if price is None:
        price_line = "💰 Price at door"
    elif float(price) == 0:
        price_line = "💰 <b>Free</b>"
    else:
        price_line = f"💰 <b>${float(price):.0f}</b>"

    # Build book URL — use landing page if available, otherwise booking_url
    # NOTE: we never expose the source platform URL directly
    if landing_url:
        book_url = f"{landing_url.rstrip('/')}/#deals"
    else:
        book_url = slot.get("booking_url", "")

    lines = [
        f"{icon} <b>{name}</b>",
        f"📍 {(business + ' — ') if business else ''}{city}, {state}",
        f"🕐 {start_str}",
        urgency,
        price_line,
    ]

    if book_url:
        lines.append(f'\n<a href="{book_url}">Book Now →</a>')

    return "\n".join(lines)


def select_posts(slots: list[dict], posted_ids: set, max_posts: int) -> list[dict]:
    """
    Select the best slots to post:
    - Not already posted
    - Sorted by urgency (soonest first), then by having a price
    - Max max_posts slots
    """
    candidates = [
        s for s in slots
        if s.get("slot_id") not in posted_ids
        and s.get("hours_until_start") is not None
        and s.get("hours_until_start") <= 72
        and (s.get("our_price") is not None or s.get("price") is not None)
    ]

    # Sort: soonest first, price known preferred
    candidates.sort(key=lambda s: (
        s.get("hours_until_start") or 999,
        0 if s.get("our_price") or s.get("price") else 1,
    ))

    return candidates[:max_posts]


def load_posted_ids() -> set:
    if POSTED_FILE.exists():
        try:
            return set(json.loads(POSTED_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_posted_ids(posted_ids: set) -> None:
    # Keep only the last 2000 IDs to prevent unbounded growth
    ids_list = list(posted_ids)[-2000:]
    POSTED_FILE.write_text(json.dumps(ids_list), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Post deals to Telegram channel")
    parser.add_argument("--max-posts", type=int, default=5,
                        help="Max new deals to post per run (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print messages without sending")
    args = parser.parse_args()

    token      = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
    landing_url = os.getenv("LANDING_PAGE_URL", "").strip()

    if not token or not channel_id:
        print("Telegram not configured — skipping.")
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID in .env")
        print()
        print("Setup steps:")
        print("  1. Message @BotFather on Telegram -> /newbot")
        print("  2. Name: 'Last Minute Deals Bot', username: lastminutedeals_bot")
        print("  3. Copy the token -> TELEGRAM_BOT_TOKEN in .env")
        print("  4. Create a channel (e.g. @lastminutedeals_us)")
        print("  5. Add your bot as admin to the channel")
        print("  6. Set TELEGRAM_CHANNEL_ID=@lastminutedeals_us in .env")
        return

    if not DATA_FILE.exists():
        print(f"No slot data found at {DATA_FILE}. Run the pipeline first.")
        return

    slots      = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    posted_ids = load_posted_ids()
    to_post    = select_posts(slots, posted_ids, args.max_posts)

    if not to_post:
        print(f"Telegram: no new slots to post (all {len(slots)} already posted or 0 available).")
        return

    print(f"Telegram: posting {len(to_post)} deals to {channel_id}...")

    sent = 0
    for slot in to_post:
        msg = format_slot_message(slot, landing_url)

        if args.dry_run:
            print(f"\n--- DRY RUN ---\n{msg}\n")
            posted_ids.add(slot["slot_id"])
            sent += 1
            continue

        result = tg_api(token, "sendMessage", {
            "chat_id":    channel_id,
            "text":       msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })

        if result.get("ok"):
            posted_ids.add(slot["slot_id"])
            sent += 1
            print(f"  Sent: {slot.get('service_name', '')[:50]} ({slot.get('location_city')})")
        else:
            print(f"  ERROR: {result.get('description', result)}")

        # Telegram rate limit: 30 messages/second — 1s delay is safe
        if sent < len(to_post):
            time.sleep(1)

    save_posted_ids(posted_ids)
    print(f"Telegram: {sent}/{len(to_post)} messages sent.")


if __name__ == "__main__":
    main()
