"""
post_to_reddit.py -- Post deals to relevant Reddit communities via PRAW.

Posts to a combination of:
  - City-specific subreddits (r/nyc, r/chicago, etc.)
  - Category subreddits (r/deals, r/Frugal, r/travel)
  - Specialty subreddits (r/massage, r/fitness, r/AirBnB)

Links to landing page ONLY (never direct affiliate links -- Reddit bans those).
Rate limit: max 1 post per subreddit per 6 hours to avoid spam detection.
Tracks all post history in .tmp/reddit_post_history.json.

Prerequisites:
    1. Create a Reddit account for the bot
    2. Go to reddit.com/prefs/apps -> Create Another App -> Script
    3. Add to .env:
         REDDIT_CLIENT_ID=<from app page>
         REDDIT_CLIENT_SECRET=<from app page>
         REDDIT_USERNAME=<your bot's reddit username>
         REDDIT_PASSWORD=<your bot's reddit password>
         REDDIT_USER_AGENT=LastMinuteDealsBot/1.0 by u/<your_username>

Usage:
    python tools/post_to_reddit.py [--max-posts 5] [--dry-run]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

DATA_FILE    = Path(".tmp/aggregated_slots.json")
HISTORY_FILE = Path(".tmp/reddit_post_history.json")

# How long to wait before posting to the same subreddit again (hours)
SUBREDDIT_COOLDOWN_H = 6

# City -> subreddit mapping (city name lowercase, no spaces)
CITY_SUBREDDITS = {
    "new york":      ["nyc", "newyorkcity"],
    "los angeles":   ["LosAngeles", "LAlist"],
    "chicago":       ["chicago"],
    "houston":       ["houston"],
    "phoenix":       ["phoenix"],
    "philadelphia":  ["philadelphia"],
    "san antonio":   ["sanantonio"],
    "san diego":     ["sandiego"],
    "dallas":        ["dallas"],
    "san jose":      ["SanJose"],
    "austin":        ["Austin"],
    "jacksonville":  ["jacksonville"],
    "columbus":      ["Columbus"],
    "charlotte":     ["Charlotte"],
    "san francisco": ["sanfrancisco", "bayarea"],
    "indianapolis":  ["Indianapolis"],
    "seattle":       ["Seattle"],
    "denver":        ["Denver"],
    "washington":    ["washingtondc", "nova"],
    "nashville":     ["nashville"],
    "boston":        ["boston"],
    "portland":      ["Portland"],
    "las vegas":     ["LasVegas"],
    "memphis":       ["memphis"],
    "baltimore":     ["baltimore"],
    "milwaukee":     ["milwaukee"],
    "atlanta":       ["Atlanta"],
    "kansas city":   ["kansascity"],
    "raleigh":       ["raleigh"],
    "minneapolis":   ["minneapolis"],
    "tampa":         ["tampa"],
    "new orleans":   ["NewOrleans"],
    "cleveland":     ["Cleveland"],
    "sacramento":    ["Sacramento"],
    "pittsburgh":    ["pittsburgh"],
    "orlando":       ["orlando"],
    "miami":         ["Miami"],
    "detroit":       ["Detroit"],
}

# Category -> subreddits (always included when category matches)
CATEGORY_SUBREDDITS = {
    "wellness":              ["Fitness", "yoga", "bodyweightfitness"],
    "beauty":                ["beauty", "Hair"],
    "hospitality":           ["travel", "AirBnB", "solotravel"],
    "home_services":         ["HomeImprovement", "DIY"],
    "professional_services": ["careerguidance", "Entrepreneur"],
    "events":                ["ifyoulikeblank", "Concerts"],
}

# Always-on national deal subreddits
NATIONAL_SUBREDDITS = ["deals", "Frugal"]

# Max subreddits per slot per run
MAX_SUBREDDITS_PER_SLOT = 3

# Category icons / labels for post copy
CAT_ICON = {
    "wellness":              "💆",
    "beauty":                "💅",
    "hospitality":           "🏠",
    "home_services":         "🔧",
    "professional_services": "💼",
    "events":                "🎟",
}


def _load_history() -> dict:
    """Load post history: {subreddit: [timestamp_str, ...]}"""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_history(history: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def _can_post_to(subreddit: str, history: dict) -> bool:
    """Return True if we haven't posted to this subreddit within SUBREDDIT_COOLDOWN_H hours."""
    posts = history.get(subreddit, [])
    if not posts:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SUBREDDIT_COOLDOWN_H)
    recent = [p for p in posts if datetime.fromisoformat(p) > cutoff]
    return len(recent) == 0


def _record_post(subreddit: str, history: dict) -> None:
    now_str = datetime.now(timezone.utc).isoformat()
    if subreddit not in history:
        history[subreddit] = []
    history[subreddit].append(now_str)
    # Keep only last 100 records per subreddit
    history[subreddit] = history[subreddit][-100:]


def _get_target_subreddits(slot: dict, history: dict) -> list[str]:
    """Get the list of subreddits to post a slot to, respecting cooldowns."""
    # Try channel directory first (covers more cities)
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent))
        from route_distribution import get_channels_for_slot
        channels = get_channels_for_slot(slot)
        all_subs = channels.get("subreddits", [])
    except Exception:
        # Fallback to local dict
        city = (slot.get("location_city") or "").lower()
        cat  = slot.get("category", "events")
        all_subs = (
            CITY_SUBREDDITS.get(city, [])[:2]
            + CATEGORY_SUBREDDITS.get(cat, [])[:1]
            + NATIONAL_SUBREDDITS[:1]
        )

    subs = []
    for sub in all_subs:
        if _can_post_to(sub, history):
            subs.append(sub)
        if len(subs) >= MAX_SUBREDDITS_PER_SLOT:
            break

    return subs


def _format_post_title(slot: dict) -> str:
    """Format a Reddit post title (<= 300 chars)."""
    name   = slot.get("service_name", "Deal")[:80]
    city   = slot.get("location_city", "")
    state  = slot.get("location_state", "")
    hours  = slot.get("hours_until_start")
    price  = slot.get("our_price") or slot.get("price")
    cat    = slot.get("category", "events")
    icon   = CAT_ICON.get(cat, "🎯")

    if hours is not None:
        if hours <= 6:
            urgency = "LAST CHANCE -- starts in under 6 hours"
        elif hours <= 24:
            urgency = f"today ({hours:.0f}h away)"
        else:
            urgency = f"{hours:.0f}h away"
    else:
        urgency = "available now"

    price_str = f"${price:.0f}" if price is not None else "free/PWYW"
    loc       = f"{city}, {state}" if city and state else city or state

    title = f"{icon} Last-minute deal: {name} in {loc} -- {urgency} -- from {price_str}"
    return title[:300]


def _format_post_body(slot: dict, landing_url: str) -> str:
    """Format a Reddit post body in Markdown."""
    cat      = slot.get("category", "events")
    icon     = CAT_ICON.get(cat, "🎯")
    name     = slot.get("service_name", "Deal")
    business = slot.get("business_name", "") or ""
    if "@" in business or business.lower().startswith("for venue details"):
        business = ""
    city     = slot.get("location_city", "")
    state    = slot.get("location_state", "")
    hours    = slot.get("hours_until_start")
    price    = slot.get("our_price") or slot.get("price")

    # Start time
    start_str = ""
    try:
        from datetime import datetime as dt
        start_iso = slot.get("start_time", "")
        if start_iso.endswith("Z"):
            start_iso = start_iso[:-1] + "+00:00"
        start_dt  = dt.fromisoformat(start_iso)
        start_str = start_dt.strftime("%A, %B %d at %I:%M %p UTC").replace(" 0", " ")
    except Exception:
        pass

    urgency_line = ""
    if hours is not None:
        if hours <= 6:
            urgency_line = "> **LAST CHANCE -- starting in under 6 hours!**\n\n"
        elif hours <= 24:
            urgency_line = f"> Starting today -- {hours:.0f} hours from now.\n\n"
        else:
            urgency_line = f"> Coming up in {hours:.0f} hours.\n\n"

    price_line = f"**Price:** ${price:.0f}" if price else "**Price:** Free / Pay at door"
    location   = f"{business} -- {city}, {state}" if business else f"{city}, {state}"

    link = f"{landing_url.rstrip('/')}/#deals" if landing_url else ""
    link_line  = f"\n[See all last-minute deals]({link})\n" if link else ""

    body = (
        f"{icon} **{name}**\n\n"
        f"**Location:** {location}\n"
        f"**When:** {start_str}\n"
        f"{price_line}\n\n"
        f"{urgency_line}"
        f"---\n"
        f"*Found via our automated last-minute deals aggregator.*\n"
        f"{link_line}"
        f"\n*Not affiliated with the venue. Book directly through the link above.*"
    )
    return body


def main():
    parser = argparse.ArgumentParser(description="Post deals to Reddit")
    parser.add_argument("--max-posts", type=int, default=5,
                        help="Max posts per run (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print posts without submitting")
    args = parser.parse_args()

    client_id     = os.getenv("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    username      = os.getenv("REDDIT_USERNAME", "").strip()
    password      = os.getenv("REDDIT_PASSWORD", "").strip()
    user_agent    = os.getenv("REDDIT_USER_AGENT", "LastMinuteDealsBot/1.0").strip()
    landing_url   = os.getenv("LANDING_PAGE_URL", "").strip()

    if not all([client_id, client_secret, username, password]):
        print("Reddit not configured -- skipping.")
        print("Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME,")
        print("    REDDIT_PASSWORD in .env")
        print()
        print("Setup: reddit.com/prefs/apps -> Create Another App -> Script")
        return

    if not DATA_FILE.exists():
        print(f"No slot data at {DATA_FILE}. Run pipeline first.")
        return

    # Import PRAW
    try:
        import praw
    except ImportError:
        print("PRAW not installed. Run: pip install praw")
        return

    slots   = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    history = _load_history()

    # Filter slots that have subreddits to post to
    candidates = [
        s for s in slots
        if s.get("hours_until_start") is not None
        and s.get("hours_until_start") <= 72
        and s.get("hours_until_start") >= 0
    ]
    candidates.sort(key=lambda s: s.get("hours_until_start") or 999)

    if not candidates:
        print("Reddit: no slots in window to post.")
        return

    # Connect to Reddit
    if not args.dry_run:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            user_agent=user_agent,
        )

    sent  = 0
    seen_slots: set[str] = set()

    for slot in candidates:
        if sent >= args.max_posts:
            break

        slot_id = slot.get("slot_id", "")
        if slot_id in seen_slots:
            continue

        subreddits = _get_target_subreddits(slot, history)
        if not subreddits:
            continue

        seen_slots.add(slot_id)
        title = _format_post_title(slot)
        body  = _format_post_body(slot, landing_url)

        for sub in subreddits:
            if sent >= args.max_posts:
                break

            if args.dry_run:
                print(f"\n--- DRY RUN: r/{sub} ---")
                print(f"TITLE: {title}")
                print(f"BODY:\n{body[:400]}...")
                _record_post(sub, history)
                sent += 1
                continue

            try:
                subreddit_obj = reddit.subreddit(sub)
                submission    = subreddit_obj.submit(title=title, selftext=body)
                print(f"  Posted to r/{sub}: {submission.shortlink}")
                _record_post(sub, history)
                sent += 1
            except Exception as exc:
                err_str = str(exc).lower()
                if "banned" in err_str or "forbidden" in err_str:
                    print(f"  r/{sub}: banned or restricted -- skipping forever")
                    _record_post(sub, history)  # record to prevent retries
                elif "ratelimit" in err_str or "rate limit" in err_str:
                    print(f"  r/{sub}: rate limited -- stopping")
                    break
                else:
                    print(f"  r/{sub}: ERROR -- {exc}")

            time.sleep(2)  # Reddit recommends >= 2s between requests

    _save_history(history)
    print(f"Reddit: {sent} posts submitted.")


if __name__ == "__main__":
    main()
