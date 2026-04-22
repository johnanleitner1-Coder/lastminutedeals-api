# Last Minute Deals HQ — Complete System Map

**Last updated:** 2026-04-21 (v34 — Added 3 new Egypt vendors: Amazing Global Travel (106938), Perfect Day Tours (121323), Nefertiti Tours (101086). Vendor count 21→24. Updated all supplier counts and MCP instructions. Previous: v33 B-159 FIXED, Sailing Windermere added.)
**Status key:** ✅ Verified working | ⚠️ Partially working / untested | ❌ Broken (code bug confirmed) | 🔲 Not yet built

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Process 1: Slot Discovery Pipeline](#2-process-1-slot-discovery-pipeline)
3. [Process 2: Human Booking (Stripe Checkout)](#3-process-2-human-booking-stripe-checkout)
4. [Process 3: Autonomous Booking — Direct Wallet](#4-process-3-autonomous-booking--direct-wallet)
5. [Process 4: Autonomous Booking — Saved Stripe Card](#5-process-4-autonomous-booking--saved-stripe-card)
6. [Process 5: Autonomous Booking — Execute/Guaranteed](#6-process-5-autonomous-booking--executeguaranteed)
7. [Process 6: Semi-Autonomous — /api/execute Intent](#7-process-6-semi-autonomous--apiexecute-intent)
8. [Process 7: Intent Sessions](#8-process-7-intent-sessions)
9. [Process 8: Cancellation Matrix](#9-process-8-cancellation-matrix)
10. [Process 9: Supplier-Initiated Cancellation (Bokun Webhook)](#10-process-9-supplier-initiated-cancellation-bokun-webhook)
11. [Process 10: MCP Agent Integration](#11-process-10-mcp-agent-integration)
12. [Process 11: Wallet System](#12-process-11-wallet-system)
13. [Process 12: Background Services (APScheduler)](#13-process-12-background-services-apscheduler)
14. [Process 13: Webhook Subscriber Notifications](#14-process-13-webhook-subscriber-notifications)
15. [Multi-Quantity Booking — All Paths](#15-multi-quantity-booking--all-paths)
16. [Infrastructure & Dependencies](#16-infrastructure--dependencies)
17. [Bug Register — Confirmed Code Defects](#17-bug-register--confirmed-code-defects)
18. [Environment Variables Required](#18-environment-variables-required)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  DATA PIPELINE (Railway APScheduler every 4h)                        │
│  fetch_octo_slots.py → aggregate_slots.py → compute_pricing.py      │
│  → sync_to_supabase.py                                              │
│  ✅ (A-9) Railway now runs fetch_octo + aggregate autonomously every 4h │
└────────────────────────────┬────────────────────────────────────────┘
                             │ upserts to Supabase
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SUPABASE                                                           │
│  • "slots" table              — available inventory (REST API)      │
│  • "bookings" bucket          — booking records (Storage JSON)      │
│  • "cancellation_queue/"      — failed OCTO cancellations retry     │
│  • "circuit_breaker/{id}.json"— per-supplier circuit state         │
│  • "config/wallets.json"      — ALL wallet balances in ONE file     │
│  • "inbound_emails/"          — SendGrid parsed inbound emails      │
│  • "intent_sessions.json"     — active agent intent sessions        │
│  • "market_snapshot.json"     — pre-computed market intelligence    │
│  • "sms_subscribers.json"     — SMS opt-in subscriber list          │
│  • "sms_sent_log.json"        — per-phone daily send tracking       │
│  • "request_logs" (Postgres)  — UNUSED (IPv6-only, blocked from Railway)│
└──────────┬──────────────────────────────────────────────────────────┘
           │ REST API
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  RAILWAY — run_api_server.py (Flask + APScheduler + embedded FastMCP)│
│  https://api.lastminutedealshq.com                                  │
│                                                                     │
│  Booking entry points:                                              │
│  POST /api/book              — human Stripe checkout (quantity OK)  │
│  POST /api/book/direct       — autonomous wallet (quantity OK)      │
│  POST /api/customers/{id}/book — autonomous saved Stripe card       │
│  POST /execute/guaranteed    — autonomous multi-path engine         │
│  POST /execute/best          — goal-optimized autonomous booking    │
│  POST /api/execute           — semi-auto: agent picks, human pays   │
│  POST /intent/create         — persistent goal session              │
│                                                                     │
│  Human booking page:                                                │
│  GET /book/{slot_id}         — HTML booking page (slot details+form)│
│  POST /book/{slot_id}/checkout — form POST → Stripe redirect       │
│                                                                     │
│  Cancellation entry points:                                         │
│  DELETE /bookings/{id}       — API cancel (Stripe + OCTO + retry)  │
│  GET/POST /cancel/{id}       — customer self-serve cancel page      │
│  POST /api/bokun/webhook     — supplier-initiated (Bokun)           │
│                                                                     │
│  Observability / Admin:                                             │
│  GET /health, /metrics, /bookings/{id}, /verify/{id}               │
│  GET /webhooks/peek          — Bokun peek verify (flags only)       │
│  POST /admin/refresh-slots   — runs pipeline in-process on Railway  │
│  POST /api/inbound-email     — SendGrid inbound parse webhook       │
│                                                                     │
│  Background jobs (APScheduler, in-process):                        │
│  retry_cancellations()       — every 15 min                        │
│  reconcile_bookings()        — every 30 min                        │
│  slot_discovery()            — every 4 h (fetch_octo + aggregate)  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. Process 1: Slot Discovery Pipeline

**Trigger:** Railway APScheduler every 4h (automated, 24/7). Local Task Scheduler jobs DISABLED 2026-04-18 — see archive/local_pipeline/README.md
**Status:** ✅ Bokun path working
**Inventory (2026-04-16):** 1,491 live slots in Supabase | 904 within 72h window | 14 cities | 4 categories
**Test supplier guard:** `sync_to_supabase.py` filters `_TEST_SUPPLIER_NAMES` before every upsert — Zaui/Ventrata/Peek test slots cannot re-enter production

### Step 1: fetch_octo_slots.py

```
START
  │
  ├─ Load tools/seeds/octo_suppliers.json
  │    Only processes: enabled=true AND API key set in .env
  │    Currently enabled: bokun_reseller ONLY
  │
  ├─ For each vendor_id (23 total): [85, 22298, 134418, 103510, 137492, 16261, 105917, 3020, 33562, 70, 102991, 123380, 98502, 109399, 4278, 136863, 126903, 137927, 104051, 17252, 57545, 121323, 101086]
  │    ├─ GET /products  (NO pricing capability header — avoids Bokun hang)
  │    ├─ For each product:
  │    │    ├─ POST /availability (WITH octo/pricing header, date range: today → +8 days)
  │    │    ├─ Filter: status in {AVAILABLE, FREESALE, LIMITED}
  │    │    ├─ Filter: starts within hours_ahead (default 168h)
  │    │    ├─ _resolve_product_identity() — 3-level resolution chain:
  │    │    │    ├─ Level 1: reference_supplier_map prefix match (city-level precision)
  │    │    │    ├─ Level 2: product_id_map exact match (null/empty ref fallback)
  │    │    │    ├─ Level 3: vendor_id_to_supplier_map (catch-all — all 23 vendors mapped)
  │    │    │    ├─ WARNING logged if all 3 levels fail (new vendor added without config)
  │    │    │    └─ 0 unresolved slots ✅ | guaranteed for any future product from known vendors
  │    │    └─ normalize_slot: slot_id = sha256(platform+product_id+start_time)
  │    │         booking_url = JSON blob: {_type:"octo", base_url, api_key_env,
  │    │                                   product_id, option_id, availability_id,
  │    │                                   unit_id, supplier_id, vendor_id}
  │    └─ retry_on_timeout=true: one retry on timeout
  │
  └─ Write .tmp/octo_slots.json

Step 2: aggregate_slots.py
  Read ONLY .tmp/octo_slots.json (explicit allowlist, no glob) → deduplicate on slot_id → filter → sort by hours_until_start → .tmp/aggregated_slots.json

Step 3: compute_pricing.py
  Per slot: base 8-12% markup × urgency multiplier (×1.0 at 48-72h → ×2.5 at 0-12h)
  Google Sheets Pricing Log → fetch historical fill rate (CURRENTLY BROKEN: OAuth expired)
  Falls back to defaults when Sheets unavailable → writes our_price, our_markup to slot

Step 4: sync_to_supabase.py
  Upsert all slots to Supabase "slots" table (keyed on slot_id). Purge past slots.

```

**IMPORTANT:** `.tmp/aggregated_slots.json` only exists on the local laptop. It does NOT exist on
Railway. Railway reads slot data from the Supabase `slots` table.

---

## 3. Process 2: Human Booking (Stripe Checkout)

**Entry points:** Landing page, MCP book_slot, POST /api/book, POST /api/execute
**Status:** ✅ Checkout creation working | ❌ Real OCTO execution untested end-to-end
**Quantity support:** ✅ Full (1-20 persons)

```
POST /api/book
  { slot_id, customer_name, customer_email, customer_phone, quantity (1-20) }
  │
  ├─ Validate all fields present → 400 if not
  ├─ Idempotency key check (memory cache + Supabase) → return same checkout_url if duplicate
  ├─ get_slot_by_id → 404 if not found
  ├─ Already booked check → 409
  ├─ Start time already passed → 410
  ├─ our_price > 0 check → 400
  │
  ├─ Stripe: create checkout session (capture_method=manual — HOLD NOT CHARGE)
  │    price_cents = our_price × 100 (per person)
  │    line_item quantity = requested quantity → total = per_person × quantity
  │    metadata: slot_id, customer info, booking_id, platform, booking_url, quantity, dry_run
  │    └─ STRIPE ERROR → 500
  │
  ├─ Save pending_payment record to Supabase Storage (booking_id keyed)
  └─ Return { checkout_url, booking_id, status: "pending_payment" }


CUSTOMER: completes payment on Stripe page
  └─ Stripe fires: POST /api/webhook (checkout.session.completed)


POST /api/webhook (Stripe)
  │
  ├─ Verify Stripe HMAC → 400 if invalid
  ├─ session.expired → mark booking "expired" → 200
  ├─ wallet_topup fast path → credit wallet → 200
  ├─ In-memory idempotency lock (same session already running) → 200
  ├─ Supabase idempotency record (already processed) → 200
  ├─ Mark session "processing" in Supabase Storage
  ├─ Spawn daemon thread: _fulfill_booking_async()
  └─ Return 200 immediately to Stripe


_fulfill_booking_async() [daemon thread, 45s hard ceiling]
  │
  ├─ dry_run=true?
  │    ├─ YES → synthetic confirmation, skip supplier + payment capture
  │    │         (pipeline test mode — no real booking, no real charge)
  │    └─ NO  → send "booking_initiated" email (non-fatal if fails)
  │
  ├─ _fulfill_booking(slot_id, customer, platform, booking_url, quantity)
  │    │
  │    ├─ Parse booking_url JSON → OCTO params
  │    ├─ Check circuit breaker → OPEN: raise BookingUnavailableError
  │    │
  │    └─ OCTOBooker.run():
  │         │
  │         ├─ POST /reservations
  │         │    body: { productId, optionId, availabilityId,
  │         │            unitItems: [{"unitId": unit_id} × quantity],
  │         │            contact: {fullName, emailAddress, phoneNumber} }
  │         │    ├─ 2xx → reservation_uuid captured
  │         │    ├─ 409 (availability conflict):
  │         │    │    ├─ POST /availability for fresh slots
  │         │    │    ├─ Found new available slot → retry POST /reservations with new availability_id
  │         │    │    └─ No fresh slot → raise BookingUnavailableError
  │         │    ├─ 4xx other → raise immediately (no retry)
  │         │    └─ 5xx → retry once (1-1.5s jitter) → still fails → raise BookingTimeoutError
  │         │
  │         ├─ POST /bookings/{reservation_uuid}/confirm
  │         │    body: { contact, resellerReference: "LMD-{slot_id[:12]}" }
  │         │    ├─ 2xx → extract confirmation (OCTO uuid) + supplierReference (Bokun ref)
  │         │    ├─ 4xx → _octo_cleanup() then raise
  │         │    └─ 5xx → retry once → _octo_cleanup() then raise
  │         │
  │         └─ _octo_cleanup() (orphaned reservation release):
  │              DELETE /bookings/{reservation_uuid}, retry once
  │              If both fail → meta["cleanup_required"]=true
  │              → caller saves cleanup record to Supabase for manual review
  │
  ├─ Returns 3-tuple: (confirmation, booking_meta, supplier_reference)
  │    caller correctly unpacks all 3: confirmation, booking_meta, supplier_reference = fut.result()
  │
  ├─ SUCCESS:
  │    ├─ stripe.PaymentIntent.capture() ← CARD CHARGED
  │    ├─ _mark_booked(slot_id)
  │    ├─ Save booking record to Supabase Storage:
  │    │    { booking_id, confirmation (OCTO uuid), supplier_reference (Bokun ref),
  │    │      payment_intent_id, status: "booked", payment_method: "stripe", ... }
  │    └─ Send "booking_confirmed" email with cancel link
  │         cancel link: /cancel/{booking_id}?t={hmac_token}
  │
  └─ FAILURE:
       ├─ stripe.PaymentIntent.cancel() ← HOLD RELEASED, CUSTOMER NOT CHARGED
       ├─ Save booking record: status "failed", failure_reason
       └─ Send "booking_failed" email


POLLING: GET /bookings/{booking_id}
  Returns: { status, confirmation, service_name, executed_at, ... }
  Statuses: pending_payment → booked | failed | expired | cancelled
```

---

## 4. Process 3: Autonomous Booking — Direct Wallet

**Entry point:** POST /api/book/direct
**Requires:** X-API-Key header + wallet_id + execution_mode: "autonomous"
**Status:** ✅ OPERATIONAL
**Quantity support:** ✅ SUPPORTED (quantity clamped 1–20)

```
POST /api/book/direct
  { slot_id, customer_name, customer_email, customer_phone,
    wallet_id, execution_mode: "autonomous" }
  │
  ├─ X-API-Key validation → 401 if invalid
  ├─ execution_mode != "autonomous" → 400
  ├─ wallet_id missing → 400
  ├─ All customer fields present → 400 if not
  ├─ get_wallet(wallet_id) → 404 if not found
  ├─ get_slot_by_id → 404 if not found
  ├─ Already booked → 409
  ├─ Start time passed → 410
  ├─ our_price > 0 → 400 if not
  │
  ├─ Balance check: wallet.balance ≥ our_price → 402 if insufficient
  ├─ Spending limit check: our_price ≤ spending_limit_cents → 403 if exceeded
  │
  ├─ 5-minute idempotency key (slot+email+wallet+time bucket)
  │    └─ Same request already in-flight → 409
  │
  ├─ Write crash-recovery record (wallet_debited=false)
  ├─ debit_wallet(wallet_id, amount_cents)     ← DEBIT BEFORE BOOKING (crash-safe)
  │    ├─ raises ValueError if insufficient (double-check)
  │    └─ DEBIT FAILED → delete recovery record → 500
  ├─ Update crash-recovery record (wallet_debited=true)
  │
  ├─ _fulfill_booking(slot_id, customer, platform, booking_url, quantity)
  │    Returns 3-tuple: (confirmation, booking_meta, supplier_reference)
  │
  ├─ FAILURE:
  │    ├─ credit_wallet(wallet_id, amount_cents, "Refund: failed booking")
  │    │    └─ CREDIT FAILS → log "manual refund needed" (no automatic recovery)
  │    ├─ Mark recovery record resolved: "refunded"
  │    ├─ Send "booking_failed" email (non-fatal if fails)
  │    └─ Return { status: "failed", wallet_refunded: true }
  │
  └─ SUCCESS:
       ├─ _mark_booked(slot_id)
       ├─ Save booking record:
       │    { confirmation, supplier_reference, payment_method: "wallet", wallet_id,
       │      service_name, business_name, location_city, start_time,
       │      price_charged (= our_price × quantity), quantity, status: "booked", ... }
       ├─ Mark recovery record resolved: "completed"
       ├─ Send "booking_confirmed" email with confirmation # and cancel link (non-fatal)
       │    cancel_url only built if BOOKING_SERVER_HOST env var is set
       └─ Return { status: "confirmed", confirmation_number, wallet_balance_remaining }


CRASH RECOVERY (runs at server startup):
  _reconcile_pending_debits() scans "pending_exec_*" records in Supabase Storage
  ├─ wallet_debited=false → pre-debit crash → mark resolved (no refund needed)
  └─ wallet_debited=true, resolved=false → post-debit crash → credit_wallet() refund
```

---

## 5. Process 4: Autonomous Booking — Saved Stripe Card

**Entry point:** POST /api/customers/{customer_id}/book
**Requires:** X-API-Key header (internal agents only — not exposed to users)
**Status:** ❌ BROKEN — confirmation stored as tuple string (Bug #11)
**Quantity support:** ❌ NOT SUPPORTED (no quantity arg — always books 1)

This is the FOURTH booking entry point. It was not mapped in v1 or v2.
It allows a registered customer with a saved Stripe payment method to book without
going through the checkout page. The booking is fully autonomous — no human payment step.

```
POST /api/customers/{customer_id}/book
  { slot_id, customer_name, customer_email, customer_phone }
  │
  ├─ X-API-Key validation → 401 if invalid
  ├─ Validate required fields → 400 if missing
  ├─ get_slot_by_id → 404 if not found
  ├─ Already booked → 409
  ├─ Start time passed → 410
  ├─ our_price > 0 → 400 if not
  │
  ├─ GET /api/customers/{customer_id}:
  │    ├─ Load customer record from Supabase Storage
  │    ├─ NOT FOUND → 404
  │    └─ stripe_customer_id + stripe_payment_method_id required → 400 if absent
  │
  ├─ Stripe: stripe.PaymentIntent.create(
  │    amount = our_price_cents,
  │    currency = "usd",
  │    customer = stripe_customer_id,
  │    payment_method = stripe_payment_method_id,
  │    capture_method = "manual",           ← HOLD, NOT CHARGE
  │    off_session = True,                  ← no human interaction
  │    confirm = True                       ← charge attempt is immediate
  │    )
  │    ├─ Stripe SUCCESS → payment_intent created, hold captured
  │    └─ Stripe FAILURE (card declined etc.) → 402 { error }
  │
  ├─ _fulfill_booking(slot_id, customer, platform, booking_url)
  │    ← ❌ BUG: no quantity arg — always books 1 person
  │    Returns 3-tuple: (confirmation, booking_meta, supplier_reference)
  │    ← ❌ BUG (Bug #11): code does:
  │         confirmation = _fulfill_booking(...)
  │         This assigns the entire 3-tuple to `confirmation`
  │         Python does NOT crash — it silently stores the tuple
  │         booking record stores tuple.__str__() as confirmation number
  │         Supplier reference is never extracted or stored
  │
  ├─ FAILURE:
  │    ├─ stripe.PaymentIntent.cancel(payment_intent_id) ← HOLD RELEASED
  │    ├─ Save booking record: status="failed"
  │    └─ Send "booking_failed" email
  │
  └─ SUCCESS:
       ├─ stripe.PaymentIntent.capture(payment_intent_id) ← CARD CHARGED
       ├─ _mark_booked(slot_id)
       ├─ Save booking record to Supabase Storage:
       │    { booking_id, confirmation = "<tuple string>",  ← ❌ wrong (Bug #11)
       │      payment_intent_id, status: "booked",
       │      payment_method: "stripe_saved_card",
       │      customer_id, ... }
       │    ← ❌ BUG: supplier_reference NOT stored
       └─ Send "booking_confirmed" email with cancel link
            cancel link: /cancel/{booking_id}?t={hmac_token}  ← cancel link IS present ✅


CUSTOMER REGISTRATION (prerequisite for saved card booking):
POST /api/customers/register
  { customer_name, customer_email, customer_phone }
  │
  ├─ X-API-Key required → 401
  ├─ Check if customer already registered → 409 if duplicate email
  ├─ Create Stripe Customer object → stripe_customer_id
  ├─ Create Stripe SetupIntent → client_secret returned to caller
  │    Caller must complete card setup client-side using the client_secret
  ├─ Save customer record to Supabase Storage
  └─ Return { customer_id, setup_intent_client_secret }
```

---

## 6. Process 5: Autonomous Booking — Execute/Guaranteed

**Entry point:** POST /execute/guaranteed
**Requires:** wallet_id OR payment_intent_id (NO API key required — open endpoint)
**Status:** ⚠️ Partially implemented | ❌ Multiple critical gaps
**Quantity support:** ❌ NOT SUPPORTED (always 1 — see Bug #3)

```
POST /execute/guaranteed
  { slot_id (optional), category, city, hours_ahead, budget, allow_alternatives,
    customer: {name, email, phone},
    wallet_id OR payment_intent_id }
  │
  ├─ customer fields present → 400 if not
  ├─ wallet_id or payment_intent_id required → 400
  ├─ Load execution_engine.py → 500 if not found
  │
  ├─ Load all matching slots from Supabase (limit=10000)
  │
  └─ ExecutionEngine.execute(request):
       │
       ├─ Compute confidence score (0.0–1.0) based on matching slot count + data freshness
       │
       ├─ Try up to 7 strategies in order:
       │    1. exact:         Original slot_id (if provided)
       │    2. exact:         Retry same slot (transient failure retry)
       │    3. similar:       Same category+city, within ±2h of original start
       │    4. category_city: Same category+city, any time within hours_ahead
       │    5. any_platform:  Same as category_city (no additional platform filter)
       │    6. metro:         Partial city match (NYC → New York, Brooklyn, etc.)
       │    7. alternatives:  Relax category entirely (if allow_alternatives=true)
       │
       ├─ For each candidate slot:
       │    │
       │    ├─ _attempt_booking(slot, customer):
       │    │    complete_booking(slot_id, customer, platform, booking_url)
       │    │    ← ❌ BUG: quantity not passed — always books 1 person (Bug #3)
       │    │
       │    ├─ BOOKING SUCCESS → handle payment:
       │    │    ├─ payment_method == "wallet":
       │    │    │    _charge_wallet() AFTER booking ⚠️ debit-after pattern (A-11 skipped)
       │    │    │    → wallet charge fails → _cancel_octo()
       │    │    │         → fails → _queue_failed_octo_cancel() to retry queue ✅ (A-3)
       │    │    │         _cancel_octo header fixed ✅ (A-4)
       │    │    │
       │    │    ├─ payment_method == "stripe_pi":
       │    │    │    _capture_stripe(payment_intent_id)
       │    │    │    → capture fails → _cancel_octo() + _cancel_stripe()
       │    │    │         → cancel fails → _queue_failed_octo_cancel() ✅ (A-3)
       │    │    │
       │    │    └─ payment_method == "stripe_checkout":
       │    │         No payment action here — Stripe webhook handles capture
       │    │
       │    ├─ PAYMENT OK → mark slot booked (in-memory + .tmp/booked_slots.json)
       │    │    → _make_receipt() → Supabase Storage (20-field record) ✅ (A-2)
       │    │         stores: customer_name/phone, wallet_id, payment_intent_id, slot_id,
       │    │                 supplier_id, supplier_reference, start_time, location_city,
       │    │                 business_name, currency, payment_method
       │    │
       │    ├─ BOOKING FAILURE → log, try next strategy
       │    └─ PAYMENT FAILURE after booking → _cancel_octo() (fixed header ✅ A-4)
       │         → fails → _queue_failed_octo_cancel() (retry queue) ✅ (A-3)
       │
       ├─ All 7 attempts exhausted:
       │    └─ payment_method == "stripe_pi" → cancel the hold
       │         payment_method == "wallet" → NO REFUND (wallet was never charged here)
       │
       └─ Return ExecutionResult { success, confirmation, attempt_log, fallbacks_used }
```

---

## 7. Process 6: Semi-Autonomous — /api/execute Intent

**Entry point:** POST /api/execute
**Status:** ⚠️ Implemented | Agent selects slot, human still pays via Stripe
**Quantity support:** ❌ NOT SUPPORTED (hardcoded quantity=1 in /api/book call)

```
POST /api/execute
  { category, city, budget, hours_ahead, customer: {name, email, phone} }
  │
  ├─ Validate customer fields → 400
  ├─ GET /slots with filters → find soonest priced slot
  │    └─ NO SLOTS → 404
  ├─ POST /api/book { slot_id, customer, quantity: NOT PASSED (defaults to 1) }
  │    → Returns checkout_url
  └─ Return { checkout_url, selected_slot }

NOTE: Customer must still open checkout_url and pay manually.
This is NOT autonomous — it is agent-assisted slot selection with human payment.
```

---

## 8. Process 7: Intent Sessions

**Entry point:** POST /intent/create
**Requires:** X-API-Key header
**Status:** ⚠️ Implemented | ❌ Critical persistence gap | ❌ Price trigger broken on Railway
**Quantity support:** ❌ NOT SUPPORTED

```
POST /intent/create
  { goal, constraints, customer, payment: {method, wallet_id}, autonomy, ttl_hours }
  │
  ├─ goal: "find_and_book" | "monitor_only" | "price_alert"
  ├─ autonomy: "full" (auto-execute) | "notify" (alert only) | "monitor" (never execute)
  │
  └─ Create intent session → saved to Supabase Storage "bookings/intent_sessions.json"
       ✅ FIXED (A-5): Primary Supabase; .tmp/ local fallback. Survives redeploys.


GET /intent/{id}      — status poll (API key required, ownership verified)
POST /intent/{id}/execute — manually trigger "notify" intent (temporarily upgrades to "full")
POST /intent/{id}/cancel  — marks cancelled (no booking cleanup if mid-execution)


IntentMonitor thread (daemon, starts with server, sweeps every 60 seconds):
  │
  ├─ Load all sessions from Supabase Storage (fallback: .tmp/intent_sessions.json)
  ├─ For each active (non-expired, non-completed, non-cancelled) session:
  │    │
  │    ├─ EXPIRED? → mark "expired", fire callback
  │    │
  │    ├─ goal == "price_alert":
  │    │    _check_price_trigger(session):
  │    │    ├─ Load .tmp/aggregated_slots.json
  │    │    │    ← ❌ BUG: this file only exists on laptop, NOT on Railway (Bug #12)
  │    │    │    ← On Railway: FileNotFoundError or empty — price alerts never trigger
  │    │    ├─ Find slots matching constraints within price threshold
  │    │    └─ MATCH FOUND → fire price_alert callback, mark "completed"
  │    │
  │    ├─ goal == "find_and_book" + autonomy == "full":
  │    │    execute_intent(session):
  │    │    ├─ Calls ExecutionEngine (same as execute/guaranteed)
  │    │    ├─ SUCCESS → write booking record to Supabase + .tmp/bookings/ [FIXED B-4]
  │    │    │         → fire booking_completed callback, send email, mark "completed"
  │    │    └─ FAILURE → stays "monitoring", fire attempt_failed callback, retry next sweep
  │    │
  │    └─ goal == "monitor_only" / autonomy == "notify":
  │         Check for matching slots → fire callback if found, do NOT book
  │
  └─ Write updated sessions to Supabase Storage + .tmp/intent_sessions.json
```

---

## 9. Process 8: Cancellation Matrix

This is the most critical section. All paths documented separately, covering all 4 booking entry points.

### 8A: Customer Cancels — Human (Stripe) Booking

**Entry: GET/POST /cancel/{booking_id}?t={token}**

```
GET /cancel/{booking_id}?t={token}
  ├─ Verify HMAC token → 403 if invalid
  ├─ Load booking record → 404 if not found
  ├─ Already cancelled → show "already cancelled" page
  └─ Show confirmation page (with "Confirm Cancellation" button)

POST /cancel/{booking_id}?t={token}  [form submit]
  ├─ Verify HMAC token → 403
  ├─ Load booking record
  │
  ├─ refund_issued = False  ← initialized before POST block (C-2 fix)
  │
  ├─ _refund_stripe(payment_intent_id):
  │    ├─ PI status "requires_capture" → cancel hold (customer never charged)
  │    ├─ PI status "succeeded" → full refund created
  │    ├─ PI already cancelled/refunded → treated as success
  │    └─ Retry 3× with backoff → fails after 3 → log, continue
  │
  ├─ OCTO cancellation (if OCTO platform):
  │    is_octo = supplier_id in octo_platforms OR platform == "octo"  (C-3 fix)
  │    _cancel_octo_booking(supplier_id, confirmation)
  │    ├─ SUCCESS → booking released on supplier
  │    └─ FAILURE (transient) → _queue_octo_retry() → background retry every 15 min ✅
  │    └─ FAILURE (permanent 4xx) → log, no retry
  │
  ├─ C-4 fix: Stripe-outcome-dependent record write:
  │    stripe_ok → status="cancelled", cancelled_at=now
  │    Stripe failed → status="cancellation_refund_failed", cancellation_flag_at=now
  │    (matches DELETE /bookings/{id} logic — enables future monitoring/retry)
  │
  ├─ record["cancelled_by"] = "customer_self_serve"
  ├─ Send cancellation email (cancelled_by_customer=True → "You cancelled" copy) (C-8 fix)
  └─ Show "Booking cancelled" page with refund notice
```

### 8B: Customer Cancels — Saved Stripe Card Booking

**Entry: GET/POST /cancel/{booking_id}?t={token}**
**Same self-serve flow as 8A — cancel link IS included in confirmation email**

```
Same as 8A, with differences:
  │
  ├─ _refund_stripe(payment_intent_id):
  │    payment_intent_id = real PI from saved-card flow → refund works ✅
  │
  ├─ OCTO cancellation:
  │    Uses booking record's "confirmation" field
  │    ← ❌ BUG (Bug #11): confirmation is stored as tuple string — OCTO cancel call will fail
  │    ← ❌ BUG (Bug #5): failure not queued for retry
  │
  └─ Customer IS refunded; supplier booking remains active (OCTO cancel fails)
```

### 8C: Customer Cancels — Autonomous (Wallet) Booking

**Entry: DELETE /bookings/{booking_id} (API key required)**
**No self-serve cancel link — wallet bookings' confirmation emails do NOT include a cancel URL**

```
DELETE /bookings/{booking_id}
  ├─ X-API-Key required → 401
  ├─ Load booking record → 404
  ├─ Already cancelled → 200 (idempotent)
  │
  ├─ _refund_stripe(payment_intent_id):
  │    payment_intent_id = "" for wallet bookings
  │    ← ❌ BUG (Bug #6): Stripe retrieve("") → silently no-ops or errors
  │    No wallet credit-back issued
  │
  ├─ OCTO cancellation:
  │    _cancel_octo_booking(supplier_id, confirmation)
  │    ├─ SUCCESS → booking released
  │    └─ FAILURE transient → _queue_octo_retry() → background retry every 15 min ✅
  │    └─ FAILURE permanent (4xx) → log, no retry
  │
  ├─ Update record: status="cancelled"
  └─ Return { success, refund_id (empty), octo_queued_for_retry }

← ❌ BUG: Wallet bookings get no refund on cancellation (Bug #6)
         Stripe path is called with empty payment_intent_id
         No credit_wallet() call anywhere in the cancel path
```

### 8D: Customer Cancels — Execute/Guaranteed Booking

**No customer-facing cancel path — booking record is not in Supabase → no cancel link possible**

```
← ❌ BUG (Bug #7): execute/guaranteed bookings are not in Supabase Storage
   GET /bookings/{id} returns 404
   GET/POST /cancel/{id} returns 404
   DELETE /bookings/{id} returns 404
   Customer has no way to cancel; admin has no way to cancel
   Only path: manual OCTO cancellation via direct API call
```

### 8E: Supplier Cancels — Human (Stripe) Booking

**Entry: POST /api/bokun/webhook?token={token}**

```
Bokun POSTs when supplier cancels in their dashboard
  │
  ├─ Token auth → 401 if wrong
  ├─ Parse: booking_data["status"], booking_data["confirmationCode"]
  ├─ Not a cancellation event → 200 "event_ignored"
  │
  ├─ _find_booking_by_confirmation(confirmationCode):
  │    Scans Supabase Storage (O(n) scan — no index)
  │    Matches on: record["confirmation"] == code OR record["supplier_reference"] == code
  │    └─ NOT FOUND → 200 "not_found"
  │
  ├─ _refund_stripe(payment_intent_id):
  │    ├─ PI "requires_capture" → cancel hold
  │    ├─ PI "succeeded" → full refund ✅
  │    └─ FAILURE → log prominently, continue
  │
  ├─ Update record: status="cancelled", cancelled_by="supplier_bokun_webhook"
  └─ Send cancellation email to customer
```

### 8F: Supplier Cancels — Saved Stripe Card Booking

**Entry: POST /api/bokun/webhook?token={token}**

```
Same as 8E, BUT:
  ├─ _find_booking_by_confirmation(confirmationCode):
  │    record["confirmation"] = "<tuple string>" ← ❌ BUG (Bug #11)
  │    Tuple string will NOT match Bokun's confirmationCode
  │    Webhook returns "not_found" — customer never refunded
  │
  └─ Stripe refund: NEVER TRIGGERED for saved-card bookings on supplier cancel
     ← ❌ BUG: combination of Bug #11 and the lookup failure
```

### 8G: Supplier Cancels — Autonomous (Wallet) Booking

**Entry: POST /api/bokun/webhook?token={token}**

```
Same flow as 8E, BUT:
  ├─ _find_booking_by_confirmation(confirmationCode):
  │    Wallet booking records DO NOT store supplier_reference ← ❌ BUG (Bug #4)
  │    OCTO uuid (confirmation) may match only if Bokun sends the OCTO uuid
  │    If Bokun sends their own Bokun reference → no match → not_found
  │
  └─ _refund_stripe(payment_intent_id):
       payment_intent_id = "" for wallet bookings
       ← ❌ BUG (Bug #6): no wallet credit-back
       Wallet is never refunded on supplier cancellation of wallet bookings
```

### 8H: Supplier Cancels — Execute/Guaranteed Booking

**Entry: POST /api/bokun/webhook?token={token}**

```
← ❌ BUG (Bug #7): execute/guaranteed bookings are not in Supabase Storage
   _find_booking_by_confirmation() will never find these records
   Supplier cancel → "not_found" → customer never refunded, record never updated
```

### 8I: Cancellation Retry Queue (OCTO failures)

**Tool: retry_cancellations.py — runs every 15 min via APScheduler on Railway**

```
_queue_octo_retry() writes to Supabase Storage: cancellation_queue/{booking_id}.json

retry_cancellations.py (every 15 min):
  │
  ├─ Load all files from Supabase Storage "cancellation_queue/" prefix
  ├─ For each record:
  │    ├─ MAX ATTEMPTS EXCEEDED (48 attempts = 12 hours) → log "giving up", delete from queue
  │    ├─ _cancel_octo_booking(supplier_id, confirmation):
  │    │    ├─ SUCCESS (2xx) → delete from queue ✅
  │    │    ├─ 404           → treat as success (booking already gone) → delete ✅
  │    │    ├─ 400/401/403/422 → permanent failure → log, delete from queue (won't recover)
  │    │    └─ 5xx / timeout → increment attempt_count → keep in queue (retry next cycle)
  │    └─ Update record in Supabase
  │
  └─ No retry if circuit breaker is OPEN for that supplier
```

**Which cancellation paths populate the retry queue:**
- ✅ DELETE /bookings/{id} — queues on OCTO failure
- ✅ GET/POST /cancel/{booking_id} (self-serve) — queues on OCTO failure (corrected in system map; code always did this)
- ❌ Bokun webhook supplier cancel — does NOT queue on OCTO failure at all
- ❌ Execute/Guaranteed — no Supabase record, no retry possible

### 8J: Peek Webhook — Supplier Verification (Flags Only)

**Entry: GET /webhooks/peek?booking_id={id}&supplier_id={sid}**

```
GET /webhooks/peek
  │
  ├─ Calls OCTO GET /bookings/{octo_uuid} for the booking
  ├─ Status CANCELLED or EXPIRED:
  │    → Flags booking as "reconciliation_required" in Supabase Storage
  │    ← ❌ Does NOT trigger a refund
  │    ← ❌ Does NOT trigger OCTO cancellation
  │    ← Does NOT send customer notification
  │    → Manual review required to process refund
  └─ Status OK → no action
```

---

## 10. Process 9: Supplier-Initiated Cancellation (Bokun Webhook)

**Entry point:** POST /api/bokun/webhook?token={BOKUN_WEBHOOK_TOKEN}
**Status:** ✅ Auth working (smoke tested 2026-04-16) | ❌ Wallet/saved-card/execute gaps (see Cancellation Matrix 8F–8H)

```
Bokun: POST /api/bokun/webhook?token=...
  │
  ├─ Token: hmac.compare_digest(BOKUN_WEBHOOK_TOKEN, request.args["token"])
  │    ├─ WRONG → 401
  │    └─ BOKUN_WEBHOOK_TOKEN not set → WARNING log, allow through (insecure)
  │
  ├─ Parse: booking_data = data["booking"] OR data (handles nested/flat Bokun formats)
  │    confirmation = booking_data["confirmationCode"] / "confirmation_code" / "id"
  │    status = booking_data["status"].upper()
  │
  ├─ Not a cancellation → 200 "event_ignored" (Bokun also sends create/modify events)
  │
  ├─ _find_booking_by_confirmation(confirmation):
  │    Scans ALL Supabase Storage booking files (O(n) scan — no index)
  │    Matches: record["confirmation"] == code OR record["supplier_reference"] == code
  │    └─ NOT FOUND → 200 "not_found"
  │
  ├─ _refund_stripe(payment_intent_id):  [3× retry with backoff]
  │    For Stripe bookings: ✅ works
  │    For wallet bookings: ❌ no wallet credit-back (Bug #6)
  │    For saved-card bookings: ❌ never reached (Bug #11 prevents lookup match)
  │    For execute/guaranteed: ❌ never reached (not in Supabase)
  │
  ├─ Update record: status="cancelled", cancelled_by="supplier_bokun_webhook"
  └─ Send cancellation email to customer
```

---

## 11. Process 10: MCP Agent Integration

**Entry points:**
- `POST /mcp` — MCP-over-HTTP (Smithery, direct API agents) — on Flask server
- `GET /sse` + `POST /messages` — SSE proxied to embedded FastMCP thread
- `api.lastminutedealshq.com/mcp` — also serves as MCP endpoint (same Railway service; run_mcp_remote.py used by Smithery on their infra)

**Status:** ✅ search_slots, get_supplier_info working | ✅ book_slot returns checkout_url | ⚠️ Human Stripe payment still required for bookings

```
MCP tool: search_slots(city, category, hours_ahead, max_price)
  ├─ Cache hit (60s TTL, keyed on city|category|hours_ahead|max_price) → return cached
  ├─ _load_slots_from_supabase() — full pagination, no agent-facing limit
  ├─ _sanitize_slot(): strips internal fields, recomputes hours_until_start dynamically
  └─ Store in cache, return list
  ← NOTE: POST /mcp path calls Supabase directly (avoids gunicorn deadlock)
  ← FastMCP SSE path calls GET /slots via HTTP; both now return full inventory

MCP tool: book_slot(slot_id, customer_name, customer_email, customer_phone, quantity)
  ├─ POST /api/book internally → creates Stripe checkout
  ├─ Returns { checkout_url, booking_id, service_name, start_time, price_per_person, total_price, action_required }
  │   ← B-23 FIXED: response now includes price + service context so agents don't need a follow-up status call
  ├─ Immediately emails checkout_url to customer_email (checkout_created template)
  │   ← B-24 FIXED: customer gets payment link even if agent doesn't surface it
  └─ booking record saves our_price + price_charged at creation (not just at fulfillment)
     ← B-22 FIXED: get_booking_status now returns price_per_person in pending_payment state

MCP tool: get_booking_status(booking_id)
  └─ GET /bookings/{booking_id} → returns record (now includes price_per_person field)

MCP tool: preview_slot(slot_id)
  ├─ Looks up slot via get_slot_by_id (JSON-RPC) or GET /slots/{id}/quote (FastMCP/remote)
  ├─ Returns { booking_page_url, service_name, start_time, location_city, price, currency }
  └─ booking_page_url → GET /book/{slot_id} → HTML page where human enters details + pays
  ← NEW: Added v30. Preferred flow for human-in-the-loop bookings (no need to collect details)

MCP tool: get_supplier_info()
  ├─ _get_live_supplier_directory() — queries Supabase for distinct (business_name, city, country)
  ├─ Groups client-side, 5-minute cache
  └─ Falls back to _SUPPLIER_DIR_STATIC (14 known Bokun suppliers) if Supabase unreachable
  ← Previously: TWO diverging hardcoded lists (POST /mcp: 9 suppliers; FastMCP SSE: 7 suppliers)
  ← Both implementations were missing Vakare Travel Service (61% of OCTO inventory) — FIXED
```

**Booking page flow (NEW v30):**
```
Agent calls preview_slot(slot_id)
  → Returns booking_page_url: https://api.lastminutedealshq.com/book/{slot_id}
  → Agent shares URL with user
  → User clicks URL → GET /book/{slot_id} → HTML page with slot details + form
  → User fills name/email/phone/quantity → POST /book/{slot_id}/checkout
  → Server calls /api/book internally → Stripe checkout session created → redirect to Stripe
  → User pays → webhook confirms booking with supplier
```

**Smithery connection path:** Smithery → `run_mcp_remote.py` (Streamable HTTP transport) → Railway REST API
  ← Previously used SSE transport (deprecated April 1 2026); caused 20.6% Unavailable on tools/call — FIXED
**Claude Desktop path:** `GET /sse` → proxied SSE → embedded FastMCP

---

## 12. Process 11: Wallet System

**Tool: manage_wallets.py**
**Storage: Supabase Storage — `config/wallets.json` (ALL wallets in ONE file)**
**Status:** ⚠️ Implemented | ❌ Concurrency risk | ❌ No refund on wallet booking cancellation

```
CREATE:   POST /api/wallets/create → { wallet_id, api_key, balance: 0 }
          Writes new entry to config/wallets.json

FUND:     POST /api/wallets/fund   → Stripe checkout for top-up
          → checkout.session.completed → wallet_topup fast path → credit_wallet()

BALANCE:  GET /api/wallets/{id}/balance (requires wallet api_key)
HISTORY:  GET /api/wallets/{id}/transactions
LIMIT:    PUT /api/wallets/{id}/spending-limit


Internal functions:
  get_wallet(wallet_id)         → loads config/wallets.json, finds by id
  debit_wallet(id, cents)       → raises ValueError if insufficient (not bool return)
  credit_wallet(id, cents, note)→ returns bool (True/False)
  create_topup_session()        → Stripe checkout for wallet funding


⚠️ CONCURRENCY RISK (not a bug — a design limitation):
  ALL wallets share a single JSON file: config/wallets.json in Supabase Storage
  Pattern: download → parse → modify → upload
  Under concurrent requests:
  ├─ Request A reads file (balance: $100)
  ├─ Request B reads file (balance: $100)
  ├─ Request A debits $50, uploads (balance: $50)
  └─ Request B debits $50, uploads (balance: $50) ← OVERWRITES A's write
     Net result: $100 debited but file shows $50 (both debits "succeed")
  Risk level: LOW for single-user wallets; HIGH if wallet is used for concurrent bookings
  Fix: Supabase Postgres row-level locking, or per-wallet file with optimistic locking


Wallet booking payment timing:
  book_direct:          DEBIT BEFORE booking attempt (crash-safe, correct pattern)
  execution_engine:     DEBIT AFTER booking success (Bug #8 — double-spend risk)

Wallet booking cancellation:
  ✅ FIXED (A-1): credit_wallet() called in all 3 paths: DELETE /bookings, self_serve_cancel,
     bokun_webhook. Called after Stripe refund step. Non-fatal — logs on failure.
```

---

## 13. Process 12: Background Services (APScheduler)

**Runs in-process on Railway Flask server — started by `_start_retry_scheduler()` at app init**

### Job 1: retry_cancellations (every 15 minutes)

```
See Process 8I (Cancellation Retry Queue) for full detail.
Reads: Supabase Storage "cancellation_queue/" prefix
Writes: deletes from queue on success/permanent-failure, increments count on transient failure
Max attempts: 48 (12 hours at 15-min intervals)
```

### Job 2: reconcile_bookings (every 30 minutes) [3 sub-jobs]

**Tool: reconcile_bookings.py**

```
reconcile_bookings():
  │
  ├─ Load ALL booking records from Supabase Storage "bookings/" prefix
  ├─ Filter: status == "booked" (only active bookings need reconciliation)
  ├─ For each booking:
  │    ├─ booking["platform"] != "octo" → skip (non-OCTO bookings not reconcilable)
  │    ├─ GET OCTO: GET /bookings/{confirmation_uuid} using booking's supplier_id
  │    │
  │    ├─ OCTO returns booking: status OK → no action (booking is confirmed with supplier)
  │    │
  │    ├─ OCTO returns "not_found" (404):
  │    │    → Update record: status = "reconciliation_required", reconciliation_flag_at = now
  │    │    → Job 2 (below) acts on it after ≥35 min (two-cycle guard)
  │    │
  │    └─ Transient error (5xx, timeout):
  │         → Skip this booking, retry next cycle
  │
  └─ NOTE: execute/guaranteed bookings ARE in Supabase (fixed A-2) → reconciled

Job 2 (A-6): act on reconciliation_required (every 30 min):
  ├─ Two-cycle guard: skip if reconciliation_flag_at < 35 min ago
  ├─ Issue Stripe refund (_refund_stripe_once)
  ├─ Wallet credit-back (if payment_method="wallet")
  ├─ status → "cancelled" (Stripe OK) or "cancellation_refund_failed" (Stripe fail)
  └─ Send cancellation email to customer ✅ FIXED (A-6)

Job 3 (A-15): retry cancellation_refund_failed (every 30 min):
  ├─ No payment_intent_id? → wallet credit-back + mark cancelled
  ├─ Retry Stripe refund
  │    ├─ SUCCESS → wallet credit-back + mark cancelled + email customer ✅ FIXED (A-15)
  │    └─ FAILURE → increment refund_retry_count, retry next cycle
```

### Circuit Breaker (per supplier, cross-cutting)

**Tool: circuit_breaker.py**
**Storage: Supabase Storage — `circuit_breaker/{supplier_id}.json` (persists across redeploys)**

```
States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (probe allowed)

Thresholds:
  consecutive_failures ≥ 5 → trip to OPEN state
  OPEN cooldown = 300 seconds (5 minutes)
  After cooldown → transition to HALF_OPEN (allows one probe request)

State transitions:
  record_failure():
    ├─ CLOSED: increment consecutive_failures
    │    consecutive_failures ≥ 5 → set OPEN, record opened_at
    ├─ HALF_OPEN: → OPEN (probe failed)
    └─ OPEN: no state change

  record_success():
    ├─ CLOSED: reset consecutive_failures to 0
    └─ HALF_OPEN: → CLOSED, reset counter

  is_open(supplier_id):
    ├─ OPEN + elapsed < 300s → return True (reject request)
    ├─ OPEN + elapsed ≥ 300s → transition to HALF_OPEN, return False (allow probe)
    └─ CLOSED / HALF_OPEN → return False

Usage:
  _fulfill_booking() checks is_open() before calling OCTOBooker.run()
  → OPEN → raise BookingUnavailableError (no OCTO call made)
  OCTOBooker.run() calls record_success() on 2xx, record_failure() on repeated failures

Admin endpoint: GET /admin/circuit-breaker → get_all_states() from all supplier files
```

---

## 14. Process 13: Webhook Subscriber Notifications

**Status:** 🔲 Not yet built (notify_webhooks.py was removed in Session 24 cleanup).
If needed in future: build a Railway-side job that queries Supabase for new slots
and POSTs to subscriber callback URLs.

---

## 15. Supporting & Utility Endpoints (Non-Booking)

These endpoints exist and are wired but were not covered in the main booking flows.

| Endpoint | Auth | Purpose | Data source | Status |
|---|---|---|---|---|
| GET /health | None | Slot count + DB success rates | Supabase (slot count) + Postgres (rates always null — TCP blocked) | ✅ partial |
| GET /metrics | None | Public perf beacon: slot count, platform count, success rates, fill velocity, **api_usage** (in-memory deque, last 1h/24h/since-deploy by path+source) | Supabase slots + in-memory request log | ✅ |
| GET /slots | None | Search slots with category/city/hours_ahead/max_price filter | Supabase REST, paginated (1000/page), falls back to .tmp/ | ✅ |
| GET /slots/{slot_id}/quote | None | Confirm availability + price for one slot (now includes business_name — B-148 FIXED) | Supabase + .tmp/booked_slots.json | ⚠️ dedup lost on redeploy |
| GET /book/{slot_id} | None | HTML booking page — slot details + form for name/email/phone/qty + Stripe checkout | Supabase via get_slot_by_id | ✅ NEW v30 |
| POST /book/{slot_id}/checkout | None | Form POST → creates Stripe session via /api/book loopback → redirect to Stripe | Internal POST to /api/book | ✅ NEW v30 |
| POST /api/keys/register | None | Register free API key (name + email → lmd_... key) | Supabase Storage config/api_keys.json ✅ persists | ✅ |
| POST /test/dry-run | X-API-Key | Trigger dry-run fulfillment (no real booking, no charge) | Uses get_slot_by_id → Supabase | ✅ |
| GET /verify/{booking_id} | None | Public receipt verification with HMAC signature check | Supabase Storage | ✅ |
| GET /bookings/{booking_id} | None | Booking status poll | Supabase Storage | ✅ (execute/guaranteed not found — Bug #7) |
| GET /intent/list | X-API-Key | List all intent sessions for caller | .tmp/intent_sessions.json (lost on redeploy) | ❌ |
| GET /insights/market | None | Market intelligence: success rates, fill velocity, optimal windows | .tmp/insights/ — empty after Railway redeploy | ❌ on Railway |
| GET /insights/platform/{name} | None | Per-platform reliability stats | .tmp/insights/ — same gap | ❌ on Railway |
| GET /api/watcher/status | None | Real-time watcher health + last poll timestamps | .tmp/watcher_status.json — local only, watcher not running | ❌ on Railway |
| POST /api/subscribe | None | SMS opt-in from landing page | .tmp/sms_subscribers.json — local only (Bug #22) | ❌ on Railway |
| POST /api/webhooks/subscribe | None | Subscribe to deal alert webhook | .tmp/webhook_subscriptions.json — local only (Bug #13) | ❌ on Railway |
| POST /api/webhooks/unsubscribe | None | Cancel webhook subscription | .tmp/webhook_subscriptions.json — local only | ❌ on Railway |
| POST /admin/refresh-slots | X-API-Key | Run full slot pipeline in-process on Railway | Writes to Supabase, .tmp/ | ⚠️ |
| GET /api/inbound-email/list | X-API-Key | List stored inbound emails | Supabase Storage inbound_emails/ | ✅ |
| GET /sse | None | SSE stream — proxied to embedded FastMCP thread | — | ✅ |
| POST /messages | None | FastMCP message handler | — | ✅ |
| GET /mcp | None | MCP server info + tool list | — | ✅ |
| POST /mcp | X-API-Key optional | MCP JSON-RPC 2.0 tool calls | — | ✅ |

---

## 15a. Disabled Platforms & Inactive Tools

All non-OCTO fetchers, Playwright bookers, and debug scripts were deleted in Sessions 23-24.
Only `fetch_octo_slots.py` and `OCTOBooker` are active. `RezdyBooker` exists in
`complete_booking.py` but is dormant (no Rezdy fetch script in pipeline).

**SDK & client tools (not server-side components):**
- `lmd_sdk.py` — Python SDK wrapping the Railway API. Client-side only.
- `run_mcp_server.py` — LOCAL MCP server reading from `.tmp/` files on laptop. Not Railway.

---

## 16. Multi-Quantity Booking — All Paths

| Booking Path | Quantity Supported? | Notes |
|---|---|---|
| POST /api/book (human Stripe) | ✅ Yes (1-20) | quantity × per_person_price via Stripe line_item |
| POST /api/book/direct (wallet) | ❌ No (always 1) | Bug #2 — quantity not parsed or passed |
| POST /api/customers/{id}/book (saved card) | ❌ No (always 1) | quantity arg not implemented |
| POST /execute/guaranteed | ❌ No (always 1) | Bug #3 — not in ExecutionRequest, not passed to complete_booking |
| POST /execute/best | ❌ No (always 1) | Same gap as execute/guaranteed |
| POST /api/execute (semi-auto) | ❌ No (always 1) | quantity not passed to /api/book call |
| MCP book_slot | ✅ Passes quantity to /api/book | Customer still manually pays via Stripe |
| Intent sessions | ❌ No | Not in constraints object |

**Multi-quantity cancellation:**
- All cancellation paths issue FULL refund only — no partial refund support
- Customer cancels 2 of 3 seats: impossible — must cancel all or none
- No partial OCTO cancellation implemented

---

## 16. Infrastructure & Dependencies

| Component | Service | Status | Notes |
|---|---|---|---|
| Slot storage | Supabase "slots" table | ✅ | REST API only — direct Postgres TCP blocked from Railway |
| Booking records | Supabase Storage "bookings/" | ✅ | JSON files per booking, persists across Railway redeploys |
| Wallet storage | Supabase Storage "config/wallets.json" | ✅ | Single file for ALL wallets — concurrency risk under load |
| Circuit breaker state | Supabase Storage "circuit_breaker/" | ✅ | Per-supplier JSON, persists across redeploys |
| Cancellation queue | Supabase Storage "cancellation_queue/" | ✅ | Used by DELETE /bookings only |
| Inbound emails | Supabase Storage "inbound_emails/" | ✅ | SendGrid inbound parse → stored here |
| Request logs | In-memory deque (50k entries) | ✅ FIXED | Was Postgres (TCP blocked). Now in-memory — resets on redeploy but tracks all requests live. Powers /metrics api_usage. |
| Intent sessions | .tmp/intent_sessions.json | ❌ | LOCAL only — lost on every Railway redeploy |
| Webhook subscriptions | .tmp/webhook_subscriptions.json | ❌ | LOCAL only — never fires on Railway |
| Aggregated slots | .tmp/aggregated_slots.json | ⚠️ | LOCAL only — Railway reads from Supabase instead |
| Execute/guaranteed bookings | .tmp/booked_slots.json | ❌ | LOCAL only — lost on Railway redeploy |
| API server | Railway (web service) | ✅ | Auto-redeploys on git push |
| MCP Streamable HTTP server | Railway (mcp service) | ✅ | run_mcp_remote.py (transport changed SSE → streamable-http, 2026-04-18) |
| Payments | Stripe | ✅ | Checkout + webhooks + auth-capture + saved cards |
| Supplier booking | Bokun OCTO API | ✅ (API reachable) / ❌ (real end-to-end untested) | 17 vendor IDs, 417 products, ~6,400 total in marketplace |
| Bokun notifications | HTTP notification (URL token auth) | ✅ | Smoke tested 2026-04-16 |
| Email | SendGrid (primary) + SMTP (fallback) | ✅ | 4 email types wired |
| Landing page | Cloudflare Pages | ✅ | Rebuilt every pipeline run |
| Slot discovery | Railway APScheduler (every 4h) | ✅ | Local Task Scheduler DISABLED 2026-04-18 — Railway handles it 24/7 |
| Pricing history | Google Sheets | ❌ | OAuth token expired — urgency pricing disabled |
| Booked slot dedup | .tmp/booked_slots.json (ALL paths) | ❌ | Lost on Railway redeploy — fast dedup breaks; Stripe path degrades gracefully via OCTO 409; execute/guaranteed double-booking risk |
| Stripe customer records | .tmp/stripe_customers.json | ❌ | LOCAL ONLY — ALL saved-card customers lost on every Railway redeploy (Bug #20) |
| SMS subscriber list | .tmp/sms_subscribers.json | ❌ | LOCAL ONLY — subscriptions lost on Railway redeploy (Bug #22) |
| Market insights data | .tmp/insights/ directory | ❌ | LOCAL ONLY — /insights/market and /execute/best reliability data lost on redeploy (Bug #21) |
| Watcher status | .tmp/watcher_status.json | ❌ | LOCAL ONLY — watcher not running; /api/watcher/status always returns "not started" |
| SMS alerts | Twilio | 🔲 | Implemented, not activated |
| Social posting | Twitter/Reddit/Telegram | 🔲 | Scripts exist, not running |

---

## 17. Bug Register — Confirmed Code Defects

> All critical and high/medium code bugs confirmed in sessions 1–5 have been fixed.
> Remaining items below are architectural gaps (data persistence, missing features) not yet addressed.

### FIXED — Sessions 1–5 (code bugs resolved)

| # | Bug | Fixed in |
|---|---|---|
| 1 | `book_direct` 2-tuple unpack of 3-tuple return from `_fulfill_booking` → ValueError | b3116b9 |
| 2 | `OCTOBooker.run()` dict return not handled in `execution_engine._attempt_booking` | b3116b9 |
| 3 | Retry queue double-prefix path — entire cancellation retry queue non-functional | b3116b9 |
| 5 | `_signing_secret()` written to `.tmp/` → invalidated on every Railway redeploy | b3116b9 |
| 6 | `book_direct` recovery record partial-write destroys wallet_id/amount_cents | b3116b9 |
| 7 | Stripe idempotency check blocked retries for "failed" sessions | b3116b9 |
| 8 | Non-unique `booking_record_id` — concurrent bookings of same slot collide | b3116b9 |
| 9 | `GET /bookings/<id>` had no authentication (IDOR) | b3116b9 |
| 10 | `GET /verify/<id>` returned full PII publicly | b3116b9 |
| 11 | `book_with_saved_card` assigned 3-tuple to scalar `confirmation` | b3116b9 |
| 12 | Bokun webhook marked `"cancelled"` even when Stripe refund failed | b3116b9 |
| 13 | `self_serve_cancel` always sent "refund issued" email regardless of Stripe outcome | b3116b9 |
| 14 | `self_serve_cancel` silently dropped failed OCTO cancellations (no retry queued) | b3116b9 |
| 15 | `_find_booking_by_confirmation` hard 500-record limit | b3116b9 |
| 16 | `reconcile_bookings.py` hard 1000-record limit | b3116b9 |
| 17 | Peek webhook had no authentication | b3116b9 |
| 18 | `DELETE /bookings` marked cancelled even when Stripe refund AND OCTO cancel both failed | b3116b9 |
| 19 | APScheduler multi-worker guard used per-process env var → duplicate background jobs | 1d562ef |
| 20 | `stripe_customers.json` in `.tmp/` only → lost on Railway redeploy | 1d562ef |
| 21 | `_mark_booked()` had no thread safety | 2077e3f |
| 22 | `GenericBooker.complete()` returned fake success string instead of raising | b3116b9 |
| 23 | `book_direct` booking record omitted `supplier_reference` | b3116b9 |
| 24 | `debit_wallet()` never checked `spending_limit_cents` at wallet level | 1d562ef |
| 25 | Intent session stayed `"executing"` forever on unhandled engine exception | 1d562ef |
| 27 | OCTOBooker 409 re-resolution matched first slot without verifying `start_time` | 1d562ef |
| 28 | Playwright availability check blocked OCTOBooker (pure HTTP, no browser needed) | b3116b9 |
| 29 | `_validate_api_key` hit Supabase twice per request — no caching | 1d562ef |
| 31 | `_queue_octo_retry()` checked `SUPABASE_URL` but not `SUPABASE_SECRET_KEY` | 2077e3f |
| 34 | `list_inbound_emails` used `==` for auth (timing oracle) | 1d562ef |
| 35 | `execution_engine._cancel_stripe()` swallowed all exceptions silently | 1d562ef |
| 37 | `get_wallet_by_api_key()` triggered full Supabase round-trip on every wallet request | 2077e3f |
| 38 | Intent session reads had no `_sessions_lock` → race condition | 1d562ef |
| 39 | `_fire_callback()` blocked intent monitor thread with synchronous HTTP call | 1d562ef |
| 40 | `market_insights` module loaded via `exec_module` on every booking attempt | 1d562ef |
| 41/42 | `circuit_breaker.py` read Supabase env vars at import time → silently disabled | 1d562ef |
| 43 | Circuit breaker half-open allowed unlimited concurrent probes | 1d562ef |
| 44 | `create_topup_session()` called `_load_wallets()` twice (TOCTOU) | 1d562ef |
| 51 | `booked_slots.json` write was non-atomic → crash mid-write could corrupt file | 1d562ef |
| EE-4 | Hardcoded `+0.1` confidence floor → intent monitor booked with zero matching slots | 2077e3f |
| B-1 | `slot_count` undefined in `_compute_confidence()` → NameError crashed every confidence call | Session 14 |
| B-2 | Wallet credit-back in DELETE/self-serve cancel paths ran before `stripe_ok` check → double-credit risk if reconciler retried | Session 14 |
| B-3 | Wallet credit-back in `_act_on_reconciliation_required()` unconditional → double-credit if Stripe failed then Job 3 retried | Session 14 |
| B-4 | `execute_intent()` wrote no booking record to Supabase → intent-booked slots not cancellable | Session 14 |
| B-5 | `book_with_saved_card` booking record missing 7 fields: customer_name, customer_phone, business_name, location_city, start_time, currency, payment_method | Session 14 |
| B-6 | `_find_booking_by_confirmation()` O(n) linear scan → replaced with O(1) `by_confirmation/` index written at every booking creation point | Session 14 |
| B-7 | No startup signal when Supabase Storage misconfigured → `_check_supabase_on_startup()` added; `/health` now exposes storage status + discovery telemetry | Session 14 |
| B-8 | `_fire_callback()` fire-and-forget → callback retry queue in `callback_queue/` with 4 retries, 6h TTL, 2→10→30→120 min backoff | Session 14 |
| D-1 | `self_serve_cancel` HTML page always said "full refund issued" even on Stripe failure | 35d350e |
| D-2 | `_cancel_octo_booking` sent `Octo-Capabilities: octo/pricing` on DELETE (Bokun hangs) | 35d350e |
| D-3 | `retry_cancellations._cancel_octo` same header on DELETE | 35d350e |
| D-4 | `reconcile_bookings._verify_octo_booking` same header on GET `/bookings/{uuid}` | 35d350e |
| D-5 | `send_booking_email._build_failed_html` used OCTO JSON blob as `retry_url` href | 35d350e |
| D-6 | `_get_reliability_metrics` + `_find_booking_by_confirmation` missing prefix filters | 35d350e |
| D-7 | `reconcile_bookings._list_bookings` missing 7 of 8 internal prefix filters | 35d350e |
| D-8 | `_fulfill_booking` used `"burl_j" in dir()` fragile unbound-variable pattern | 35d350e |
| V-1 | EgyExcursions + Vakare Travel Service missing from `vendor_ids` → ~3,000 slots never fetched | 5dcb876 |
| V-2 | `reference_supplier_map` missing 7 prefix entries + `product_id_map` for null-ref products | 5dcb876 |
| V-3 | Supplier resolution reactive/fragile — `_resolve_product_identity()` 3-level chain added | (current) |
| M-1 | `get_supplier_info` two diverging hardcoded implementations, both missing Vakare (61% of inventory) | (current) |
| M-2 | POST /mcp `search_slots` limit=100 default hid 98% of inventory from agents | FIXED (Session 20 — B-30: removed all limits from both Flask /mcp and FastMCP; performance via pre-warm) |
| M-3 | `_MCP_TOOLS` description listed disabled platforms (Ventrata/Zaui/Peek Pro) as active | (current) |
| M-4 | `_safe()` in FastMCP SSE included `price` which is always null after `_sanitize_slot()` | (current) |
| M-5 | `_safe()` in FastMCP SSE missing `location_state` field | (current) |
| M-6 | Capabilities metadata hardcoded "11 suppliers" (now 14) | (current) |
| B-1 | Bug 27 re-introduced: `start_time` not in `booking_url` blob → 409 re-resolution always matched any time | (current) |
| B-2 | All three booking record paths missing customer_name, phone, business_name, city, start_time → get_booking_status returned nulls | (current) |
| B-3 | `_fulfill_booking_async` failure path fully overwrote pending record → lost service_name, customer_email, checkout_url | (current) |
| B-4 | Payment capture failure left OCTO booking confirmed at supplier with no payment or cancellation queued | (current) |
| B-5 | `complete_booking.py` not found returned fake confirmation → booking marked "booked" with no real reservation | (current) |
| B-6 | FastMCP `book_slot` and `book_direct` missing quantity → multi-person bookings silently became 1-person | (current) |
| B-7 | `GET /bookings/<id>` used non-existent `confirmation_number` key; missing location_city, quantity, failure_reason | (current) |
| PE-1 | `book_direct` sent zero customer emails — no `booking_confirmed` on success, no `booking_failed` on failure | (current) |
| PE-2 | `booking_initiated` and `booking_confirmed` emails showed per-person `our_price` instead of total charged (`our_price × quantity`) | (current) |
| PE-3 | `cancel_url` was `/cancel/...` (relative path) when `BOOKING_SERVER_HOST` unset — email template rendered broken href instead of fallback text | (current) |
| C-1 | `DELETE /bookings/{id}` (agent/API cancel) sent zero customer emails — customer got their money back with no notification whatsoever | (current) |
| C-2 | `NameError` crash in `self_serve_cancel` when booking already cancelled — `refund_issued` undefined → HTTP 500 on second cancel-link click | (current) |
| C-3 | `self_serve_cancel` OCTO detection missing `or platform == "octo"` branch — inconsistent with DELETE /bookings path | (current) |
| C-4 | `self_serve_cancel` always wrote `status="cancelled"` even when Stripe refund failed — `cancellation_refund_failed` state never set by self-serve path, bypassing any future retry/monitoring | (current) |
| C-8 | `booking_cancelled` email always said "the operator has cancelled your booking" regardless of initiator — customer self-serve cancel incorrectly attributed to operator | (current) |

### OPEN — Architectural gaps (not yet addressed)

| # | Gap | Location | Impact |
|---|---|---|---|
| A-1 | No wallet credit-back on any cancellation path | `run_api_server.py` | Wallet bookings never refunded on cancel |
| A-2 | `execute/guaranteed` booking state written to `.tmp/` only | `execution_engine.py` | GET /bookings/{id} returns 404; state lost on redeploy |
| A-3 | `execute/guaranteed` wallet debit after booking (double-spend risk) | `execution_engine.py` | Two concurrent calls can both succeed with one debit failure |
| A-4 | `execute/guaranteed` + wallet: OCTO cancel not queued on payment failure | `execution_engine.py` | Supplier has confirmed booking with no payment |
| A-5 | Intent sessions in `.tmp/intent_sessions.json` only | `intent_sessions.py` | All active intents lost on every Railway redeploy |
| A-6 | `reconcile_bookings.py` flags `reconciliation_required` but no auto-refund or customer notification | `reconcile_bookings.py` | Silent accumulation, no customer action |
| A-7 | ~~2 Bokun products unmapped (null/empty ref strings)~~ | `octo_suppliers.json` | FIXED — `product_id_map` fallback added to `fetch_octo_slots.py`; 0 unresolved slots |
| A-8 | Google Sheets OAuth expired | `compute_pricing.py` | Urgency pricing disabled, no pricing learning |
| A-9 | ~~Slot discovery runs on local laptop only~~ | `run_pipeline.bat` | FIXED — Railway APScheduler runs pipeline every 4h. Local Task Scheduler + batch files archived 2026-04-18 |
| A-10 | No real end-to-end booking test completed | All paths | Unknown if OCTOBooker actually works in production |
| A-11 | No partial refund/cancellation for multi-qty bookings | All cancel paths | All-or-nothing cancel only |
| A-12 | Wallet storage uses single shared JSON file — concurrent write race | `manage_wallets.py` | Balance overwrites possible under high concurrency |
| A-13 | Market insights data in `.tmp/insights/` only | `market_insights.py` | `/insights/market` empty after redeploy |
| A-14 | SMS subscribers in `.tmp/sms_subscribers.json` only | `send_sms_alert.py` | Subscriptions lost on redeploy; SMS alerts never fire |
| A-15 | `cancellation_refund_failed` records have no automatic retry or customer notification — `reconcile_bookings.py` only processes `status == "booked"` records; failed Stripe refunds on cancellation sit permanently with no escalation | `run_api_server.py`, `reconcile_bookings.py` | Customer could be left unrefunded indefinitely with no notification |

---

## 18. Environment Variables Required

### Local (.env) — all set ✅

`BOKUN_API_KEY`, `BOKUN_ACCESS_KEY`, `BOKUN_SECRET_KEY`, `SUPABASE_URL`,
`SUPABASE_SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`SENDGRID_API_KEY`, `RAILWAY_TOKEN`, `GOOGLE_SHEET_ID`,
`LANDING_PAGE_URL`, `BOOKING_SERVER_HOST`, `LMD_WEBSITE_API_KEY`

### Railway (web service) — all set ✅

`BOKUN_API_KEY`, `BOKUN_ACCESS_KEY`, `BOKUN_SECRET_KEY`, `BOKUN_WEBHOOK_TOKEN`,
`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`SENDGRID_API_KEY`, `LANDING_PAGE_URL`, `BOOKING_SERVER_HOST`,
`LMD_WEBSITE_API_KEY`, `LMD_SIGNING_SECRET`, `PEEK_WEBHOOK_SECRET`, `PORT` (auto)
