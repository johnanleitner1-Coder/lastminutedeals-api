"""
notify_webhooks.py — Fire webhook callbacks for new matching deals.

Run at end of each pipeline cycle (after sync_to_supabase.py).
Loads webhook subscriptions and POSTs matching new slots to each callback URL.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

DATA_FILE    = Path(".tmp/aggregated_slots.json")
WEBHOOKS_FILE = Path(".tmp/webhook_subscriptions.json")
NOTIFIED_FILE = Path(".tmp/webhooks_last_notified.json")
MAX_SLOTS_PER_CALL = 20  # Max slots per webhook payload


def load_subscriptions() -> dict:
    if not WEBHOOKS_FILE.exists():
        return {}
    try:
        return json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_last_notified() -> dict:
    """Returns {subscription_id: set_of_slot_ids_already_notified}"""
    if not NOTIFIED_FILE.exists():
        return {}
    try:
        data = json.loads(NOTIFIED_FILE.read_text(encoding="utf-8"))
        return {k: set(v) for k, v in data.items()}
    except Exception:
        return {}


def save_last_notified(notified: dict) -> None:
    NOTIFIED_FILE.write_text(
        json.dumps({k: list(v) for k, v in notified.items()}, indent=2),
        encoding="utf-8",
    )


def slot_matches(slot: dict, filters: dict) -> bool:
    city        = (filters.get("city") or "").lower()
    category    = (filters.get("category") or "").lower()
    max_price   = filters.get("max_price")
    hours_ahead = filters.get("hours_ahead")

    if city and city not in (slot.get("location_city") or "").lower():
        return False
    if category and slot.get("category", "").lower() != category:
        return False
    price = slot.get("our_price") or slot.get("price") or 0
    if max_price is not None and float(price) > float(max_price):
        return False
    h = slot.get("hours_until_start")
    if hours_ahead is not None and (h is None or h > float(hours_ahead)):
        return False
    return True


def fire_webhook(callback_url: str, sub_id: str, slots: list) -> bool:
    payload = {
        "subscription_id": sub_id,
        "event": "new_deals",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "deal_count": len(slots),
        "deals": slots,
    }
    try:
        resp = requests.post(callback_url, json=payload, timeout=10)
        return resp.status_code < 400
    except Exception as e:
        print(f"  Webhook {sub_id} failed: {e}")
        return False


def main():
    if not DATA_FILE.exists():
        print("No slot data. Run pipeline first.")
        return

    slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    subs  = load_subscriptions()

    if not subs:
        print("No webhook subscriptions.")
        return

    print(f"Notifying {len(subs)} webhook subscriptions for {len(slots)} slots...")

    last_notified = load_last_notified()
    fired = 0

    for sub_id, sub in subs.items():
        if not sub.get("active", True):
            continue

        callback_url = sub.get("callback_url", "")
        filters      = sub.get("filters") or {}
        already_seen = last_notified.get(sub_id, set())

        # Find new matching slots not yet notified
        new_matches = [
            {
                "slot_id":          s.get("slot_id"),
                "service_name":     s.get("service_name"),
                "category":         s.get("category"),
                "location_city":    s.get("location_city"),
                "location_state":   s.get("location_state"),
                "start_time":       s.get("start_time"),
                "hours_until_start": s.get("hours_until_start"),
                "our_price":        s.get("our_price") or s.get("price"),
                "currency":         s.get("currency", "USD"),
            }
            for s in slots
            if slot_matches(s, filters) and s.get("slot_id") not in already_seen
        ]

        if not new_matches:
            continue

        # Limit payload size
        to_send = new_matches[:MAX_SLOTS_PER_CALL]
        ok = fire_webhook(callback_url, sub_id, to_send)

        if ok:
            if sub_id not in last_notified:
                last_notified[sub_id] = set()
            last_notified[sub_id].update(s["slot_id"] for s in to_send)
            fired += 1
            print(f"  Fired {sub_id}: {len(to_send)} new deals to {callback_url}")
        else:
            print(f"  Failed {sub_id}: {callback_url}")

    save_last_notified(last_notified)
    print(f"Webhook notifications complete: {fired}/{len(subs)} fired.")


if __name__ == "__main__":
    main()
