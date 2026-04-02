"""
sync_to_supabase.py — Upsert aggregated slots into Supabase.

Run after aggregate_slots.py + compute_pricing.py on every pipeline cycle.
The booking server on Railway reads from Supabase instead of local .tmp/ files.

Usage:
    python tools/sync_to_supabase.py
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

DATA_FILE = Path(".tmp/aggregated_slots.json")
BATCH_SIZE = 500  # Supabase REST upsert limit per request


def get_headers():
    secret = os.getenv("SUPABASE_SECRET_KEY", "")
    return {
        "apikey": secret,
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",  # upsert on conflict
    }


def slot_to_row(slot: dict) -> dict:
    """Convert a slot dict to a Supabase row."""
    return {
        "slot_id":           slot.get("slot_id", ""),
        "platform":          slot.get("platform", ""),
        "business_name":     slot.get("business_name", ""),
        "category":          slot.get("category", ""),
        "service_name":      slot.get("service_name", ""),
        "start_time":        slot.get("start_time"),
        "hours_until_start": slot.get("hours_until_start"),
        "price":             slot.get("price"),
        "our_price":         slot.get("our_price"),
        "our_markup":        slot.get("our_markup"),
        "location_city":     slot.get("location_city", ""),
        "location_state":    slot.get("location_state", ""),
        "location_country":  slot.get("location_country", "US"),
        "booking_url":       slot.get("booking_url", ""),
        "affiliate_url":     slot.get("affiliate_url", ""),
        "scraped_at":        slot.get("scraped_at"),
        "confidence":        slot.get("confidence", "medium"),
        "raw":               json.dumps({
            k: v for k, v in slot.items()
            if k not in ("raw_data",)  # exclude large nested blobs
        }),
        "updated_at":        datetime.now(timezone.utc).isoformat(),
    }


def upsert_batch(rows: list, url: str, headers: dict) -> int:
    resp = requests.post(
        f"{url}/rest/v1/slots",
        headers=headers,
        json=rows,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  ERROR {resp.status_code}: {resp.text[:200]}")
        return 0
    return len(rows)


def delete_expired(url: str, headers: dict) -> int:
    """Remove slots that started more than 2 hours ago."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    del_hdrs = {**headers, "Prefer": "return=representation"}
    resp = requests.delete(
        f"{url}/rest/v1/slots",
        headers=del_hdrs,
        params={"start_time": f"lt.{cutoff}"},
        timeout=30,
    )
    if resp.status_code in (200, 204):
        try:
            return len(resp.json())
        except Exception:
            return 0
    return 0


def main():
    url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    secret = os.getenv("SUPABASE_SECRET_KEY", "")

    if not url or not secret:
        print("Supabase not configured — set SUPABASE_URL and SUPABASE_SECRET_KEY in .env")
        return

    if not DATA_FILE.exists():
        print(f"No slot data at {DATA_FILE}. Run pipeline first.")
        return

    slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    print(f"Syncing {len(slots)} slots to Supabase...")

    headers = get_headers()

    # Delete expired slots first
    deleted = delete_expired(url, headers)
    if deleted:
        print(f"  Removed {deleted} expired slots")

    # Upsert in batches
    total = 0
    for i in range(0, len(slots), BATCH_SIZE):
        batch = [slot_to_row(s) for s in slots[i:i + BATCH_SIZE]]
        n = upsert_batch(batch, url, headers)
        total += n
        print(f"  Upserted {total}/{len(slots)}...")

    print(f"Supabase sync complete: {total} slots upserted.")


if __name__ == "__main__":
    main()
