# Last Minute Deals HQ — Complete System Map

**Last updated:** 2026-04-16 (v2 — full autonomous + quantity + cancellation audit)
**Status key:** ✅ Verified working | ⚠️ Partially working / untested | ❌ Broken (code bug confirmed) | 🔲 Not yet built

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Process 1: Slot Discovery Pipeline](#2-process-1-slot-discovery-pipeline)
3. [Process 2: Human Booking (Stripe Checkout)](#3-process-2-human-booking-stripe-checkout)
4. [Process 3: Autonomous Booking — Direct Wallet](#4-process-3-autonomous-booking--direct-wallet)
5. [Process 4: Autonomous Booking — Execute/Guaranteed](#5-process-4-autonomous-booking--executeguaranteed)
6. [Process 5: Semi-Autonomous — /api/execute Intent](#6-process-5-semi-autonomous--apiexecute-intent)
7. [Process 6: Intent Sessions](#7-process-6-intent-sessions)
8. [Process 7: Cancellation Matrix](#8-process-7-cancellation-matrix)
9. [Process 8: Supplier-Initiated Cancellation (Bokun Webhook)](#9-process-8-supplier-initiated-cancellation-bokun-webhook)
10. [Process 9: MCP Agent Integration](#10-process-9-mcp-agent-integration)
11. [Process 10: Wallet System](#11-process-10-wallet-system)
12. [Multi-Quantity Booking — All Paths](#12-multi-quantity-booking--all-paths)
13. [Infrastructure & Dependencies](#13-infrastructure--dependencies)
14. [Bug Register — Confirmed Code Defects](#14-bug-register--confirmed-code-defects)
15. [Environment Variables Required](#15-environment-variables-required)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  DATA PIPELINE (local laptop, runs every 4h via Task Scheduler)     │
│  fetch_octo_slots.py → aggregate_slots.py → compute_pricing.py      │
│  → sync_to_supabase.py → update_landing_page.py                     │
└────────────────────────────┬────────────────────────────────────────┘
                             │ upserts to Supabase
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SUPABASE                                                           │
│  • "slots" table         — available inventory (REST API)           │
│  • "bookings" bucket     — booking records (Storage JSON)           │
│  • "request_logs"        — API call logs (Postgres, blocked)        │
│  • "cancellation_queue/" — failed OCTO cancellations awaiting retry │
└──────────┬──────────────────────────────────────────────────────────┘
           │ REST API
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  RAILWAY — run_api_server.py (Flask + APScheduler)                  │
│  https://api.lastminutedealshq.com                                  │
│                                                                     │
│  Booking entry points:                                              │
│  POST /api/book           — human Stripe checkout (quantity OK)     │
│  POST /api/book/direct    — autonomous wallet (❌ quantity broken)  │
│  POST /execute/guaranteed — autonomous multi-path engine            │
│  POST /execute/best       — goal-optimized autonomous booking       │
│  POST /api/execute        — semi-auto: agent picks, human pays      │
│  POST /intent/create      — persistent goal session                 │
│                                                                     │
│  Cancellation entry points:                                         │
│  DELETE /bookings/{id}    — API cancel (Stripe + OCTO + retry queue)│
│  GET/POST /cancel/{id}    — customer self-serve page                │
│  POST /api/bokun/webhook  — supplier-initiated (Bokun notification) │
│                                                                     │
│  Observability:                                                     │
│  GET /health, /metrics, /bookings/{id}, /verify/{id}               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. Process 1: Slot Discovery Pipeline

**Trigger:** `run_pipeline.bat` — local laptop, every ~4h via Task Scheduler
**Status:** ✅ Bokun path working | ⚠️ All other platforms disabled

### Step 1: fetch_octo_slots.py

```
START
  │
  ├─ Load tools/seeds/octo_suppliers.json
  │    Only processes: enabled=true AND API key set in .env
  │    Currently enabled: bokun_reseller ONLY
  │
  ├─ For each vendor_id (11 total): [85, 22298, 134418, 103510, 137492, 16261, 105917, 3020, 33562, 70, 102991]
  │    ├─ GET /products  (NO pricing capability header — avoids Bokun hang)
  │    ├─ For each product:
  │    │    ├─ POST /availability (WITH octo/pricing header, date range: today → +8 days)
  │    │    ├─ Filter: status in {AVAILABLE, FREESALE, LIMITED}
  │    │    ├─ Filter: starts within hours_ahead (default 168h)
  │    │    ├─ Resolve supplier from product.reference prefix via reference_supplier_map
  │    │    │    ├─ PREFIX MATCH → supplier name, city, country
  │    │    │    └─ NO MATCH → "Bokun Reseller", no city ⚠️ (314 products affected)
  │    │    └─ normalize_slot: slot_id = sha256(platform+product_id+start_time)
  │    │         booking_url = JSON blob: {_type:"octo", base_url, api_key_env,
  │    │                                   product_id, option_id, availability_id,
  │    │                                   unit_id, supplier_id, vendor_id}
  │    └─ retry_on_timeout=true: one retry on timeout
  │
  └─ Write .tmp/octo_slots.json

Step 2: aggregate_slots.py
  Read all .tmp/*_slots.json → deduplicate on slot_id → filter → sort by hours_until_start → .tmp/aggregated_slots.json

Step 3: compute_pricing.py
  Per slot: base 8-12% markup × urgency multiplier (×1.0 at 48-72h → ×2.5 at 0-12h)
  Google Sheets Pricing Log → fetch historical fill rate (CURRENTLY BROKEN: OAuth expired)
  Falls back to defaults when Sheets unavailable → writes our_price, our_markup to slot

Step 4: sync_to_supabase.py
  Upsert all slots to Supabase "slots" table (keyed on slot_id). Purge past slots.

Step 5: update_landing_page.py
  Renders Jinja2 HTML from Supabase slots. Groups by category/city.
  Shows: service_name, city, time, our_price — hides platform, booking_url, original_price.
  Deploys to Cloudflare Pages.
```

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
**Status:** ❌ BROKEN — unpacking bug (see Bug #1)
**Quantity support:** ❌ NOT SUPPORTED (quantity always 1 — see Bug #2)

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
  ├─ debit_wallet(wallet_id, amount_cents)
  │    └─ DEBIT FAILED → delete recovery record → 500
  ├─ Update crash-recovery record (wallet_debited=true)
  │
  ├─ _fulfill_booking(slot_id, customer, platform, booking_url)  ← ❌ BUG: no quantity arg
  │    Returns 3-tuple: (confirmation, booking_meta, supplier_reference)
  │                     ← ❌ BUG: code only unpacks 2 values → ValueError crash
  │
  ├─ FAILURE:
  │    ├─ credit_wallet(wallet_id, amount_cents, "Refund: failed booking")
  │    │    └─ CREDIT FAILS → log "manual refund needed" (no automatic recovery)
  │    ├─ Mark recovery record resolved: "refunded"
  │    └─ Return { status: "failed", wallet_refunded: true }
  │
  └─ SUCCESS:
       ├─ _mark_booked(slot_id)
       ├─ Save booking record:
       │    { confirmation, payment_method: "wallet", wallet_id,
       │      status: "booked", ... }
       │    ← ❌ BUG: supplier_reference NOT stored (Bokun webhook won't match)
       ├─ Mark recovery record resolved: "completed"
       └─ Return { status: "confirmed", confirmation_number, wallet_balance_remaining }


CRASH RECOVERY (runs at server startup):
  _reconcile_pending_debits() scans "pending_exec_*" records in Supabase Storage
  ├─ wallet_debited=false → pre-debit crash → mark resolved (no refund needed)
  └─ wallet_debited=true, resolved=false → post-debit crash → credit_wallet() refund
```

---

## 5. Process 4: Autonomous Booking — Execute/Guaranteed

**Entry point:** POST /execute/guaranteed
**Requires:** API key via header (NO — actually does NOT require API key) | wallet_id OR payment_intent_id
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
  │
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
       │    │    ← ❌ BUG: quantity not passed — always books 1 person
       │    │
       │    ├─ BOOKING SUCCESS → handle payment:
       │    │    ├─ payment_method == "wallet":
       │    │    │    _charge_wallet() AFTER booking ← ❌ BUG: debit happens after booking
       │    │    │    (unlike book_direct which debits first)
       │    │    │    → wallet charge fails → _cancel_octo() → ❌ no retry queue
       │    │    │
       │    │    ├─ payment_method == "stripe_pi":
       │    │    │    _capture_stripe(payment_intent_id)
       │    │    │    → capture fails → _cancel_octo() + _cancel_stripe()
       │    │    │
       │    │    └─ payment_method == "stripe_checkout":
       │    │         No payment action here — Stripe webhook handles capture
       │    │
       │    ├─ PAYMENT OK → mark slot booked (in-memory + .tmp/booked_slots.json)
       │    │    ← ❌ BUG: NOT saved to Supabase Storage
       │    │    ← ❌ BUG: .tmp/ files don't persist across Railway redeploys
       │    │    ← ❌ BUG: GET /bookings/{id} returns 404 for these bookings
       │    │    ← ❌ BUG: supplier_reference NOT stored (Bokun webhook won't match)
       │    │    ← ❌ BUG: no crash recovery mechanism
       │    │
       │    ├─ BOOKING FAILURE → log, try next strategy
       │    └─ PAYMENT FAILURE after booking → OCTO cancel (best-effort, no retry queue)
       │
       ├─ All 7 attempts exhausted:
       │    └─ payment_method == "stripe_pi" → cancel the hold
       │         payment_method == "wallet" → NO REFUND (wallet was never charged)
       │
       └─ Return ExecutionResult { success, confirmation, attempt_log, fallbacks_used }

Email notifications sent on success/failure (non-fatal if SendGrid fails)
← ❌ BUG: no cancel link in confirmation email (no booking_id in Supabase to build it from)
```

---

## 6. Process 5: Semi-Autonomous — /api/execute Intent

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

## 7. Process 6: Intent Sessions

**Entry point:** POST /intent/create
**Requires:** X-API-Key header
**Status:** ⚠️ Implemented | ❌ Critical persistence gap
**Quantity support:** ❌ NOT SUPPORTED

```
POST /intent/create
  { goal, constraints, customer, payment: {method, wallet_id}, autonomy, ttl_hours }
  │
  ├─ goal: "find_and_book" | "monitor_only" | "price_alert"
  ├─ autonomy: "full" (auto-execute) | "notify" (alert only) | "monitor" (never execute)
  │
  └─ Create intent session → saved to .tmp/intent_sessions.json
       ← ❌ BUG: NOT saved to Supabase — lost on every Railway redeploy


GET /intent/{id}      — status poll (API key required, ownership verified)
POST /intent/{id}/execute — manually trigger "notify" intent (temporarily upgrades to "full")
POST /intent/{id}/cancel  — marks cancelled (no booking cleanup if mid-execution)


Intent auto-execution (autonomy=full):
  When slots matching constraints appear → execute via execution_engine
  ← ❌ BUG: same issues as execute/guaranteed (no Supabase record, no quantity)
  ← ❌ BUG: if intent fires while Railway is redeploying, intent is gone → never executes
```

---

## 8. Process 7: Cancellation Matrix

This is the most critical section. All paths documented separately.

### 7A: Customer Cancels — Human (Stripe) Booking

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
  ├─ _refund_stripe(payment_intent_id):
  │    ├─ PI status "requires_capture" → cancel hold (customer never charged)
  │    ├─ PI status "succeeded" → full refund created
  │    ├─ PI already cancelled/refunded → treated as success
  │    └─ Retry 3× with backoff → fails after 3 → log, continue anyway
  │
  ├─ OCTO cancellation (if OCTO platform):
  │    _cancel_octo_booking(supplier_id, confirmation)
  │    ├─ SUCCESS → booking released on supplier
  │    └─ FAILURE → ❌ BUG: NOT queued for retry
  │                  (unlike DELETE /bookings/{id} which queues)
  │                  Supplier retains active reservation even though customer is refunded
  │
  ├─ Update record: status="cancelled", cancelled_by="customer_self_serve"
  ├─ Send cancellation email
  └─ Show "Booking cancelled" page with refund notice
```

### 7B: Customer Cancels — Autonomous (Wallet) Booking

**Entry: DELETE /bookings/{booking_id} (API key required)**
**No self-serve cancel link for wallet bookings (no cancel URL in confirmation email)**

```
DELETE /bookings/{booking_id}
  ├─ X-API-Key required → 401
  ├─ Load booking record → 404
  ├─ Already cancelled → 200 (idempotent)
  │
  ├─ _refund_stripe(payment_intent_id):
  │    payment_intent_id = "" for wallet bookings
  │    ← ❌ BUG: Stripe retrieve("") → silently no-ops or errors
  │    No wallet credit-back issued
  │
  ├─ OCTO cancellation (if OCTO platform):
  │    _cancel_octo_booking(supplier_id, confirmation)
  │    ├─ SUCCESS → booking released
  │    └─ FAILURE transient → _queue_octo_retry() → background retry every 15 min ✅
  │    └─ FAILURE permanent → log, no retry
  │
  ├─ Update record: status="cancelled"
  └─ Return { success, refund_id (empty), octo_queued_for_retry }

← ❌ BUG: Wallet bookings get no refund on cancellation
         Stripe path is called with empty payment_intent_id
         No credit_wallet() call anywhere in the cancel path
```

### 7C: Supplier Cancels — Human (Stripe) Booking

**Entry: POST /api/bokun/webhook?token={token}**

```
Bokun POSTs when supplier cancels in their dashboard
  │
  ├─ Token auth → 401 if wrong
  ├─ Parse: booking_data["status"], booking_data["confirmationCode"]
  ├─ Not a cancellation event → 200 "event_ignored"
  │
  ├─ _find_booking_by_confirmation(confirmationCode):
  │    Scans Supabase Storage
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

### 7D: Supplier Cancels — Autonomous (Wallet) Booking

**Entry: POST /api/bokun/webhook?token={token}**

```
Same flow as 7C, BUT:
  ├─ _find_booking_by_confirmation(confirmationCode):
  │    Wallet booking records DO NOT store supplier_reference ← ❌ BUG
  │    OCTO uuid (confirmation) may match, OR may not if Bokun sends their own ref
  │
  └─ _refund_stripe(payment_intent_id):
       payment_intent_id = "" for wallet bookings
       ← ❌ BUG: no wallet credit-back
       Wallet is never refunded on supplier cancellation of wallet bookings
```

### 7E: API Cancel — Any Booking

**Entry: DELETE /bookings/{booking_id} (X-API-Key required)**

```
This is the "admin" cancel path. Same as 7B but usable for Stripe bookings too.
  │
  ├─ _refund_stripe(payment_intent_id):
  │    Works correctly for Stripe bookings (PI exists)
  │    ← ❌ BUG: fails silently for wallet bookings (no PI)
  │
  ├─ OCTO cancellation with retry queue ✅
  └─ Update record: status="cancelled"
```

### 7F: Cancellation Retry Queue (OCTO failures)

```
_queue_octo_retry() writes to Supabase Storage: cancellation_queue/{booking_id}.json
  │
  └─ retry_cancellations.py runs every 15 min (APScheduler on Railway)
       ├─ Load all pending_octo records from cancellation_queue/
       ├─ _cancel_octo_booking() for each
       ├─ SUCCESS → delete from queue
       └─ FAILURE → increment attempts, keep in queue (max attempts unknown)
```

**Which cancellation paths populate the retry queue:**
- ✅ DELETE /bookings/{id} — queues on OCTO failure
- ❌ GET/POST /cancel/{booking_id} — does NOT queue on OCTO failure (Bug #5)
- ❌ Bokun webhook — does NOT queue on OCTO failure at all (only does Stripe refund)

---

## 9. Process 8: Supplier-Initiated Cancellation (Bokun Webhook)

**Entry point:** POST /api/bokun/webhook?token={BOKUN_WEBHOOK_TOKEN}
**Status:** ✅ Auth working (smoke tested 2026-04-16) | ❌ Wallet booking gap (Bug #4, #6)

```
Bokun: POST /api/bokun/webhook?token=...
  │
  ├─ Token: hmac.compare_digest(BOKUN_WEBHOOK_TOKEN, request.args["token"])
  │    ├─ WRONG → 401
  │    └─ BOKUN_WEBHOOK_TOKEN not set → WARNING log, allow through
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
  │
  ├─ Update record: status="cancelled", cancelled_by="supplier_bokun_webhook"
  └─ Send cancellation email to customer
```

---

## 10. Process 9: MCP Agent Integration

**Entry points:**
- `POST /mcp` — MCP-over-HTTP (Smithery, direct API agents) — on Flask server
- `GET /sse` + `POST /messages` — SSE proxied to embedded FastMCP thread
- `mcp.lastminutedealshq.com` — standalone SSE server (run_mcp_remote.py, separate Railway service)

**Status:** ✅ search_slots, get_supplier_info working | ✅ book_slot returns checkout_url | ⚠️ Human Stripe payment still required for bookings

```
MCP tool: search_slots
  ├─ Cache hit (60s TTL, keyed on hours_ahead+category+city+max_price+limit) → return cached
  ├─ _load_slots_from_supabase(limit=100 default)
  ├─ _sanitize_slot(): strips internal fields, recomputes hours_until_start dynamically
  └─ Store in cache, return list

MCP tool: book_slot(slot_id, customer_name, customer_email, customer_phone, quantity)
  ├─ POST /api/book internally → creates Stripe checkout
  └─ Returns { checkout_url, booking_id }
     Customer must still open checkout_url and pay manually
     ← ❌ NO AUTONOMOUS BOOKING PATH via MCP
     ← MCP agents cannot use wallets to book without human approval

MCP tool: get_booking_status(booking_id)
  └─ GET /bookings/{booking_id} → returns record

MCP tool: get_supplier_info()
  └─ Returns static supplier directory
```

**Smithery connection path:** Smithery → `server.json` → `POST /mcp` on Flask → `_mcp_call_tool()`
**Claude Desktop path:** `GET /sse` → proxied SSE → embedded FastMCP

---

## 11. Process 10: Wallet System

**Status:** ⚠️ Implemented | ❌ No refund on wallet booking cancellation

```
CREATE:   POST /api/wallets/create → { wallet_id, api_key, balance: 0 }
FUND:     POST /api/wallets/fund   → Stripe checkout for top-up
          → checkout.session.completed → wallet_topup fast path → credit_wallet()
BALANCE:  GET /api/wallets/{id}/balance (requires wallet api_key)
HISTORY:  GET /api/wallets/{id}/transactions
LIMIT:    PUT /api/wallets/{id}/spending-limit

Wallet booking payment flow (book_direct):
  ├─ PRE-CHECK: balance ≥ our_price → else 402
  ├─ PRE-CHECK: our_price ≤ spending_limit → else 403
  ├─ DEBIT before booking attempt (crash-safe)
  └─ CREDIT back if booking fails

Wallet booking cancellation:
  ← ❌ BUG: NO credit-back on ANY cancellation path
     (customer cancel, admin cancel, supplier cancel)
     Only manual wallet credit via internal tools
```

---

## 12. Multi-Quantity Booking — All Paths

| Booking Path | Quantity Supported? | Notes |
|---|---|---|
| POST /api/book (human Stripe) | ✅ Yes (1-20) | quantity × per_person_price charged via Stripe line_item |
| POST /api/book/direct (wallet) | ❌ No (always 1) | Bug #2 — quantity not parsed or passed |
| POST /execute/guaranteed | ❌ No (always 1) | Bug #3 — not in ExecutionRequest, not passed to complete_booking |
| POST /execute/best | ❌ No (always 1) | Same gap as execute/guaranteed |
| POST /api/execute (semi-auto) | ❌ No (always 1) | quantity not passed to /api/book call |
| MCP book_slot | ✅ Passes quantity to /api/book | But customer still manually pays |
| Intent sessions | ❌ No | Not in constraints object |

**Multi-quantity cancellation:**
- All cancellation paths issue FULL refund only — no partial refund support
- Customer cancels 2 of 3 seats: impossible — must cancel all or none
- No partial OCTO cancellation implemented

---

## 13. Infrastructure & Dependencies

| Component | Service | Status | Notes |
|---|---|---|---|
| Slot storage | Supabase "slots" table | ✅ | REST API only — direct Postgres TCP blocked from Railway |
| Booking records | Supabase Storage "bookings" | ✅ | JSON files per booking, persists across Railway redeploys |
| Cancellation queue | Supabase Storage "cancellation_queue/" | ✅ | Used by DELETE /bookings only |
| Request logs | Supabase Postgres "request_logs" | ❌ | TCP blocked — /health success rates always null |
| Intent sessions | .tmp/intent_sessions.json | ❌ | LOCAL only — lost on every Railway redeploy |
| API server | Railway (web service) | ✅ | Auto-redeploys on git push |
| MCP SSE server | Railway (mcp service) | ✅ | run_mcp_remote.py |
| Payments | Stripe | ✅ | Checkout + webhooks + auth-capture |
| Supplier booking | Bokun OCTO API | ✅ (API reachable) / ❌ (real booking untested) | 11 vendor IDs |
| Bokun notifications | HTTP notification (URL token auth) | ✅ | Smoke tested 2026-04-16 |
| Email | SendGrid | ✅ | 4 email types wired |
| Landing page | Cloudflare Pages | ✅ | Rebuilt every pipeline run |
| Slot discovery | Local Windows laptop | ⚠️ | No cloud scheduling — fails if laptop sleeps |
| Pricing history | Google Sheets | ❌ | OAuth token expired |
| Booked slot dedup | .tmp/booked_slots.json (execute/guaranteed) | ❌ | Lost on Railway redeploy — double-booking risk |
| SMS alerts | Twilio | 🔲 | Implemented, not activated |
| Social posting | Twitter/Reddit/Telegram | 🔲 | Scripts exist, not running |

---

## 14. Bug Register — Confirmed Code Defects

### CRITICAL — Will cause live booking failures

| # | Bug | Location | Symptom | Fix Required |
|---|---|---|---|---|
| 1 | `book_direct` unpacks 2 values from `_fulfill_booking` which returns 3-tuple | `run_api_server.py:1984` | ValueError crash on every wallet booking attempt | Unpack 3 values: `confirmation, booking_meta, supplier_reference = fut.result(...)` |
| 2 | `book_direct` ignores `quantity` — always books 1 person | `run_api_server.py:1983` | Multi-person autonomous bookings silently book 1 | Parse quantity from request, pass to `_fulfill_booking` |
| 3 | `execute/guaranteed` engine ignores `quantity` | `execution_engine.py:302` | Always books 1 person regardless of request | Add quantity to ExecutionRequest, pass to `_attempt_booking` |
| 4 | `book_direct` does NOT store `supplier_reference` | `run_api_server.py:2017` | Bokun webhook cannot find wallet bookings by supplier ref | Store `supplier_reference` in the booking record |
| 5 | `self_serve_cancel` does NOT queue failed OCTO cancellations for retry | `run_api_server.py:4197` | Customer refunded but supplier retains active booking | Add `_queue_octo_retry()` call on failure |
| 6 | No wallet credit-back on ANY cancellation path | `run_api_server.py:3944, 4191, 4099` | Wallet bookings never refunded when cancelled | Add `credit_wallet()` call when `payment_method == "wallet"` in all 3 paths |
| 7 | `execute/guaranteed` writes booking state to `.tmp/` only — not Supabase Storage | `execution_engine.py:512` | GET /bookings/{id} returns 404; state lost on redeploy | Save booking record to Supabase Storage |
| 8 | `execute/guaranteed` wallet debit happens AFTER booking (double-spend risk) | `execution_engine.py:489` | Two concurrent calls can both succeed, one wallet charge may fail | Debit before attempt (match book_direct pattern) |
| 9 | `execute/guaranteed` + wallet: if wallet charge fails post-booking, OCTO cancel not queued | `execution_engine.py:499` | Supplier has confirmed booking with no payment, no retry | Queue OCTO cancel with retry on payment failure |
| 10 | Intent sessions stored in `.tmp/intent_sessions.json` only | `intent_sessions.py` | All active intents lost on every Railway redeploy | Persist intent sessions to Supabase Storage |

### HIGH — Silent failures or data gaps

| # | Bug | Location | Symptom | Fix Required |
|---|---|---|---|---|
| 11 | 314 Bokun products unmapped to supplier/city | `octo_suppliers.json` | Slots show as "Bokun Reseller" with no city | Run scraper, update reference_supplier_map |
| 12 | Google Sheets OAuth expired | `compute_pricing.py` | Urgency pricing disabled, no pricing learning | Re-authenticate OAuth token |
| 13 | Slot discovery runs on local laptop only | `run_pipeline.bat` | Pipeline fails when laptop sleeps | Move to GitHub Actions scheduled workflow |
| 14 | No real end-to-end booking test completed | All paths | Unknown if OCTOBooker actually works | Run one real booking (cheapest slot) |
| 15 | No partial refund/cancellation support for multi-qty bookings | All cancel paths | All-or-nothing only | Design and implement partial cancel |

---

## 15. Environment Variables Required

### Local (.env) — all set ✅

`BOKUN_API_KEY`, `BOKUN_ACCESS_KEY`, `BOKUN_SECRET_KEY`, `SUPABASE_URL`,
`SUPABASE_SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`SENDGRID_API_KEY`, `RAILWAY_TOKEN`, `GOOGLE_SHEET_ID`,
`LANDING_PAGE_URL`, `BOOKING_SERVER_HOST`, `LMD_WEBSITE_API_KEY`

### Railway (web service) — all set ✅

`BOKUN_API_KEY`, `BOKUN_ACCESS_KEY`, `BOKUN_SECRET_KEY`, `BOKUN_WEBHOOK_TOKEN`,
`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`SENDGRID_API_KEY`, `LANDING_PAGE_URL`, `BOOKING_SERVER_HOST`,
`LMD_WEBSITE_API_KEY`, `PORT` (auto)
