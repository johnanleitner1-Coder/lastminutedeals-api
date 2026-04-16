# Last Minute Deals HQ — Complete System Map

**Last updated:** 2026-04-16
**Status key:** ✅ Verified working | ⚠️ Partially working | ❌ Broken/untested | 🔲 Not yet built

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Process 1: Slot Discovery Pipeline](#2-process-1-slot-discovery-pipeline)
3. [Process 2: Customer Booking Flow (Human-in-Loop)](#3-process-2-customer-booking-flow-human-in-loop)
4. [Process 3: Agent Autonomous Booking (Wallet-Based)](#4-process-3-agent-autonomous-booking-wallet-based)
5. [Process 4: Supplier-Initiated Cancellation (Bokun Webhook)](#5-process-4-supplier-initiated-cancellation-bokun-webhook)
6. [Process 5: Customer Self-Serve Cancellation](#6-process-5-customer-self-serve-cancellation)
7. [Process 6: MCP Agent Integration](#7-process-6-mcp-agent-integration)
8. [Process 7: Goal-Oriented Autonomous Booking (execute/best)](#8-process-7-goal-oriented-autonomous-booking)
9. [Process 8: Intent Sessions](#9-process-8-intent-sessions)
10. [Process 9: Wallet System](#10-process-9-wallet-system)
11. [Process 10: B2B API Key Access](#11-process-10-b2b-api-key-access)
12. [Infrastructure & Dependencies](#12-infrastructure--dependencies)
13. [Known Issues & Gaps](#13-known-issues--gaps)
14. [Environment Variables Required](#14-environment-variables-required)

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
│  • "slots" table      — available inventory                         │
│  • "bookings" bucket  — booking records (Supabase Storage JSON)     │
│  • "request_logs"     — API call logs (Postgres, Railway blocked)   │
└──────────┬─────────────────────────────────────────────────────────┘
           │ REST API (read/write)
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  RAILWAY — run_api_server.py (Flask)                                │
│  https://web-production-dc74b.up.railway.app                        │
│  https://api.lastminutedealshq.com (custom domain)                  │
│                                                                     │
│  Exposes:                                                           │
│  • /slots               — slot search                               │
│  • /api/book            — create Stripe checkout                    │
│  • /api/book/direct     — autonomous wallet booking                 │
│  • /api/webhook         — Stripe event handler                      │
│  • /api/bokun/webhook   — Bokun cancellation handler                │
│  • /cancel/{id}         — customer self-serve cancel page           │
│  • /bookings/{id}       — booking status                            │
│  • /mcp                 — MCP-over-HTTP for AI agents               │
│  • /execute/best        — goal-oriented agent booking               │
│  • /execute/guaranteed  — explicit agent booking with retry         │
│  • /intent/*            — intent session management                 │
│  • /api/wallets/*       — agent wallet operations                   │
│  • /health, /metrics    — observability                             │
└──────────────────────────────────────────────────────────────────────┘
           │
    ┌──────┴────────┐
    ▼               ▼
STRIPE           BOKUN OCTO API
(payments)       (booking execution)
```

---

## 2. Process 1: Slot Discovery Pipeline

**Trigger:** Manual or Task Scheduler — `run_pipeline.bat` every ~4h on local Windows laptop
**Status:** ✅ Working end-to-end (Bokun path); ⚠️ Other platforms disabled

### Step 1: fetch_octo_slots.py

```
START
  │
  ├─ Load tools/seeds/octo_suppliers.json
  │    Only processes entries with enabled=true and API key set in .env
  │    Currently enabled: bokun_reseller only
  │
  ├─ For each enabled supplier:
  │    │
  │    ├─ [Bokun] For each vendor_id in vendor_ids list (11 vendors):
  │    │    ├─ GET /products (no pricing capability header — avoids Bokun timeout)
  │    │    ├─ For each product:
  │    │    │    ├─ POST /availability (with Octo-Capabilities: octo/pricing header)
  │    │    │    │    date range: today → today+8 days
  │    │    │    ├─ Filter: status in {AVAILABLE, FREESALE, LIMITED}
  │    │    │    ├─ Filter: starts within hours_ahead window (default 168h)
  │    │    │    ├─ Map product.reference prefix → supplier name/city via reference_supplier_map
  │    │    │    │    ├─ MATCH FOUND → use mapped name/city/country
  │    │    │    │    └─ NO MATCH → falls back to "Bokun Reseller" + no city ⚠️ (314 products affected)
  │    │    │    └─ normalize_slot.py: compute slot_id = sha256(platform+product_id+start_time)
  │    │    └─ [retry_on_timeout=true]: retry once on timeout
  │    │
  │    └─ [Other OCTO platforms — Ventrata, Zaui, Peek, Rezdy]: disabled=true, skipped
  │
  └─ Write .tmp/octo_slots.json
```

### Step 2: aggregate_slots.py

```
START
  │
  ├─ Read all .tmp/*_slots.json files
  ├─ Deduplicate on slot_id (keep latest scraped_at)
  ├─ Filter: hours_until_start ≤ configured window
  ├─ Sort: by hours_until_start ascending (most urgent first)
  └─ Write .tmp/aggregated_slots.json
```

### Step 3: compute_pricing.py

```
START
  │
  ├─ Read .tmp/aggregated_slots.json
  ├─ For each slot:
  │    ├─ Try to fetch historical fill rate from Google Sheets "Pricing Log"
  │    │    ├─ SHEETS OAUTH VALID → use historical data
  │    │    └─ SHEETS OAUTH EXPIRED → use defaults only ⚠️ (current state)
  │    │
  │    ├─ Compute our_price:
  │    │    base_markup = 8-12% of original price
  │    │    urgency_multiplier:
  │    │      48-72h → ×1.0
  │    │      24-48h → ×1.5
  │    │      12-24h → ×2.0
  │    │       0-12h → ×2.5
  │    │    our_price = original_price + (base_markup × urgency_multiplier)
  │    │    Floor: original + $2 | Cap: original + 25%
  │    └─ Write our_price, our_markup back to slot record
  │
  └─ Write .tmp/aggregated_slots.json (updated in-place)
```

### Step 4: sync_to_supabase.py

```
START
  │
  ├─ Read .tmp/aggregated_slots.json
  ├─ For each slot:
  │    ├─ Upsert to Supabase "slots" table (keyed on slot_id)
  │    └─ On conflict: update all fields
  └─ Purge stale slots (start_time in the past)
```

### Step 5: update_landing_page.py

```
START
  │
  ├─ Read slots from Supabase (or .tmp/aggregated_slots.json)
  ├─ Group by category → city
  ├─ Render Jinja2 HTML template
  │    Shows: service_name, business_name, city, time, our_price, "Book Now" button
  │    Does NOT show: platform, booking_url, original_price, our_markup
  ├─ Write index.html
  └─ Deploy to Cloudflare Pages (or GitHub Pages)
       "Book Now" → links to Stripe checkout via POST /api/book
```

---

## 3. Process 2: Customer Booking Flow (Human-in-Loop)

**Entry points:** Landing page "Book Now" button, MCP `book_slot` tool, direct API call
**Status:** ✅ Stripe checkout creation working | ❌ Real booking execution untested (dry-run only)

```
CUSTOMER: POST /api/book
  { slot_id, customer_name, customer_email, customer_phone, quantity }
  │
  ├─ Validate: all required fields present?
  │    └─ NO → 400 Missing required fields
  │
  ├─ Idempotency check (idempotency_key provided?):
  │    ├─ In-memory cache hit → return same checkout_url (idempotent_replay: true)
  │    └─ Supabase idem_ record exists → return same checkout_url
  │
  ├─ get_slot_by_id(slot_id):
  │    └─ NOT FOUND → 404 "This slot is no longer available"
  │
  ├─ Already booked check (_load_booked()):
  │    └─ YES → 409 "This slot has already been booked"
  │
  ├─ Already started check (start_time ≤ now):
  │    └─ YES → 410 "This slot has already started"
  │
  ├─ our_price > 0 check:
  │    └─ NO → 400 "Not available for checkout"
  │
  ├─ Stripe: create checkout session (capture_method=manual — HOLD not charge)
  │    metadata: slot_id, customer info, booking_id, platform, booking_url, quantity
  │    │
  │    ├─ STRIPE ERROR → 500 "Payment system error"
  │    └─ SUCCESS → save pending_payment record to Supabase Storage
  │
  └─ Return: { success: true, checkout_url, booking_id, status: "pending_payment" }


CUSTOMER: Completes payment on Stripe hosted checkout page
  │
  └─ Stripe fires: POST /api/webhook  (checkout.session.completed)


POST /api/webhook (Stripe event handler)
  │
  ├─ Verify Stripe HMAC signature
  │    └─ INVALID → 400 (Stripe will retry)
  │
  ├─ event.type == checkout.session.expired?
  │    └─ YES → mark booking record as "expired", return 200
  │
  ├─ event.type == checkout.session.completed, metadata.event_type == wallet_topup?
  │    └─ YES → credit wallet, return 200 (fast path — no fulfillment needed)
  │
  ├─ Idempotency: session already in-flight? (in-memory lock)
  │    └─ YES → return 200 "already_processing"
  │
  ├─ Idempotency: session already processed? (Supabase Storage record)
  │    └─ YES → return 200 "already_processed"
  │
  ├─ Mark session as "processing" in Supabase Storage
  │
  ├─ Spawn daemon thread: _fulfill_booking_async()
  │
  └─ Return 200 immediately to Stripe


_fulfill_booking_async() [daemon thread, 45s hard ceiling]
  │
  ├─ Send "booking_initiated" email to customer (non-fatal if fails)
  │
  ├─ dry_run == true?
  │    ├─ YES → synthetic confirmation, skip supplier, skip payment capture
  │    │        (used for end-to-end pipeline testing)
  │    └─ NO → _fulfill_booking(slot_id, customer, platform, booking_url, quantity)
  │              [runs in sub-thread, 45s timeout enforced by ThreadPoolExecutor]
  │
  │  ┌─ _fulfill_booking() ──────────────────────────────────────────────────┐
  │  │                                                                       │
  │  │  Check circuit breaker for this supplier:                             │
  │  │    ├─ OPEN (supplier recently failed 3×) → raise BookingUnavailableError
  │  │    └─ CLOSED → proceed                                                │
  │  │                                                                       │
  │  │  Parse booking_url JSON → extract OCTO params:                        │
  │  │    base_url, api_key_env, product_id, option_id, availability_id, unit_id
  │  │                                                                       │
  │  │  OCTOBooker.run():                                                    │
  │  │    │                                                                  │
  │  │    ├─ POST /reservations (hold)                                       │
  │  │    │    body: { productId, optionId, availabilityId,                  │
  │  │    │            unitItems: [{unitId}×quantity],                       │
  │  │    │            contact: {fullName, emailAddress, phoneNumber} }      │
  │  │    │    ├─ 2xx → reservation_uuid captured                            │
  │  │    │    ├─ 409 (availability conflict):                               │
  │  │    │    │    ├─ POST /availability to get fresh slots                 │
  │  │    │    │    ├─ Find first AVAILABLE/FREESALE/LIMITED slot            │
  │  │    │    │    ├─ new availability_id found → retry POST /reservations  │
  │  │    │    │    │    _meta["re_resolved"] = true                         │
  │  │    │    │    └─ no fresh slot → raise BookingUnavailableError         │
  │  │    │    ├─ 4xx (non-409) → raise BookingUnknownError (no retry)      │
  │  │    │    └─ 5xx → retry once after jitter (1-1.5s)                    │
  │  │    │         → still fails → raise BookingTimeoutError                │
  │  │    │                                                                  │
  │  │    └─ POST /bookings/{reservation_uuid}/confirm                       │
  │  │         body: { contact, resellerReference: "LMD-{slot_id[:12]}" }   │
  │  │         ├─ 2xx → extract confirmation (OCTO uuid) + supplierReference │
  │  │         ├─ 4xx → _octo_cleanup() to release hold, raise error        │
  │  │         └─ 5xx → retry once, then _octo_cleanup(), raise error       │
  │  │                                                                       │
  │  │  _octo_cleanup() (orphaned reservation release):                      │
  │  │    ├─ DELETE /bookings/{reservation_uuid}                             │
  │  │    ├─ Retries once                                                    │
  │  │    └─ Both fail → set meta["cleanup_required"]=true                  │
  │  │         → caller saves cleanup record to Supabase for manual review   │
  │  │                                                                       │
  │  │  Return: (confirmation_uuid, booking_meta, supplier_reference)        │
  │  └────────────────────────────────────────────────────────────────────────┘
  │
  ├─ SUCCESS PATH:
  │    ├─ stripe.PaymentIntent.capture(payment_intent)  ← CHARGES CARD
  │    ├─ _mark_booked(slot_id)  ← prevents double-booking
  │    ├─ Save booking record to Supabase Storage:
  │    │    status: "booked", confirmation, supplier_reference, execution_duration_ms, etc.
  │    ├─ Update webhook idempotency record: status "booked"
  │    └─ Send "booking_confirmed" email with cancel link
  │
  └─ FAILURE PATH:
       ├─ stripe.PaymentIntent.cancel(payment_intent)  ← CUSTOMER NOT CHARGED
       ├─ Save booking record: status "failed", failure_reason, error
       ├─ Update webhook idempotency record: status "failed"
       └─ Send "booking_failed" email to customer


CUSTOMER: GET /bookings/{booking_id}
  Returns: { status, confirmation, service_name, executed_at, ... }
  Statuses: pending_payment → booked | failed | expired | cancelled
```

---

## 4. Process 3: Agent Autonomous Booking (Wallet-Based)

**Entry point:** POST /api/book/direct
**Status:** ⚠️ Implemented, not production-tested

```
AGENT: POST /api/book/direct
  { slot_id, customer, wallet_id, execution_mode: "autonomous" }
  │
  ├─ execution_mode != "autonomous" → 400 (prevents accidental charges)
  ├─ wallet_id missing → 400
  │
  ├─ Load wallet from Supabase Storage
  │    ├─ NOT FOUND → 401
  │    └─ HMAC signature invalid → 401
  │
  ├─ Check spending limit:
  │    ├─ slot.our_price > wallet.spending_limit_cents/100 → 402 "Exceeds spending limit"
  │    └─ slot.our_price > wallet.balance → 402 "Insufficient balance"
  │
  ├─ Debit wallet (reserve funds)
  │
  ├─ _fulfill_booking(slot_id, customer, platform, booking_url)
  │    [same OCTOBooker.run() path as human booking]
  │    │
  │    ├─ SUCCESS → wallet debit committed, save booking record
  │    └─ FAILURE → wallet debit reversed, save failed record
  │
  └─ Return: { success, confirmation, booking_id, wallet_balance_remaining }
```

---

## 5. Process 4: Supplier-Initiated Cancellation (Bokun Webhook)

**Entry point:** POST /api/bokun/webhook?token={BOKUN_WEBHOOK_TOKEN}
**Status:** ✅ Implemented and verified (smoke tested 2026-04-16)

```
BOKUN: POST /api/bokun/webhook?token=...
  (fires when supplier cancels in their Bokun dashboard)
  │
  ├─ Token check: request.args["token"] vs BOKUN_WEBHOOK_TOKEN (hmac.compare_digest)
  │    ├─ MISSING/WRONG → 401 Unauthorized
  │    └─ BOKUN_WEBHOOK_TOKEN not set in env → WARNING log, allow through
  │
  ├─ Parse JSON body:
  │    booking_data = data["booking"] OR data (handles nested or flat format)
  │    bokun_status = booking_data["status"].upper()
  │    confirmation = booking_data["confirmationCode"] OR ["confirmation_code"] OR ["id"]
  │
  ├─ Is cancellation? (status in CANCELLED/CANCELED OR "cancel" in event_type)
  │    └─ NO → return 200 {"action": "event_ignored"} (new bookings, modifications, etc.)
  │
  ├─ _find_booking_by_confirmation(confirmation):
  │    Scans all Supabase Storage booking records
  │    Matches on: record["confirmation"] == code OR record["supplier_reference"] == code
  │    ├─ NOT FOUND → return 200 {"action": "not_found"}
  │    └─ FOUND → (booking_id, record)
  │
  ├─ Already cancelled? → return 200 {"action": "already_cancelled"}
  │
  ├─ _refund_stripe(payment_intent_id):
  │    ├─ Check if payment captured or just held
  │    ├─ If captured → stripe.Refund.create()
  │    ├─ If held (uncaptured) → stripe.PaymentIntent.cancel()
  │    ├─ Retry up to 3× with backoff
  │    ├─ SUCCESS → refund_id recorded
  │    └─ FAILURE → log prominently, continue (record still marked cancelled)
  │
  ├─ Update booking record: status "cancelled", cancelled_by "supplier_bokun_webhook"
  │
  ├─ Send cancellation email to customer
  │    └─ FAILURE → log, non-fatal
  │
  └─ Return 200 { action: "cancelled", booking_id, refund_id }
```

---

## 6. Process 5: Customer Self-Serve Cancellation

**Entry point:** GET/POST /cancel/{booking_id}?t={hmac_token}
**Status:** ✅ Implemented | ⚠️ OCTO cancellation not queued for retry if it fails

```
CUSTOMER: GET /cancel/{booking_id}?t={token}
  │
  ├─ Verify HMAC token (_verify_cancel_token)
  │    └─ INVALID → 403 "Invalid or expired link" HTML page
  │
  ├─ Load booking record
  │    └─ NOT FOUND → 404 HTML page
  │
  ├─ Already cancelled? → show "already cancelled" HTML page
  │
  └─ Show cancellation confirmation HTML page with "Confirm Cancellation" button


CUSTOMER: POST /cancel/{booking_id}?t={token}  [form submit]
  │
  ├─ Verify HMAC token (same check)
  ├─ Load booking record
  │
  ├─ _refund_stripe(payment_intent_id)
  │    [same 3× retry logic as supplier cancellation]
  │
  ├─ OCTO cancellation (if OCTO platform and confirmation exists):
  │    _cancel_octo_booking(supplier_id, confirmation)
  │    ├─ SUCCESS → booking released on supplier side
  │    └─ FAILURE → logged but NOT queued for retry ⚠️ (gap — OCTO hold may persist)
  │
  ├─ Update booking record: status "cancelled", cancelled_by "customer_self_serve"
  │
  ├─ Send cancellation email to customer
  │
  └─ Show "Booking cancelled" HTML page
```

**Gap:** Unlike the API cancellation endpoint (`DELETE /bookings/{id}`), `self_serve_cancel` does not queue failed OCTO cancellations for background retry. If the OCTO call fails, the booking record shows cancelled but the supplier still has an active reservation.

---

## 7. Process 6: MCP Agent Integration

**Entry points:**
- `POST /mcp` — MCP-over-HTTP (Smithery, direct API agents)
- `GET /sse` — SSE transport proxy (Claude Desktop, Claude Code)
- `run_mcp_remote.py` — standalone SSE server at mcp.lastminutedealshq.com

**Status:** ✅ search_slots, get_supplier_info working | ✅ book_slot returns checkout_url | ❌ no autonomous booking via MCP (requires human Stripe checkout)

```
MCP CLIENT: POST /mcp  { method: "tools/call", params: { name, arguments } }
  │
  ├─ _mcp_call_tool(name, arguments):
  │
  ├─ name == "search_slots":
  │    ├─ Build cache_key from (hours_ahead, category, city, max_price, limit)
  │    ├─ _MCP_SLOTS_CACHE hit (< 60s old)? → return cached result
  │    ├─ _load_slots_from_supabase(hours_ahead, category, city, budget, limit=100)
  │    │    ├─ Supabase REST query on "slots" table
  │    │    ├─ Filters: hours_until_start ≤ hours_ahead, category match, city match, price ≤ max
  │    │    └─ Pagination: Supabase default limit (1000 rows max per page)
  │    ├─ _sanitize_slot() — strips internal fields, recomputes hours_until_start dynamically
  │    ├─ Store in _MCP_SLOTS_CACHE for 60s
  │    └─ Return list of safe slot dicts
  │
  ├─ name == "book_slot":
  │    ├─ POST /api/book internally
  │    └─ Return { checkout_url, booking_id } — customer must complete payment manually
  │
  ├─ name == "get_booking_status":
  │    └─ GET /bookings/{booking_id} → return booking record
  │
  └─ name == "get_supplier_info":
       └─ Return static supplier directory dict
```

**Transport note:**
- Smithery → connects via `server.json` → `POST /mcp` on Flask server (Railway)
- Claude Desktop / Claude Code → `GET /sse` → proxied to embedded FastMCP SSE thread
- `mcp.lastminutedealshq.com` → `run_mcp_remote.py` standalone SSE server (separate Railway service)

---

## 8. Process 7: Goal-Oriented Autonomous Booking

**Entry points:** POST /execute/best, POST /execute/guaranteed
**Status:** ⚠️ Implemented, not production-tested

```
POST /execute/best
  { goal, city, category, budget, hours_ahead, customer, wallet_id }
  │
  ├─ Load all matching slots from Supabase
  ├─ Score each slot by goal:
  │    maximize_value   → highest (original_price - our_price) / our_price
  │    minimize_wait    → lowest hours_until_start
  │    maximize_success → highest platform_reliability × confidence score
  │    minimize_price   → lowest our_price within budget
  ├─ Select top-scoring candidate
  └─ Execute via _fulfill_booking() [same OCTO path]


POST /execute/guaranteed
  { slot_id OR (category + city), customer, wallet_id, max_retries }
  │
  ├─ If slot_id provided → try that slot first
  │    └─ On failure → fall back to next available in same category/city
  ├─ Up to max_retries attempts across different slots
  └─ Return: { success, confirmation, slot_used, attempts }
```

---

## 9. Process 8: Intent Sessions

**Entry point:** POST /intent/create, GET /intent/{id}, POST /intent/{id}/execute
**Status:** ⚠️ Implemented, not production-tested

```
POST /intent/create
  { category, city, budget, customer, wallet_id }
  → Creates a pending intent. Watcher monitors for matching slots.
  → Returns intent_id

GET /intent/{intent_id}
  → Returns { status, matched_slot, executed, booking_id }
  Statuses: waiting → matched → executing → completed | expired | cancelled

POST /intent/{intent_id}/execute
  → Manually trigger execution on the matched slot
  → Goes through _fulfill_booking() path

POST /intent/{intent_id}/cancel
  → Cancels the intent if not yet executed
```

---

## 10. Process 9: Wallet System

**Status:** ⚠️ Implemented, not production-tested

```
POST /api/wallets/create
  { name, email }
  → Creates wallet with wallet_id, api_key, balance=0
  → Persisted in Supabase Storage

POST /api/wallets/fund
  { wallet_id, amount_usd }
  → Creates Stripe checkout session for top-up
  → On payment: Stripe fires /api/webhook → wallet_topup fast path → credit_wallet()

GET /api/wallets/{wallet_id}/balance
  → Requires wallet api_key in X-API-Key header
  → Returns { balance, spending_limit, transactions_count }

GET /api/wallets/{wallet_id}/transactions
  → Returns paginated transaction history

PUT /api/wallets/{wallet_id}/spending-limit
  → Sets per-transaction spending cap
  → Defaults to $400/transaction if not set
```

---

## 11. Process 10: B2B API Key Access

**Status:** ✅ Key generation working | ⚠️ Key-gated endpoints not all tested

```
POST /api/keys/register
  { name, email, use_case }
  → Generates lmd_... API key
  → Persisted to Supabase Storage
  → Returns { api_key, rate_limit, endpoints }

API key required for:
  DELETE /bookings/{id}    — programmatic cancellation + refund
  POST /api/book/direct    — autonomous booking (also requires wallet_id)
  POST /execute/best       — goal-oriented booking
  POST /execute/guaranteed — retry-booking
  POST /api/wallets/*      — wallet operations

No API key required:
  GET /slots               — slot search
  POST /api/book           — create Stripe checkout (returns URL, no execution)
  GET /bookings/{id}       — booking status
  POST /mcp                — MCP tool calls (search only without key)
```

---

## 12. Infrastructure & Dependencies

| Component | Service | Status | Notes |
|---|---|---|---|
| Slot storage | Supabase "slots" table | ✅ | REST API only — direct Postgres blocked by Railway |
| Booking records | Supabase Storage "bookings" bucket | ✅ | JSON files per booking |
| Request logs | Supabase Postgres "request_logs" | ❌ | Direct TCP blocked from Railway — /health success rates always null |
| API server | Railway (web service) | ✅ | Auto-redeploys on git push to main |
| MCP SSE server | Railway (mcp service) | ✅ | run_mcp_remote.py on mcp.lastminutedealshq.com |
| Payments | Stripe | ✅ | Checkout sessions + webhooks + auth-capture pattern |
| Supplier booking | Bokun OCTO API | ✅ (API) / ❌ (real booking untested) | 11 vendor IDs, OCTO reseller |
| Cancellation notifications | Bokun HTTP notification | ✅ | URL token auth, smoke tested |
| Email | SendGrid | ✅ | booking_initiated, booking_confirmed, booking_failed, booking_cancelled |
| Landing page | Cloudflare Pages | ✅ | Rebuilt every pipeline run |
| Slot discovery | Local Windows laptop | ⚠️ | No cloud scheduling yet — manual or Task Scheduler |
| Pricing history | Google Sheets | ❌ | OAuth token expired — compute_pricing uses defaults |
| SMS alerts | Twilio | 🔲 | Implemented, not activated |
| Social posting | Twitter/Reddit/Telegram | 🔲 | Scripts exist, not running |

---

## 13. Known Issues & Gaps

### Critical (affects live bookings)

| # | Issue | Impact | Fix |
|---|---|---|---|
| C1 | **Real booking never tested end-to-end** | Don't know if OCTOBooker.run() actually works with real payment capture against Bokun | Run one real booking with cheapest slot — requires user confirmation |
| C2 | **self_serve_cancel doesn't queue failed OCTO cancellations** | If Bokun API fails during customer cancel, supplier still has active reservation | Add `_queue_octo_retry()` call matching the `DELETE /bookings` endpoint |

### High (data quality / reliability)

| # | Issue | Impact | Fix |
|---|---|---|---|
| H1 | **314 unmapped Bokun products** | Show as "Bokun Reseller" with no city — bad UX, invisible in city search | Run scrape_bokun_supplier_directory.py → update reference_supplier_map |
| H2 | **Google Sheets OAuth expired** | compute_pricing.py runs on default markups — no urgency-based pricing, no learning | Re-authenticate OAuth token |
| H3 | **Slot discovery runs on local laptop** | Pipeline fails if laptop is off/asleep — no redundancy | Move to GitHub Actions scheduled workflow (free) |

### Medium (observability)

| # | Issue | Impact | Fix |
|---|---|---|---|
| M1 | **request_logs Postgres blocked from Railway** | /health success rates always null, /metrics api_usage always errors | Switch request logging to Supabase REST API instead of direct Postgres |
| M2 | **Smithery uptime shows 80.3%** | Some search_slots calls still timing out occasionally | Monitor after 60s cache fix; may need to investigate further |

### Low (missing features)

| # | Issue | Impact | Fix |
|---|---|---|---|
| L1 | **Customer cancellation OCTO failure not retried** | Supplier may have active booking after customer cancel | Queue for retry same as API cancel endpoint |
| L2 | **No real-time slot watcher** | Slots only update every 4h | watch_slots_realtime.py exists but not running |
| L3 | **Social posting not running** | No deal distribution to Twitter/Reddit/Telegram | Run post_to_telegram.py at minimum |

---

## 14. Environment Variables Required

### Local (.env)

| Variable | Purpose | Set? |
|---|---|---|
| BOKUN_API_KEY | Bokun OCTO reseller token | ✅ |
| BOKUN_ACCESS_KEY | Bokun REST API access key | ✅ |
| BOKUN_SECRET_KEY | Bokun REST API secret key | ✅ |
| SUPABASE_URL | Supabase project URL | ✅ |
| SUPABASE_SECRET_KEY | Supabase service role key | ✅ |
| STRIPE_SECRET_KEY | Stripe secret key | ✅ |
| STRIPE_WEBHOOK_SECRET | Stripe webhook signing secret | ✅ |
| SENDGRID_API_KEY | Email sending | ✅ |
| RAILWAY_TOKEN | Railway GraphQL API access | ✅ |
| GOOGLE_SHEET_ID | Pricing log sheet | ✅ |
| LANDING_PAGE_URL | https://lastminutedealshq.com | ✅ |
| BOOKING_SERVER_HOST | https://api.lastminutedealshq.com | ✅ |
| LMD_WEBSITE_API_KEY | Internal API key for Railway API | ✅ |

### Railway (web service) — set via Railway dashboard or GraphQL API

| Variable | Purpose | Set? |
|---|---|---|
| BOKUN_API_KEY | Same as local | ✅ |
| BOKUN_ACCESS_KEY | Same as local | ✅ |
| BOKUN_SECRET_KEY | Same as local | ✅ |
| BOKUN_WEBHOOK_TOKEN | Authenticates Bokun notifications | ✅ |
| SUPABASE_URL | Same as local | ✅ |
| SUPABASE_SECRET_KEY | Same as local | ✅ |
| STRIPE_SECRET_KEY | Same as local | ✅ |
| STRIPE_WEBHOOK_SECRET | Same as local | ✅ |
| SENDGRID_API_KEY | Same as local | ✅ |
| LANDING_PAGE_URL | https://lastminutedealshq.com | ✅ |
| BOOKING_SERVER_HOST | https://api.lastminutedealshq.com | ✅ |
| LMD_WEBSITE_API_KEY | Internal API key | ✅ |
| PORT | Set by Railway automatically | ✅ |

---

## Appendix: Key File Locations

| File | Role |
|---|---|
| `tools/run_api_server.py` | All API endpoints (5,140 lines) — Flask server on Railway |
| `tools/complete_booking.py` | OCTOBooker + platform bookers — executes bookings |
| `tools/fetch_octo_slots.py` | OCTO API client — fetches availability from Bokun |
| `tools/aggregate_slots.py` | Deduplication and filtering |
| `tools/compute_pricing.py` | Dynamic markup engine |
| `tools/sync_to_supabase.py` | Upserts slots to Supabase |
| `tools/update_landing_page.py` | Regenerates HTML landing page |
| `tools/run_mcp_remote.py` | Standalone MCP SSE server |
| `tools/seeds/octo_suppliers.json` | Supplier config + reference_supplier_map |
| `tools/seeds/airbnb_listing_ids.json` | Airbnb listing IDs for iCal fetcher |
| `run_pipeline.bat` | Pipeline orchestrator (runs all 5 steps in sequence) |
| `railway.json` | Railway deployment config |
| `server.json` | MCP server descriptor (for Smithery) |
| `SYSTEM_MAP.md` | This file — update whenever system changes |
