"""
send_sms_alert.py — Send SMS deal alerts to opted-in subscribers via Twilio.

Subscribers opt in on the landing page (name, phone, city, category preferences).
Their preferences are stored in .tmp/sms_subscribers.json.
This tool sends a matching deal alert when new slots are found.

Twilio pricing: ~$0.0079/SMS (US). At 5 alerts/subscriber/day, that's
$0.04/day per active subscriber. Even 1,000 subscribers = $40/day — well
justified by conversion rate (SMS has 98% open rate vs email's 20%).

Required .env:
  TWILIO_ACCOUNT_SID=
  TWILIO_AUTH_TOKEN=
  TWILIO_FROM_NUMBER=+1...   (your Twilio number)
  LANDING_PAGE_URL=          (included in SMS for booking link)

Usage:
  python tools/send_sms_alert.py [--dry-run] [--max-per-run 50]
  python tools/send_sms_alert.py --subscribe +15550001234 --city "New York" --categories events,wellness
  python tools/send_sms_alert.py --unsubscribe +15550001234
  python tools/send_sms_alert.py --list-subscribers
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

DATA_FILE   = Path(".tmp/aggregated_slots.json")
SUBS_FILE   = Path(".tmp/sms_subscribers.json")
SENT_FILE   = Path(".tmp/sms_sent_log.json")

# Don't send more than this many SMSes per phone per day
MAX_SMS_PER_PHONE_PER_DAY = 3


def load_subscribers() -> list[dict]:
    if SUBS_FILE.exists():
        try:
            return json.loads(SUBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_subscribers(subs: list[dict]) -> None:
    SUBS_FILE.write_text(json.dumps(subs, indent=2), encoding="utf-8")


def load_sent_log() -> dict:
    """Returns {phone: [iso_timestamp, ...]} for today's sends."""
    if SENT_FILE.exists():
        try:
            return json.loads(SENT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_sent_log(log: dict) -> None:
    SENT_FILE.write_text(json.dumps(log), encoding="utf-8")


def sent_today(log: dict, phone: str) -> int:
    """Return how many SMSes have been sent to this phone today."""
    today = datetime.now(timezone.utc).date().isoformat()
    timestamps = log.get(phone, [])
    return sum(1 for t in timestamps if t.startswith(today))


def record_send(log: dict, phone: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if phone not in log:
        log[phone] = []
    log[phone].append(now)
    # Prune old entries (keep only last 7 days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    log[phone] = [t for t in log[phone] if t >= cutoff]


def format_sms(slot: dict, landing_url: str) -> str:
    """Format a deal as a compact SMS (160 chars recommended)."""
    name  = slot.get("service_name", "Deal")[:40]
    city  = slot.get("location_city", "")
    hours = slot.get("hours_until_start")
    price = slot.get("our_price") or slot.get("price")

    if hours is not None:
        if hours <= 6:
            urgency = "LAST CHANCE"
        elif hours <= 12:
            urgency = "Today only"
        elif hours <= 24:
            urgency = "Tomorrow"
        else:
            urgency = f"{hours:.0f}h away"
    else:
        urgency = "Now available"

    price_str = f"${float(price):.0f}" if price and float(price) > 0 else "Free"

    link = landing_url.rstrip("/") if landing_url else "https://lastminutedeals.netlify.app"

    msg = f"LastMinuteDeals: {urgency} - {name} in {city} for {price_str}. Book: {link}"
    return msg[:160]


def matches_subscriber(slot: dict, sub: dict) -> bool:
    """Check if a slot matches a subscriber's preferences."""
    # City match (partial, case-insensitive)
    sub_city = (sub.get("city") or "").lower().strip()
    slot_city = (slot.get("location_city") or "").lower()
    if sub_city and sub_city not in slot_city:
        return False

    # Category match
    sub_cats = sub.get("categories") or []
    if sub_cats and slot.get("category") not in sub_cats:
        return False

    # Must have a price
    price = slot.get("our_price") or slot.get("price")
    if price is None:
        return False

    return True


def send_sms_twilio(phone: str, message: str, account_sid: str, auth_token: str, from_number: str) -> dict:
    """Send SMS via Twilio REST API."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    resp = requests.post(
        url,
        data={"From": from_number, "To": phone, "Body": message},
        auth=(account_sid, auth_token),
        timeout=15,
    )
    return resp.json()


def run_alerts(dry_run: bool = False, max_per_run: int = 50) -> None:
    account_sid  = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token   = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number  = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    landing_url  = os.getenv("LANDING_PAGE_URL", "").strip()

    if not (account_sid and auth_token and from_number):
        print("Twilio not configured — skipping SMS alerts.")
        print("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER in .env")
        print()
        print("Sign up free at twilio.com (trial gives $15 credit ~ 1900 SMSes)")
        return

    subs = load_subscribers()
    if not subs:
        print("SMS: No subscribers yet. Add subscribers via landing page opt-in form.")
        return

    if not DATA_FILE.exists():
        print(f"No slot data at {DATA_FILE}. Run pipeline first.")
        return

    slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    # Filter to slots starting within 24h (high urgency only for SMS)
    urgent = [
        s for s in slots
        if s.get("hours_until_start") is not None
        and s.get("hours_until_start") <= 24
        and (s.get("our_price") or s.get("price")) is not None
    ]
    urgent.sort(key=lambda s: s.get("hours_until_start") or 9999)

    sent_log = load_sent_log()
    total_sent = 0

    print(f"SMS alerts: {len(subs)} subscribers, {len(urgent)} urgent slots (<24h)")

    for sub in subs:
        if total_sent >= max_per_run:
            print(f"  Reached max_per_run={max_per_run}. Stopping.")
            break

        phone = sub.get("phone", "").strip()
        if not phone:
            continue

        # Daily send limit
        if sent_today(sent_log, phone) >= MAX_SMS_PER_PHONE_PER_DAY:
            continue

        # Find the best matching slot for this subscriber
        match = next((s for s in urgent if matches_subscriber(s, sub)), None)
        if not match:
            continue

        msg = format_sms(match, landing_url)

        if dry_run:
            print(f"  DRY RUN -> {phone}: {msg}")
            total_sent += 1
            continue

        result = send_sms_twilio(phone, msg, account_sid, auth_token, from_number)
        if result.get("sid"):
            record_send(sent_log, phone)
            total_sent += 1
            print(f"  Sent to {phone[-4:]}: {match.get('service_name', '')[:40]}")
        else:
            err = result.get("message") or result.get("code") or result
            print(f"  ERROR for {phone[-4:]}: {err}")

        time.sleep(0.1)  # Twilio rate limit is generous but be polite

    save_sent_log(sent_log)
    print(f"SMS alerts: {total_sent} messages sent.")


def subscribe(phone: str, city: str, categories: list[str]) -> None:
    subs = load_subscribers()
    # Update if exists, add if new
    for sub in subs:
        if sub.get("phone") == phone:
            sub["city"] = city
            sub["categories"] = categories
            sub["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_subscribers(subs)
            print(f"Updated subscription for {phone}")
            return

    subs.append({
        "phone":      phone,
        "city":       city,
        "categories": categories,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_subscribers(subs)
    print(f"Subscribed {phone} (city={city or 'any'}, categories={categories or 'all'})")


def unsubscribe(phone: str) -> None:
    subs = load_subscribers()
    before = len(subs)
    subs = [s for s in subs if s.get("phone") != phone]
    save_subscribers(subs)
    removed = before - len(subs)
    print(f"Removed {removed} subscription(s) for {phone}")


def main():
    parser = argparse.ArgumentParser(description="Send SMS deal alerts to subscribers")
    parser.add_argument("--dry-run",  action="store_true", help="Print without sending")
    parser.add_argument("--max-per-run", type=int, default=50, help="Max SMSes to send")
    parser.add_argument("--subscribe",   help="Subscribe a phone number")
    parser.add_argument("--unsubscribe", help="Unsubscribe a phone number")
    parser.add_argument("--city",        default="", help="City for subscribe")
    parser.add_argument("--categories",  default="", help="Comma-separated categories")
    parser.add_argument("--list-subscribers", action="store_true")
    args = parser.parse_args()

    if args.subscribe:
        cats = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else []
        subscribe(args.subscribe, args.city, cats)
        return

    if args.unsubscribe:
        unsubscribe(args.unsubscribe)
        return

    if args.list_subscribers:
        subs = load_subscribers()
        print(f"{len(subs)} subscriber(s):")
        for s in subs:
            print(f"  {s['phone']} | city={s.get('city', 'any')} | cats={s.get('categories', 'all')}")
        return

    run_alerts(dry_run=args.dry_run, max_per_run=args.max_per_run)


if __name__ == "__main__":
    main()
