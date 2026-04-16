#!/usr/bin/env python3
"""
Automatic OCTO cancellation retry worker.

Runs every 15 minutes as a background thread inside run_api_server.py (APScheduler).
Also executable standalone: python tools/retry_cancellations.py

Picks up any entries in the Supabase Storage `cancellation_queue/` prefix whose OCTO
cancellation failed during the synchronous DELETE /bookings/{id} request, and retries
them until they succeed.

Storage layout (Supabase Storage bucket: "bookings"):
  bookings/{booking_id}.json          — individual booking record
  cancellation_queue/{booking_id}.json — pending OCTO retries

A cancellation can fail transiently (supplier timeout, 5xx) while Stripe has already
refunded the customer. This worker ensures the booking is also cleaned up on the source
platform — fully automated, zero human intervention.

Retry policy:
  - Up to 48 attempts (12 hours at 15-min intervals)
  - 404 from supplier = success (booking already gone = desired state)
  - 400/401/403/422 = permanent failure, stops retrying immediately
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SB_URL    = os.getenv("SUPABASE_URL", "").rstrip("/")
SB_SECRET = os.getenv("SUPABASE_SECRET_KEY", "")
SEEDS_PATH = Path(__file__).parent / "seeds" / "octo_suppliers.json"

MAX_ATTEMPTS = 48   # 12 hours at 15-min intervals


def _headers() -> dict:
    return {"apikey": SB_SECRET, "Authorization": f"Bearer {SB_SECRET}"}


def _storage_get(path: str) -> dict | None:
    """Read a JSON object from Supabase Storage. Returns None on 404."""
    try:
        r = requests.get(f"{SB_URL}/storage/v1/object/bookings/{path}",
                         headers=_headers(), timeout=8)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _storage_put(path: str, data: dict) -> None:
    """Write a JSON object to Supabase Storage."""
    try:
        requests.post(
            f"{SB_URL}/storage/v1/object/bookings/{path}",
            headers={**_headers(), "Content-Type": "application/json", "x-upsert": "true"},
            data=json.dumps(data),
            timeout=8,
        )
    except Exception as e:
        print(f"[STORAGE] Write failed for {path}: {e}")


def _storage_delete(path: str) -> None:
    """Delete a file from Supabase Storage."""
    try:
        requests.delete(f"{SB_URL}/storage/v1/object/bookings/{path}",
                        headers=_headers(), timeout=8)
    except Exception:
        pass


def _storage_list_prefix(prefix: str) -> list[str]:
    """List all files under a prefix in the bookings bucket. Returns list of names."""
    try:
        r = requests.post(
            f"{SB_URL}/storage/v1/object/list/bookings",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"prefix": prefix, "limit": 500, "offset": 0},
            timeout=10,
        )
        if r.status_code == 200:
            return [item["name"] for item in r.json() if item.get("name")]
    except Exception:
        pass
    return []


def _cancel_octo(supplier_id: str, booking_uuid: str) -> tuple[bool, str, bool]:
    """
    Attempt a single OCTO DELETE /bookings/{uuid}.
    Returns (success, detail, is_permanent_failure).
    """
    try:
        suppliers = json.loads(SEEDS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"Could not load supplier config: {e}", True

    supplier = next((s for s in suppliers if s.get("supplier_id") == supplier_id
                     and s.get("enabled")), None)
    if not supplier:
        return False, f"No enabled supplier config for '{supplier_id}'", True

    api_key_env = supplier.get("api_key_env", "")
    if not api_key_env:
        return False, f"Supplier '{supplier_id}' missing api_key_env in config", True
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        return False, f"API key env var not set: {api_key_env}", True

    base_url = supplier.get("base_url", "").rstrip("/")
    try:
        r = requests.delete(
            f"{base_url}/bookings/{booking_uuid}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        if r.status_code in (200, 204):
            return True, f"Cancelled (HTTP {r.status_code})", False
        if r.status_code == 404:
            return True, "Not found on supplier (already cancelled or expired)", False
        if r.status_code in (400, 401, 403, 422):
            return False, f"Permanent HTTP {r.status_code}: {r.text[:300]}", True
        return False, f"HTTP {r.status_code}: {r.text[:200]}", False
    except requests.RequestException as e:
        return False, f"Request failed: {e}", False


def main() -> None:
    if not SB_URL or not SB_SECRET:
        print("[RETRY] SUPABASE_URL or SUPABASE_SECRET_KEY not set — exiting")
        sys.exit(1)

    # List all pending entries in the cancellation_queue/ prefix
    names = _storage_list_prefix("cancellation_queue/")
    if not names:
        print("[RETRY] No pending OCTO cancellations.")
        return

    print(f"[RETRY] Processing {len(names)} pending cancellation(s)...")

    for name in names:
        # name is the full path returned by Supabase list API (e.g. "cancellation_queue/bk_xxx.json").
        # Do NOT prepend the prefix again — that creates a double-prefix 404.
        path  = name
        entry = _storage_get(path)
        if not entry:
            continue

        # Skip terminal entries — they have already been fully processed.
        if entry.get("status") in ("exhausted", "permanent_error", "resolved"):
            continue

        booking_id   = entry.get("booking_id") or name.split("/")[-1].replace(".json", "")
        supplier_id  = entry.get("supplier_id", "")
        confirmation = entry.get("confirmation", "")
        attempts     = entry.get("attempts", 0)

        if not supplier_id or not confirmation:
            print(f"  [{booking_id}] Missing supplier_id or confirmation — skipping")
            continue

        print(f"  [{booking_id}] supplier={supplier_id} uuid={confirmation} attempt={attempts + 1}")

        success, detail, permanent = _cancel_octo(supplier_id, confirmation)

        entry["attempts"]         = attempts + 1
        entry["last_attempt_at"]  = datetime.now(timezone.utc).isoformat()
        entry["error_detail"]     = detail

        if success:
            entry["status"] = "resolved"
            # Remove from queue — it's done
            _storage_delete(path)
            print(f"  ✓ Resolved: {detail}")
        elif permanent or attempts + 1 >= MAX_ATTEMPTS:
            entry["status"] = "exhausted" if attempts + 1 >= MAX_ATTEMPTS else "permanent_error"
            # Keep the entry but mark terminal — stops future retries
            _storage_put(path, entry)
            print(f"  ✗ Terminal ({entry['status']}): {detail}")
        else:
            # Update attempt count and retry next cycle
            _storage_put(path, entry)
            print(f"  ↻ Will retry next cycle: {detail}")

    print("[RETRY] Done.")


if __name__ == "__main__":
    main()
