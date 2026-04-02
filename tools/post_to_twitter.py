"""
post_to_twitter.py -- Post new deals to Twitter/X via API v2.

Posts the top N new/updated deals as tweets. Links to the landing page.
Tracks posted slot IDs in .tmp/twitter_posted_ids.json to avoid spam.

Prerequisites:
    1. Apply for Twitter Developer access at developer.twitter.com
    2. Create a project + app with OAuth 1.0a User Context (read/write)
    3. Generate Access Token + Secret for your account
    4. Add to .env:
         TWITTER_API_KEY=<Consumer Key>
         TWITTER_API_SECRET=<Consumer Secret>
         TWITTER_ACCESS_TOKEN=<Access Token>
         TWITTER_ACCESS_TOKEN_SECRET=<Access Token Secret>

Free tier allows 1,500 tweets/month (50/day).

Usage:
    python tools/post_to_twitter.py [--max-posts 3] [--dry-run]
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

DATA_FILE   = Path(".tmp/aggregated_slots.json")
POSTED_FILE = Path(".tmp/twitter_posted_ids.json")

TWITTER_V2_TWEET = "https://api.twitter.com/2/tweets"

# Category hashtags for tweet copy
CAT_HASHTAGS = {
    "wellness":              "#Wellness #Fitness #LastMinute",
    "beauty":                "#Beauty #Salon #LastMinute",
    "hospitality":           "#Travel #Hotel #LastMinute",
    "home_services":         "#HomeServices #LastMinute",
    "professional_services": "#Professional #LastMinute",
    "events":                "#Events #LastMinute",
}

CAT_ICON = {
    "wellness":              "💆",
    "beauty":                "💅",
    "hospitality":           "🏠",
    "home_services":         "🔧",
    "professional_services": "💼",
    "events":                "🎟",
}


# ── OAuth 1.0a request signing ─────────────────────────────────────────────
def _oauth_header(method: str, url: str, params: dict,
                  api_key: str, api_secret: str,
                  access_token: str, access_token_secret: str) -> str:
    """Build the Authorization header for OAuth 1.0a."""
    nonce     = b64encode(os.urandom(32)).decode().rstrip("=")
    timestamp = str(int(time.time()))

    oauth_params = {
        "oauth_consumer_key":     api_key,
        "oauth_nonce":            nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        timestamp,
        "oauth_token":            access_token,
        "oauth_version":          "1.0",
    }

    all_params = {**params, **oauth_params}
    sorted_params = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )

    base = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote(sorted_params, safe=""),
    ])

    signing_key = f"{urllib.parse.quote(api_secret, safe='')}&{urllib.parse.quote(access_token_secret, safe='')}"
    sig = b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()

    oauth_params["oauth_signature"] = sig
    header_parts = ", ".join(
        f'{k}="{urllib.parse.quote(str(v), safe="")}"'
        for k, v in sorted(oauth_params.items())
    )
    return f"OAuth {header_parts}"


def post_tweet(text: str, api_key: str, api_secret: str,
               access_token: str, access_secret: str) -> dict:
    """Post a single tweet via Twitter API v2."""
    payload = {"text": text}
    auth_header = _oauth_header(
        "POST", TWITTER_V2_TWEET, {},
        api_key, api_secret, access_token, access_secret
    )
    resp = requests.post(
        TWITTER_V2_TWEET,
        json=payload,
        headers={
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "User-Agent": "LastMinuteDealBot/1.0",
        },
        timeout=15,
    )
    return resp.json()


def format_tweet(slot: dict, landing_url: str) -> str:
    """Format a slot as a tweet (<= 280 chars including URL)."""
    cat    = slot.get("category", "events")
    icon   = CAT_ICON.get(cat, "🎯")
    name   = slot.get("service_name", "Deal")[:55]
    city   = slot.get("location_city", "")
    state  = slot.get("location_state", "")
    hours  = slot.get("hours_until_start")
    price  = slot.get("our_price") or slot.get("price")
    tags   = CAT_HASHTAGS.get(cat, "#LastMinute")

    # Location hashtags from channel directory (more specific than generic city tag)
    city_hashtags = []
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent))
        from route_distribution import get_channels_for_slot
        channels = get_channels_for_slot(slot)
        city_hashtags = ["#" + h for h in channels.get("twitter_hashtags", [])[:2]]
    except Exception:
        pass
    city_tag = " ".join(city_hashtags) if city_hashtags else (
        f"#{city.replace(' ', '').replace('.', '')}" if city else ""
    )

    # Urgency
    if hours is not None:
        if hours <= 6:
            urgency = "LAST CHANCE"
        elif hours <= 24:
            urgency = f"TODAY - {hours:.0f}h away"
        else:
            urgency = f"{hours:.0f}h away"
    else:
        urgency = "Available now"

    # Price
    if price is not None:
        price_str = f"from ${price:.0f}"
    else:
        price_str = "free / pay at door"

    # Build URL (Twitter counts all URLs as 23 chars via t.co shortener)
    link = f"{landing_url.rstrip('/')}/#deals" if landing_url else ""

    location_str = f"{city}, {state}" if city and state else city or state

    tweet = (
        f"{icon} {name}\n"
        f"📍 {location_str}\n"
        f"⏰ {urgency} | {price_str}\n"
        f"{tags} {city_tag}\n"
    )

    if link:
        tweet += f"\n{link}"

    return tweet[:280]


def select_posts(slots: list, posted_ids: set, max_posts: int) -> list:
    """Select unposted, most urgent slots."""
    candidates = [
        s for s in slots
        if s.get("slot_id") not in posted_ids
        and s.get("hours_until_start") is not None
        and s.get("hours_until_start") <= 72
    ]
    candidates.sort(key=lambda s: (
        s.get("hours_until_start") or 999,
        0 if (s.get("our_price") or s.get("price")) else 1,
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
    ids_list = list(posted_ids)[-3000:]
    POSTED_FILE.write_text(json.dumps(ids_list), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Post deals to Twitter/X")
    parser.add_argument("--max-posts", type=int, default=3,
                        help="Max new tweets per run (default: 3, free tier safe)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print tweets without posting")
    args = parser.parse_args()

    api_key      = os.getenv("TWITTER_API_KEY", "").strip()
    api_secret   = os.getenv("TWITTER_API_SECRET", "").strip()
    access_token = os.getenv("TWITTER_ACCESS_TOKEN", "").strip()
    access_secret= os.getenv("TWITTER_ACCESS_TOKEN_SECRET", "").strip()
    landing_url  = os.getenv("LANDING_PAGE_URL", "").strip()

    if not all([api_key, api_secret, access_token, access_secret]):
        print("Twitter not configured -- skipping.")
        print("Set TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN,")
        print("    TWITTER_ACCESS_TOKEN_SECRET in .env")
        print()
        print("Setup: developer.twitter.com -> New App -> OAuth 1.0a -> read+write")
        return

    if not DATA_FILE.exists():
        print(f"No slot data at {DATA_FILE}. Run pipeline first.")
        return

    slots      = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    posted_ids = load_posted_ids()
    to_post    = select_posts(slots, posted_ids, args.max_posts)

    if not to_post:
        print(f"Twitter: no new slots to post ({len(slots)} total, all posted or none available).")
        return

    print(f"Twitter: posting {len(to_post)} tweets...")
    sent = 0

    for slot in to_post:
        tweet_text = format_tweet(slot, landing_url)

        if args.dry_run:
            print(f"\n--- DRY RUN ({len(tweet_text)} chars) ---\n{tweet_text}\n")
            posted_ids.add(slot["slot_id"])
            sent += 1
            continue

        result = post_tweet(tweet_text, api_key, api_secret, access_token, access_secret)

        if result.get("data", {}).get("id"):
            posted_ids.add(slot["slot_id"])
            sent += 1
            tweet_id = result["data"]["id"]
            print(f"  Posted: {slot.get('service_name', '')[:50]} "
                  f"({slot.get('location_city')}) -> tweet/{tweet_id}")
        else:
            err = result.get("errors") or result.get("detail") or result
            print(f"  ERROR: {err}")
            # On rate limit (429), stop posting
            if "rate limit" in str(err).lower() or result.get("status") == 429:
                print("  Rate limited -- stopping.")
                break

        if sent < len(to_post):
            time.sleep(2)  # Stay well under 1 req/sec

    save_posted_ids(posted_ids)
    print(f"Twitter: {sent}/{len(to_post)} tweets sent.")


if __name__ == "__main__":
    main()
