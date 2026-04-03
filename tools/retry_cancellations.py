#!/usr/bin/env python3
"""
Automatic OCTO cancellation retry worker.

Runs every 15 minutes (Railway cron: */15 * * * *).
Picks up any bookings in the Supabase `cancellation_queue` table whose OCTO
cancellation failed during the synchronous DELETE /bookings/{id} request, and
retries them until they succeed.

A cancellation can fail transiently (supplier timeout, 5xx) while Stripe has
already refunded the customer. This worker ensures the booking is also cleaned
up on the source platform — fully automated, no human intervention needed.

Retry limits:
  - Up to 48 attempts (12 hours at 15-min intervals) before marking as 'exhausted'
  - 404 from supplier = treated as success (booking already gone)
  - 400/401/403 = permanent failure, stop retrying immediately

Table: cancellation_queue
  booking_id        TEXT PRIMARY KEY
  confirmation      TEXT   -- OCTO booking UUID
  supplier_id       TEXT   -- e.g. ventrata_edinexplore
  payment_intent_id TEXT
  price_charged     FLOAT
  octo_cancelled    BOOLEAN DEFAULT FALSE
  attempts          INTEGER DEFAULT 0
  last_attempt_at   TIMESTAMPTZ
  created_at        TIMESTAMPTZ
  status            TEXT   -- pending_octo | resolved | exhausted | permanent_error
  error_detail      TEXT
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

MAX_ATTEMPTS = 48   # 12 hours at 15-min intervals
SEEDS_PATH   = Path(__file__).parent / "seeds" / "octo_suppliers.json"


def _sb_headers() -> dict:
    return {
        "apikey":        SB_SECRET,
        "Authorization": f"Bearer {SB_SECRET}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def _ensure_table() -> bool:
    """
    Create the cancellation_queue table if it doesn't exist.
    Uses psycopg2 for the DDL — REST API can't run CREATE TABLE.
    Falls back gracefully if psycopg2 isn't available.
    """
    db_url = os.getenv("SUPABASE_DB_URL", "")
    if not db_url:
        return True  # Can't create, assume it exists

    try:
        import psycopg2  # type: ignore
    except ImportError:
        print("[SETUP] psycopg2 not available — skipping table auto-creation")
        return True

    try:
        # Parse DB URL manually to handle special chars in password
        # Format: postgresql://user:password@host:port/dbname
        stripped = db_url.replace("postgresql://", "").replace("postgres://", "")
        userinfo, hostinfo = stripped.rsplit("@", 1)
        user, password = userinfo.split(":", 1)
        host_port, dbname = hostinfo.rsplit("/", 1)
        host, port = host_port.rsplit(":", 1)

        conn = psycopg2.connect(
            host=host, port=int(port), dbname=dbname,
            user=user, password=password, sslmode="require",
            connect_timeout=10,
        )
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cancellation_queue (
                booking_id        TEXT PRIMARY KEY,
                confirmation      TEXT NOT NULL,
                supplier_id       TEXT NOT NULL,
                payment_intent_id TEXT,
                price_charged     FLOAT,
                octo_cancelled    BOOLEAN DEFAULT FALSE,
                attempts          INTEGER DEFAULT 0,
                last_attempt_at   TIMESTAMPTZ,
                created_at        TIMESTAMPTZ DEFAULT NOW(),
                status            TEXT DEFAULT 'pending_octo',
                error_detail      TEXT
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("[SETUP] cancellation_queue table ready")
        return True
    except Exception as e:
        print(f"[SETUP] Could not create table: {e}")
        return True  # Still try to proceed — table may already exist


def _cancel_octo(supplier_id: str, booking_uuid: str) -> tuple[bool, str, bool]:
    """
    Attempt a single OCTO DELETE /bookings/{uuid}.
    Returns (success, detail, is_permanent_failure).
    """
    try:
        suppliers = json.loads(SEEDS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"Could not load supplier config: {e}", True

    supplier = next((s for s in suppliers if s.get("supplier_id") == supplier_id and s.get("enabled")), None)
    if not supplier:
        return False, f"No enabled supplier config for '{supplier_id}'", True

    api_key = os.getenv(supplier["api_key_env"], "").strip()
    if not api_key:
        return False, f"API key env var not set: {supplier['api_key_env']}", True

    base_url = supplier["base_url"].rstrip("/")

    # One attempt per cron cycle — the cron itself IS the retry loop
    try:
        r = requests.delete(
            f"{base_url}/bookings/{booking_uuid}",
            headers={
                "Authorization":     f"Bearer {api_key}",
                "Octo-Capabilities": "octo/pricing",
                "Content-Type":      "application/json",
            },
            timeout=15,
        )

        if r.status_code in (200, 204):
            return True, f"Cancelled (HTTP {r.status_code})", False

        if r.status_code == 404:
            # Booking no longer exists on supplier — desired state
            return True, "Not found on supplier (already cancelled or expired)", False

        if r.status_code in (400, 401, 403, 422):
            return False, f"Permanent failure HTTP {r.status_code}: {r.text[:300]}", True

        # 5xx or unexpected — transient, will retry next cycle
        return False, f"HTTP {r.status_code}: {r.text[:200]}", False

    except requests.RequestException as e:
        return False, f"Request failed: {e}", False


def _fetch_pending() -> list[dict]:
    """Fetch entries that still need OCTO cancellation and haven't exhausted retries."""
    r = requests.get(
        f"{SB_URL}/rest/v1/cancellation_queue",
        headers=_sb_headers(),
        params={
            "octo_cancelled": "eq.false",
            "attempts":       f"lt.{MAX_ATTEMPTS}",
            "status":         "eq.pending_octo",
            "select":         "*",
            "order":          "created_at.asc",
        },
        timeout=15,
    )
    if r.status_code != 200:
        print(f"[RETRY] Failed to fetch queue: HTTP {r.status_code} — {r.text[:200]}")
        return []
    return r.json() or []


def _update_entry(booking_id: str, patch: dict) -> None:
    """PATCH a queue entry in Supabase."""
    requests.patch(
        f"{SB_URL}/rest/v1/cancellation_queue",
        headers=_sb_headers(),
        params={"booking_id": f"eq.{booking_id}"},
        json=patch,
        timeout=10,
    )


def main() -> None:
    if not SB_URL or not SB_SECRET:
        print("[RETRY] SUPABASE_URL or SUPABASE_SECRET_KEY not set — exiting")
        sys.exit(1)

    _ensure_table()

    pending = _fetch_pending()
    if not pending:
        print("[RETRY] No pending OCTO cancellations.")
        return

    print(f"[RETRY] Processing {len(pending)} pending cancellation(s)...")

    for entry in pending:
        booking_id   = entry["booking_id"]
        supplier_id  = entry["supplier_id"]
        confirmation = entry["confirmation"]
        attempts     = entry.get("attempts", 0)

        print(f"  [{booking_id}] supplier={supplier_id} uuid={confirmation} attempt={attempts + 1}")

        success, detail, permanent = _cancel_octo(supplier_id, confirmation)

        now   = datetime.now(timezone.utc).isoformat()
        patch = {
            "attempts":          attempts + 1,
            "last_attempt_at":   now,
            "error_detail":      detail,
        }

        if success:
            patch["octo_cancelled"] = True
            patch["status"]         = "resolved"
            print(f"  ✓ Resolved: {detail}")
        elif permanent:
            patch["status"] = "permanent_error"
            print(f"  ✗ Permanent error (no more retries): {detail}")
        elif attempts + 1 >= MAX_ATTEMPTS:
            patch["status"] = "exhausted"
            print(f"  ✗ Exhausted {MAX_ATTEMPTS} attempts. Final error: {detail}")
        else:
            print(f"  ↻ Will retry next cycle: {detail}")

        _update_entry(booking_id, patch)

    print("[RETRY] Done.")


if __name__ == "__main__":
    main()
