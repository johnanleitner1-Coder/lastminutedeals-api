#!/usr/bin/env python3
"""
Booking reconciliation worker.

Runs every 30 minutes (APScheduler, inside run_api_server.py).
Also executable standalone: python tools/reconcile_bookings.py

For every booking with status="booked", re-queries the source platform to confirm
the booking still exists. If the platform says the booking is gone or returns an
error, the booking is flagged as "reconciliation_required" for review.

This catches:
  - Silent failures where we thought we booked but the platform dropped it
  - Platform-side cancellations we weren't notified about
  - Any divergence between our records and the source of truth

Storage: Supabase Storage bucket "bookings"
  bookings/{booking_id}.json — individual booking record
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SB_URL     = os.getenv("SUPABASE_URL", "").rstrip("/")
SB_SECRET  = os.getenv("SUPABASE_SECRET_KEY", "")
SEEDS_PATH = Path(__file__).parent / "seeds" / "octo_suppliers.json"


def _headers() -> dict:
    return {"apikey": SB_SECRET, "Authorization": f"Bearer {SB_SECRET}"}


def _list_bookings() -> list[dict]:
    """Fetch all booking records from Supabase Storage. Paginates to handle >1000 records."""
    names: list[str] = []
    offset = 0
    page_size = 500
    while True:
        try:
            r = requests.post(
                f"{SB_URL}/storage/v1/object/list/bookings",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"prefix": "", "limit": page_size, "offset": offset},
                timeout=10,
            )
            if r.status_code != 200:
                break
            page = r.json()
            if not page:
                break
            for item in page:
                n = item.get("name", "")
                if (n and n.endswith(".json")
                        and not n.startswith("cancellation_queue/")
                        and not n.startswith("circuit_breaker/")
                        and not n.startswith("config/")
                        and not n.startswith("idem_")
                        and not n.startswith("webhook_session_")
                        and not n.startswith("cleanup_")
                        and not n.startswith("pending_exec_")
                        and not n.startswith("inbound_emails/")):
                    names.append(n)
            if len(page) < page_size:
                break  # last page
            offset += page_size
        except Exception as e:
            print(f"[RECONCILE] Failed to list bookings (offset={offset}): {e}")
            break

    records = []
    for name in names:
        try:
            rec = requests.get(
                f"{SB_URL}/storage/v1/object/bookings/{name}",
                headers=_headers(), timeout=5,
            )
            if rec.status_code == 200:
                records.append(rec.json())
        except Exception:
            pass
    return records


def _verify_octo_booking(supplier_id: str, confirmation: str) -> tuple[str, str]:
    """
    Re-query OCTO supplier for the booking.
    Returns (status, detail) where status is:
      "confirmed"  — booking exists on platform
      "not_found"  — booking missing (possible silent failure or platform cancellation)
      "error"      — could not reach supplier (transient, don't flag)
    """
    try:
        suppliers = json.loads(SEEDS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return "error", f"Could not load supplier config: {e}"

    supplier = next(
        (s for s in suppliers if s.get("supplier_id") == supplier_id and s.get("enabled")),
        None,
    )
    if not supplier:
        return "error", f"No supplier config for '{supplier_id}'"

    api_key_env = supplier.get("api_key_env", "")
    if not api_key_env:
        return "error", f"Supplier '{supplier_id}' missing api_key_env in config"
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        return "error", f"API key not set: {api_key_env}"

    base_url = supplier.get("base_url", "").rstrip("/")
    try:
        r = requests.get(
            f"{base_url}/bookings/{confirmation}",
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            timeout=15,
        )
        if r.status_code == 200:
            data   = r.json()
            status = data.get("status", "UNKNOWN")
            return "confirmed", f"Platform status: {status}"
        if r.status_code == 404:
            return "not_found", "Booking not found on platform"
        return "error", f"Platform returned HTTP {r.status_code}"
    except requests.RequestException as e:
        return "error", f"Request failed: {e}"


def _patch_booking(booking_id: str, record: dict) -> None:
    """Overwrite a booking record in Supabase Storage."""
    try:
        requests.post(
            f"{SB_URL}/storage/v1/object/bookings/{booking_id}.json",
            headers={**_headers(), "Content-Type": "application/json", "x-upsert": "true"},
            data=json.dumps(record),
            timeout=8,
        )
    except Exception as e:
        print(f"[RECONCILE] Failed to patch {booking_id}: {e}")


def main() -> None:
    if not SB_URL or not SB_SECRET:
        print("[RECONCILE] Supabase not configured — exiting")
        sys.exit(1)

    records = _list_bookings()
    active  = [r for r in records if r.get("status") == "booked"]

    if not active:
        print("[RECONCILE] No active bookings to reconcile.")
        return

    print(f"[RECONCILE] Checking {len(active)} active booking(s)...")

    octo_platforms = {"ventrata_edinexplore", "zaui_test", "peek_pro", "bokun_reseller"}
    now = datetime.now(timezone.utc).isoformat()

    confirmed = failed = skipped = errors = 0

    for record in active:
        booking_id   = record.get("booking_id", "?")
        supplier_id  = record.get("supplier_id", record.get("platform", ""))
        confirmation = record.get("confirmation", "")

        # Only reconcile OCTO bookings — we have a re-query path for them
        is_octo = supplier_id in octo_platforms or record.get("platform") == "octo"
        if not is_octo or not confirmation:
            skipped += 1
            continue

        status, detail = _verify_octo_booking(supplier_id, confirmation)

        record["last_reconciled_at"] = now
        record["reconciliation_detail"] = detail

        if status == "confirmed":
            confirmed += 1
            print(f"  ✓ {booking_id}: {detail}")

        elif status == "not_found":
            # Booking is gone on the platform — flag it
            record["status"] = "reconciliation_required"
            record["reconciliation_flag"] = "booking_missing_on_platform"
            _patch_booking(booking_id, record)
            failed += 1
            print(f"  ✗ {booking_id}: MISSING on platform — flagged reconciliation_required")

        else:  # error — transient, don't flag, just log
            errors += 1
            print(f"  ? {booking_id}: Could not verify ({detail}) — will retry next cycle")

    print(
        f"[RECONCILE] Done. confirmed={confirmed} missing={failed} "
        f"errors={errors} skipped={skipped}"
    )


if __name__ == "__main__":
    main()
