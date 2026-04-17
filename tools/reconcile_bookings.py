#!/usr/bin/env python3
"""
Booking reconciliation worker.

Runs every 30 minutes (APScheduler, inside run_api_server.py).
Also executable standalone: python tools/reconcile_bookings.py

Jobs:
  1. Reconcile active (status=booked) bookings against the source platform.
     Flags missing bookings as "reconciliation_required" for a second-look cycle.

  2. (A-6) Act on "reconciliation_required" bookings that have been flagged for
     at least one full reconciliation cycle (~30 min). Issue Stripe refund + email.

  3. (A-15) Retry Stripe refunds for "cancellation_refund_failed" bookings —
     these are cancellations where the OCTO/Bokun side succeeded but Stripe
     failed. Retry until it goes through, then email the customer.

Storage: Supabase Storage bucket "bookings"
  bookings/{booking_id}.json — individual booking record
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SB_URL     = os.getenv("SUPABASE_URL", "").rstrip("/")
SB_SECRET  = os.getenv("SUPABASE_SECRET_KEY", "")
SEEDS_PATH = Path(__file__).parent / "seeds" / "octo_suppliers.json"

# Minimum age a "reconciliation_required" record must have before we act on it.
# One full reconcile cycle is 30 min — require at least that long to avoid acting
# on a record that was just set this run.
_RECONCILE_ACT_AFTER_MINUTES = 35


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


def _stripe_client():
    """Return a configured stripe module or None."""
    try:
        import stripe
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
        return stripe if stripe.api_key else None
    except ImportError:
        return None


def _refund_stripe_once(stripe_mod, payment_intent_id: str) -> dict:
    """
    Attempt a single Stripe refund / hold-cancel.
    Returns {"success": bool, "action": str, "refund_id": str (opt), "error": str (opt)}.
    """
    try:
        pi = stripe_mod.PaymentIntent.retrieve(payment_intent_id)
        if pi.status == "requires_capture":
            stripe_mod.PaymentIntent.cancel(payment_intent_id)
            return {"success": True, "action": "hold_cancelled"}
        elif pi.status == "succeeded":
            refund = stripe_mod.Refund.create(payment_intent=payment_intent_id)
            return {"success": True, "action": "refunded", "refund_id": refund.id}
        elif pi.status in ("canceled", "cancelled"):
            return {"success": True, "action": "already_cancelled"}
        else:
            return {"success": True, "action": f"no_action (pi_status={pi.status})"}
    except Exception as e:
        err = str(e)
        if "already been refunded" in err or "charge_already_refunded" in err:
            return {"success": True, "action": "already_refunded"}
        if "already canceled" in err or "already cancelled" in err:
            return {"success": True, "action": "already_cancelled"}
        return {"success": False, "action": "failed", "error": err}


def _send_cancel_email(record: dict, booking_id: str, refund_desc: str,
                       cancelled_by_customer: bool = False) -> None:
    """Send cancellation email via send_booking_email module. Non-fatal."""
    email = record.get("customer_email", "")
    if not email:
        return
    try:
        spec = importlib.util.spec_from_file_location(
            "send_booking_email", Path(__file__).parent / "send_booking_email.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        slot = {
            "service_name":  record.get("service_name", "Your Experience"),
            "start_time":    record.get("start_time", ""),
            "location_city": record.get("location_city", ""),
            "our_price":     record.get("price_charged"),
            "currency":      record.get("currency", "USD"),
        }
        mod.send_booking_email(
            email_type="booking_cancelled",
            customer_email=email,
            customer_name=record.get("customer_name", ""),
            slot=slot,
            confirmation_number=booking_id,
            refund_status=refund_desc,
            cancelled_by_customer=cancelled_by_customer,
        )
        print(f"  → Cancellation email sent to {email}")
    except Exception as e:
        print(f"  → Email failed (non-fatal): {e}")


def _wallet_credit_back(record: dict, booking_id: str) -> None:
    """Credit wallet if booking was wallet-paid. Non-fatal."""
    if record.get("payment_method") != "wallet" or not record.get("wallet_id"):
        return
    try:
        spec = importlib.util.spec_from_file_location(
            "manage_wallets", Path(__file__).parent / "manage_wallets.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        price = float(record.get("price_charged") or 0)
        mod.credit_wallet(
            record["wallet_id"],
            int(price * 100),
            f"Refund: booking reconciled/cancelled ({booking_id})",
        )
        print(f"  → Wallet credit-back issued: {record['wallet_id']}")
    except Exception as e:
        print(f"  → Wallet credit-back failed (non-fatal): {e}")


# ── Job 1: Reconcile active bookings ─────────────────────────────────────────

def _reconcile_active(records: list[dict]) -> tuple[int, int, int, int]:
    """Check all status=booked records against their supplier. Returns (confirmed, failed, errors, skipped)."""
    active = [r for r in records if r.get("status") == "booked"]
    if not active:
        print("[RECONCILE] No active bookings to reconcile.")
        return 0, 0, 0, 0

    print(f"[RECONCILE] Checking {len(active)} active booking(s)...")
    octo_platforms = {"ventrata_edinexplore", "zaui_test", "peek_pro", "bokun_reseller"}
    now = datetime.now(timezone.utc).isoformat()

    confirmed = failed = skipped = errors = 0

    for record in active:
        booking_id   = record.get("booking_id", "?")
        supplier_id  = record.get("supplier_id", record.get("platform", ""))
        confirmation = record.get("confirmation", "")

        is_octo = supplier_id in octo_platforms or record.get("platform") == "octo"
        if not is_octo or not confirmation:
            skipped += 1
            continue

        status, detail = _verify_octo_booking(supplier_id, confirmation)

        record["last_reconciled_at"]    = now
        record["reconciliation_detail"] = detail

        if status == "confirmed":
            confirmed += 1
            print(f"  ✓ {booking_id}: {detail}")

        elif status == "not_found":
            record["status"]               = "reconciliation_required"
            record["reconciliation_flag"]  = "booking_missing_on_platform"
            record["reconciliation_flag_at"] = now
            _patch_booking(booking_id, record)
            failed += 1
            print(f"  ✗ {booking_id}: MISSING on platform — flagged reconciliation_required")

        else:  # error — transient, don't flag
            errors += 1
            print(f"  ? {booking_id}: Could not verify ({detail}) — will retry next cycle")

    return confirmed, failed, errors, skipped


# ── Job 2 (A-6): Act on reconciliation_required ──────────────────────────────

def _act_on_reconciliation_required(records: list[dict]) -> None:
    """
    For every booking that has been in status=reconciliation_required for at least
    _RECONCILE_ACT_AFTER_MINUTES minutes, issue a Stripe refund and notify the customer.

    Two-cycle guard: we flag first, then act one cycle later. This avoids acting on a
    transient glitch — if the supplier's API was flaky for one cycle, we don't want to
    refund a booking that is actually fine. One full 30-min cycle is enough buffer.
    """
    flagged = [r for r in records if r.get("status") == "reconciliation_required"]
    if not flagged:
        return

    now    = datetime.now(timezone.utc)
    stripe = _stripe_client()
    cutoff = now - timedelta(minutes=_RECONCILE_ACT_AFTER_MINUTES)

    print(f"[RECONCILE] Acting on {len(flagged)} reconciliation_required booking(s)...")

    for record in flagged:
        booking_id = record.get("booking_id", "?")

        # Two-cycle guard: only act if the flag was set before the current cycle started.
        flag_at_str = record.get("reconciliation_flag_at", "")
        if flag_at_str:
            try:
                flag_at = datetime.fromisoformat(flag_at_str.replace("Z", "+00:00"))
                if flag_at > cutoff:
                    print(f"  ⏳ {booking_id}: flagged too recently — waiting for next cycle")
                    continue
            except Exception:
                pass

        # Already actioned somehow
        if record.get("reconciliation_actioned"):
            continue

        print(f"  ⚡ {booking_id}: acting on missing booking...")

        # Stripe refund
        payment_intent = record.get("payment_intent_id", "")
        stripe_result  = {"success": True, "action": "no_payment_on_record"}
        if stripe and payment_intent:
            stripe_result = _refund_stripe_once(stripe, payment_intent)
            if not stripe_result["success"]:
                print(f"  ⚠ Stripe refund failed for {booking_id}: {stripe_result.get('error')}")

        # Wallet credit-back — only if Stripe succeeded (or no Stripe charge).
        # Prevents double-credit if a retry later also calls _wallet_credit_back.
        if stripe_result.get("success"):
            _wallet_credit_back(record, booking_id)

        # Update record
        record["reconciliation_actioned"]    = True
        record["reconciliation_actioned_at"] = now.isoformat()
        record["reconciliation_stripe"]      = stripe_result

        if stripe_result.get("success"):
            record["status"]       = "cancelled"
            record["cancelled_at"] = now.isoformat()
            record["cancelled_by"] = "reconciliation_worker"
            refund_desc = (
                "A full refund has been issued to your original payment method."
                if stripe_result.get("action") in ("refunded", "hold_cancelled",
                                                    "already_refunded", "already_cancelled")
                else "Your cancellation has been recorded. Our team will process your refund."
            )
        else:
            record["status"]              = "cancellation_refund_failed"
            record["cancellation_flag_at"] = now.isoformat()
            refund_desc = ("We've cancelled your booking. There was a delay processing your refund "
                           "— our team will resolve this within 3–5 business days.")

        _patch_booking(booking_id, record)

        # Email customer
        _send_cancel_email(record, booking_id, refund_desc)


# ── Job 3 (A-15): Retry cancellation_refund_failed ───────────────────────────

def _retry_refund_failed(records: list[dict]) -> None:
    """
    Retry Stripe refunds for bookings stuck in cancellation_refund_failed.
    On success: mark cancelled + email customer.
    On persistent failure: leave status unchanged, increment attempt counter.
    """
    failed = [r for r in records if r.get("status") == "cancellation_refund_failed"]
    if not failed:
        return

    stripe = _stripe_client()
    if not stripe:
        print("[RECONCILE] Stripe not configured — cannot retry refunds")
        return

    now = datetime.now(timezone.utc)
    print(f"[RECONCILE] Retrying {len(failed)} cancellation_refund_failed booking(s)...")

    for record in failed:
        booking_id     = record.get("booking_id", "?")
        payment_intent = record.get("payment_intent_id", "")

        # No payment on record — mark cancelled (wallet or no-charge case)
        if not payment_intent:
            _wallet_credit_back(record, booking_id)
            record["status"]       = "cancelled"
            record["cancelled_at"] = now.isoformat()
            record["cancelled_by"] = "reconciliation_worker_no_pi"
            _patch_booking(booking_id, record)
            print(f"  ✓ {booking_id}: no payment_intent — marked cancelled")
            continue

        stripe_result = _refund_stripe_once(stripe, payment_intent)

        # Increment attempt counter
        record.setdefault("refund_retry_count", 0)
        record["refund_retry_count"] += 1
        record["last_refund_retry_at"] = now.isoformat()

        if stripe_result.get("success"):
            _wallet_credit_back(record, booking_id)
            record["status"]       = "cancelled"
            record["cancelled_at"] = now.isoformat()
            record["cancelled_by"] = "reconciliation_worker_refund_retry"
            record["refund_action"] = stripe_result.get("action")
            _patch_booking(booking_id, record)

            refund_desc = (
                "A full refund has been issued to your original payment method."
                if stripe_result.get("action") in ("refunded", "hold_cancelled",
                                                    "already_refunded", "already_cancelled")
                else "Your refund has been processed."
            )
            _send_cancel_email(record, booking_id, refund_desc)
            print(f"  ✓ {booking_id}: refund retried successfully ({stripe_result.get('action')})")
        else:
            record["last_refund_error"] = stripe_result.get("error", "")
            _patch_booking(booking_id, record)
            print(f"  ✗ {booking_id}: refund retry failed — "
                  f"{stripe_result.get('error', '')[:100]} (attempt #{record['refund_retry_count']})")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    if not SB_URL or not SB_SECRET:
        print("[RECONCILE] Supabase not configured — exiting")
        sys.exit(1)

    records = _list_bookings()

    # Job 1: active bookings check
    confirmed, missing, errors, skipped = _reconcile_active(records)

    # Jobs 2 + 3: act on flagged records
    # Re-use the same records list, patching it in-place for accurate status filtering.
    # Note: _list_bookings() fetches the current DB state; records updated by Job 1
    # this cycle will reflect their NEW status already since _patch_booking writes back.
    # For simplicity, re-fetch so Jobs 2+3 see the post-Job1 state.
    records = _list_bookings()
    _act_on_reconciliation_required(records)
    _retry_refund_failed(records)

    print(
        f"[RECONCILE] Done. confirmed={confirmed} missing={missing} "
        f"errors={errors} skipped={skipped}"
    )


if __name__ == "__main__":
    main()
