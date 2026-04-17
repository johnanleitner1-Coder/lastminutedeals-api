# Bug Audit Log — Last Minute Deals API

All confirmed bugs found and fixed across debugging sessions. Ordered by bug number; session-new bugs appended at the end.

---

## Session 1–3 Fixes (commits b3116b9, 1d562ef, 2077e3f)

### CRITICAL

| # | File | Bug | Fix |
|---|---|---|---|
| 1 | `run_api_server.py` | `book_direct` unpacked `_fulfill_booking()` return as 2-tuple — raises `ValueError` on every autonomous booking call | Changed to 3-tuple unpack `(confirmation, booking_meta, supplier_reference)` |
| 3 | `retry_cancellations.py` | Double-prefix path bug — Storage list API returns full paths; code prepended `cancellation_queue/` again → every retry 404'd silently; entire retry queue non-functional | Fixed path construction; skip terminal entries (`exhausted`/`permanent_error`) |
| 5 | `run_api_server.py` | `_signing_secret()` stored key in `.tmp/` — wiped on every Railway redeploy, invalidating all outstanding customer cancel links | Requires `LMD_SIGNING_SECRET` env var; loud warning if unset |
| 7 | `run_api_server.py` | Stripe idempotency check blocked retries for `"failed"` sessions — only `"booked"` should be idempotent | Only `"booked"` sessions treated as idempotent |
| 11 | `run_api_server.py` | `book_with_saved_card` assigned `_fulfill_booking()` 3-tuple to scalar — same root cause as Bug 1 | Fixed to 3-tuple assignment |

### HIGH

| # | File | Bug | Fix |
|---|---|---|---|
| 2 | `execution_engine.py` | `_attempt_booking()` didn't handle dict return from `OCTOBooker.run()` — confirmation never extracted; `supplier_reference` and `booking_meta` lost | Extract confirmation string from dict; store supplier_reference and booking_meta |
| 6 | `run_api_server.py` | `book_direct` recovery record partial-writes destroyed `wallet_id`/`amount_cents` — crash reconciliation broken | Read-merge-write pattern instead of full overwrite |
| 8 | `run_api_server.py` | Non-unique `booking_record_id = "bk_{slot_id[:12]}"` in `book_direct` and `book_with_saved_card` — concurrent bookings of same slot collide | Added UUID suffix (matching `/api/book` path) |
| 9 | `run_api_server.py` | `GET /bookings/<id>` had no authentication — any caller could enumerate booking records (IDOR) | Requires `X-API-Key` |
| 10 | `run_api_server.py` | `GET /verify/<booking_id>` returned full PII — name, email, phone, payment intent visible publicly | Strips all PII fields from public response |
| 12 | `run_api_server.py` | Bokun webhook marked booking `"cancelled"` even when Stripe refund failed — customer loses money with no record | Flags as `"cancellation_refund_failed"` and returns early; does not mark cancelled |
| 13 | `run_api_server.py` | `self_serve_cancel` ignored Stripe refund result — always sent "refund issued" email | Check refund outcome; send accurate email |
| 14 | `run_api_server.py` | `self_serve_cancel` silently dropped OCTO cancellation failures — no retry queued | OCTO failures now queued for automatic retry |
| 15 | `run_api_server.py` | `_find_booking_by_confirmation` had hard 500-record limit — Bokun webhook lookups failed past 500 bookings | Paginates in pages of 500 until all records scanned |
| 16 | `reconcile_bookings.py` | Hard 1000-record limit | Same paginated fix |
| 17 | `run_api_server.py` | Peek webhook had no authentication — any party could inject fake status updates | Verified via `PEEK_WEBHOOK_SECRET` env var |
| 18 | `run_api_server.py` | `DELETE /bookings` marked `"cancelled"` even when both Stripe refund AND OCTO cancel failed | Only marks cancelled when Stripe succeeds; `"cancellation_refund_failed"` + HTTP 502 otherwise |
| 19 | `run_api_server.py` | Multi-worker APScheduler guard used `os.environ` (per-process) — each gunicorn worker started its own scheduler causing duplicate runs | File-based exclusive lock (`.tmp/_scheduler.pid`) shared across all workers |
| 20 | `run_api_server.py` | `stripe_customers.json` and `webhook_subscriptions.json` in `.tmp/` — wiped on every Railway redeploy, losing saved payment methods and webhook subs | Moved to Supabase Storage; `.tmp/` kept as local cache only |
| 22 | `complete_booking.py` | `GenericBooker.complete()` returned a fake success string instead of raising — caller couldn't distinguish success from failure; payment could be captured for unautomated bookings | Now raises `BookingUnknownError` |
| 23 | `run_api_server.py` | `book_direct` booking record omitted `supplier_reference` | Added once Bug 1 made the 3-tuple available |
| 24 | `manage_wallets.py` | `debit_wallet()` never checked `spending_limit_cents` at the wallet level — only the route checked it; execution engine bypassed the limit | Limit enforced at wallet level on all callers |
| 25 | `run_api_server.py` | Intent session stayed `"executing"` forever if `engine.execute()` raised an unexpected exception | Added try/except; transitions to `"failed"` with error note |
| 27 | `complete_booking.py` | OCTOBooker 409 re-resolution matched "first available slot" without checking `start_time` — could silently rebook a different time | Requires `localDateTimeStart` to match original `start_time` prefix |
| 28 | `run_api_server.py` | Playwright availability check blocked ALL platforms including `OCTOBooker` (pure HTTP, no browser) | Moved check after platform dispatch; exempts `OCTOBooker` |
| 29 | `run_api_server.py` | `_validate_api_key` hit Supabase twice per request | Added 30s in-process TTL cache; removed usage-count writes from hot path |
| 34 | `run_api_server.py` | `list_inbound_emails` used `==` for auth comparison (timing oracle) | `hmac.compare_digest` for constant-time comparison |
| 35 | `execution_engine.py` | `_cancel_stripe()` swallowed all exceptions silently | Now logs prominently for manual review |
| 38 | `intent_sessions.py` | `get()`, `list_by_api_key()`, `actionable_sessions()` read session data without `_sessions_lock` — race condition with concurrent writes | All three acquire lock before reading |
| 39 | `run_api_server.py` | `_fire_callback()` made synchronous HTTP call in the monitor thread — 10s timeout could block entire intent monitor | Dispatched in a daemon thread |
| 40 | `execution_engine.py` | `market_insights` module loaded via `exec_module` on every booking attempt — O(n_attempts) module loads per `execute()` call | Loaded once at start of `execute()` and reused |
| 41 | `circuit_breaker.py` | `SUPABASE_URL`/`SUPABASE_SECRET_KEY` read at module import time — if imported before dotenv loaded, circuit breaker silently disabled for process lifetime | Now calls `os.getenv()` at each use point |
| 42 | `circuit_breaker.py` | Same as Bug 41 | Same fix |
| 43 | `circuit_breaker.py` | Half-open state allowed unlimited concurrent probes — all simultaneous callers could be let through, defeating single-probe design | `probe_started_at` timestamp blocks concurrent callers for 30s |
| 44 | `manage_wallets.py` | `create_topup_session()` called `_load_wallets()` twice (TOCTOU) — concurrent write between loads could clobber data | Single load; update in-place; single save |
| 51 | `execution_engine.py` | `booked_slots.json` write was non-atomic — crash mid-write could corrupt file | Writes to `.tmp` file first, then atomically renames to final path |

### MEDIUM

| # | File | Bug | Fix |
|---|---|---|---|
| 21 | `execution_engine.py` | `_mark_booked()` had no thread safety — concurrent bookings could corrupt `booked_slots.json` | Added `_BOOKED_LOCK` + atomic write |
| 26 | `run_api_server.py` | _(details in commit)_ | Fixed |
| 31 | `run_api_server.py` | `_queue_octo_retry()` checked `SUPABASE_URL` but not `SUPABASE_SECRET_KEY` — silently dropped retry entry if only secret was missing | Checks both; logs prominent error with exact env var names |
| 37 | `manage_wallets.py` | `get_wallet_by_api_key()` triggered full Supabase round-trip with no caching on every wallet-authenticated request | Added 15s in-process TTL cache to `_load_wallets()`; cache invalidated immediately on writes |
| EE-4 | `execution_engine.py` | Hardcoded `+0.1` confidence floor ensured score always ≥ 0.1 — intent monitor always attempted booking even with zero matching slots | Bonus only added when `slot_count > 0` |

---

## Session 4 Fixes — Simplify Cleanup (commit b5fd36f)

| # | File | Fix |
|---|---|---|
| S-1 | `run_api_server.py` | Removed second inline `import uuid` in `book_with_saved_card`; use module-level import |
| S-2 | `run_api_server.py` | Moved `_PII_FIELDS` from inside `verify_booking()` to module-level `frozenset` |
| S-3 | `run_api_server.py` | Added 30s TTL caches for `_load_customers()` and `_load_webhooks()` with write-through on save |
| S-4 | `manage_wallets.py` | Fixed `create_topup_session` misleading "avoids a second read" comment — code DID call `_load_wallets()` again; simplified to single load-update-save |
| S-5 | `manage_wallets.py` | Removed WHAT comments (`# Populate in-process cache`, `# Write local cache`, etc.) |

---

## Session 5 Fixes — Deep Bokun Audit (commit 35d350e)

| # | Severity | File | Bug | Fix |
|---|---|---|---|---|
| D-1 | CRITICAL | `run_api_server.py` | `self_serve_cancel` HTML confirmation page always displayed "a full refund has been issued" even when Stripe refund failed | Page now uses `refund_issued` flag; accurate message shown based on actual Stripe outcome |
| D-2 | HIGH | `run_api_server.py` | `_cancel_octo_booking` sent `Octo-Capabilities: octo/pricing` on DELETE — header was deliberately removed from booking execution because Bokun hangs on non-availability calls | Header removed from DELETE |
| D-3 | HIGH | `retry_cancellations.py` | `_cancel_octo` same header on DELETE | Removed |
| D-4 | HIGH | `reconcile_bookings.py` | `_verify_octo_booking` same header on GET `/bookings/{uuid}` | Removed |
| D-5 | HIGH | `send_booking_email.py` | `_build_failed_html` used `booking_url` as `retry_url` — for OCTO/Bokun slots `booking_url` is a JSON blob `{"_type":"octo",...}`; results in a broken href in the "Browse Available Deals" button | Check for `http` prefix before using as URL; fall back to site root |
| D-6 | MEDIUM | `run_api_server.py` | `_get_reliability_metrics` and `_find_booking_by_confirmation` missing prefix filters (`config/`, `cleanup_`, `pending_exec_`, `inbound_emails/`) — internal files inflate booking counts and appear in confirmation scans | Added all missing exclusion prefixes |
| D-7 | MEDIUM | `reconcile_bookings.py` | `_list_bookings` only excluded `cancellation_queue/` — all other internal prefixes (`circuit_breaker/`, `config/`, `idem_`, `webhook_session_`, `cleanup_`, `pending_exec_`, `inbound_emails/`) inflated reconciliation workload | Added all missing prefix filters |
| D-8 | MEDIUM | `run_api_server.py` | `_fulfill_booking` used `"burl_j" in dir()` — if the JSON parse exception fired, `burl_j` was unbound and the `dir()` check silently evaluated to `False`, masking OCTO detection | `burl_j = {}` initialized before the try block; `dir()` check removed |

---

## Session 6 Fixes — Slot Inventory Audit (commit 5dcb876)

| # | Severity | File | Bug | Fix |
|---|---|---|---|---|
| I-1 | HIGH | `sync_to_supabase.py` + Supabase | 263 test-mode supplier slots (Zaui Test, Ventrata Edinburgh Explorer, Peek Pro Test) in live production inventory — unbookable, served to real users; one real customer booked a Zaui test slot leaving zombie booking `bk_6aab082e36ec` | Immediate: deleted all 263 rows from Supabase. Permanent: `_TEST_SUPPLIER_NAMES` filter strips test slots before every upsert; `delete_test_supplier_slots()` purges any that slip through |
| I-2 | LOW | Supabase Storage | Zombie booking `bk_6aab082e36ec` — dry run test booking (Zaui test supplier, no real customer, no payment) stuck in reconcile loop every 30 min because `supplier_id=zaui_test` has no active config | Marked `status=cancelled`, `resolved=true` directly in Supabase Storage — reconciler skips non-booked records |

---

## Session 8 Fixes — Structural Resolution Fix

| # | Severity | File | Bug | Fix |
|---|---|---|---|---|
| V-3 | HIGH | `fetch_octo_slots.py` + `octo_suppliers.json` | Supplier resolution was reactive and fragile — any new product with an unrecognised ref pattern or null ref would silently become "Bokun Reseller" with no city/country; no alerting; whack-a-mole approach required manual discovery after the fact | Extracted `_resolve_product_identity()` with 3-level chain: (1) ref prefix map, (2) product_id exact match, (3) `vendor_id_to_supplier_map` catch-all covering all 13 known vendors. Pre-resolves once per product (not per availability slot), logs explicit WARNING if all 3 levels fail. Any future product from any known vendor is guaranteed to resolve. Added `Octo-Capabilities` header bug fix to `test_octo_connection.py` (same D-2 pattern missed in that file). |

---

## Session 7 Fixes — Vendor Coverage Audit

| # | Severity | File | Bug | Fix |
|---|---|---|---|---|
| V-1 | HIGH | `tools/seeds/octo_suppliers.json` | EgyExcursions (vendor 123380, Egypt) and Vakare Travel Service (vendor 98502, Turkey/Antalya) accepted Bokun partners not in `vendor_ids` — their 120 products and ~3000 slots never fetched | Added both IDs to `vendor_ids` array; 13 vendors, 336 products, 4536 slots now live |
| V-2 | HIGH | `tools/seeds/octo_suppliers.json` + `fetch_octo_slots.py` | `reference_supplier_map` missing entries for `5519190P` (EgyExcursions), `344574P` (Vakare), `384441P` (Marvel new products), `D0`/`D4` (Íshestar), `#2 SINTRA`/`#3 SINTRA` (O Turista) — 350 slots resolved as "Bokun Reseller" with no city. 2 remaining products had null/empty API reference strings and couldn't match any prefix | Added all missing prefix entries; added `product_id_map` fallback in `fetch_octo_slots.py` for products with null/empty refs; **0 unresolved slots** |

---

## Session 9 Fixes — MCP Agent Inventory Visibility

| # | Severity | File | Bug | Fix |
|---|---|---|---|---|
| M-1 | CRITICAL | `run_api_server.py` | `get_supplier_info` had two diverging hardcoded implementations (POST /mcp: 9 suppliers incl. disabled Ventrata/Zaui; FastMCP SSE: 7 different suppliers). Both missing Vakare Travel Service (61% of OCTO inventory, 2,781 slots) and EgyExcursions. Any agent calling `get_supplier_info` received stale data that would never update with new suppliers | Replaced both with single `_get_live_supplier_directory()` — queries Supabase for distinct `(business_name, location_city, location_country)` per slot, groups client-side, caches 5 minutes, falls back to `_SUPPLIER_DIR_STATIC` (covers all 14 active Bokun suppliers) if Supabase unreachable |
| M-2 | CRITICAL | `run_api_server.py` | POST /mcp `search_slots` default `limit=100` — returned ~2% of 4,500 available slots. An agent with no city/category filter would see 100 of 4,500 slots sorted by time and conclude it had reviewed full inventory | Removed `limit` parameter from agent-facing tool entirely. Cache key no longer includes limit. `_load_slots_from_supabase()` called without limit → uses 10,000 default (full pagination). FastMCP SSE already used 10,000 — now both paths consistent |
| M-3 | HIGH | `run_api_server.py` | `_MCP_TOOLS` `search_slots` description listed Ventrata, Zaui, and Peek Pro as active sources — all three are `enabled: false` in `octo_suppliers.json` with no API keys configured | Updated description to list all 14 active Bokun suppliers; removed disabled platforms; added EgyExcursions and Vakare Travel Service |
| M-4 | MEDIUM | `run_api_server.py` | `_safe()` in FastMCP SSE included `price` field — always `null` because `_sanitize_slot()` strips it before `_safe()` is called. Confusing null field returned to every agent | Removed `price` from `_safe()` field list |
| M-5 | MEDIUM | `run_api_server.py` | `_safe()` in FastMCP SSE missing `location_state` — available in slot data, provides useful city disambiguation | Added `location_state` to `_safe()` field list |
| M-6 | LOW | `run_api_server.py` | `capabilities` metadata hardcoded `"11 suppliers"` — now 14 active suppliers after adding EgyExcursions and Vakare | Updated to `"14 suppliers"` |

---

## Session 10 Fixes — Booking Flow Audit

| # | Severity | File | Bug | Fix |
|---|---|---|---|---|
| B-1 | CRITICAL | `fetch_octo_slots.py` | Bug 27 re-introduced: `start_time` not included in the `booking_url` JSON blob. OCTOBooker 409 re-resolution reads `params.get("start_time")` which always returned None — causing `orig_start = ""` and skipping the time-match guard entirely. Any 409 re-resolution would silently rebook the first available slot regardless of departure time | Added `"start_time": start_iso` to the `booking_url` JSON blob so OCTOBooker's re-resolution always matches the originally-requested time |
| B-2 | CRITICAL | `run_api_server.py` | All three booking record creation paths (`/api/book` pending record, `_fulfill_booking_async` success record, `book_direct` success record) were missing `customer_name`, `customer_phone`, `business_name`, `location_city`, `start_time`. `GET /bookings/<id>` always returned null for these fields — agents polling `get_booking_status()` could not retrieve customer or service details | Added all missing fields to all three record creation paths |
| B-3 | HIGH | `run_api_server.py` | `_fulfill_booking_async` failure path fully overwrote the pending booking record with only 5 failure fields. Lost: `service_name`, `customer_email`, `checkout_url`, `expires_at`. `get_booking_status()` returned a bare failure record with no service context | Changed to merge semantics: read existing record, update with failure fields, save — preserving all previously written fields |
| B-4 | HIGH | `run_api_server.py` | If `stripe.PaymentIntent.capture()` raised after `_fulfill_booking()` already confirmed an OCTO booking, the supplier had a live confirmed booking with no payment. The exception path correctly cancelled the payment hold but left the OCTO booking open at the supplier. Slot was also never marked as booked so future customers could book the same (now supplier-occupied) slot | Pre-initialized `confirmation = None` before the try block. In the failure handler: if `confirmation is not None` (OCTO succeeded) and payment intent exists, queue the orphaned booking via `_queue_octo_retry()` for automatic OCTO cancellation |
| B-5 | HIGH | `run_api_server.py` | `_fulfill_booking()` caught `FileNotFoundError` when `complete_booking.py` is missing and returned `("manual-fulfillment-required", {}, "")` — causing the booking to be marked `status="booked"` with a fake confirmation. Customer gets a "confirmed" booking email with no actual reservation | Changed to raise `Exception("complete_booking.py not found")` — caller cancels payment hold and marks booking failed |
| B-6 | MEDIUM | `run_api_server.py` | FastMCP SSE `book_slot` had no `quantity` parameter — multi-person bookings silently became 1-person bookings. `book_direct` also did not read `quantity`, so wallet debit was always per-person price regardless of group size | Added `quantity: int = 1` to FastMCP `book_slot` signature; forwarded to both `/api/book` and `/api/book/direct`. `book_direct` now reads `quantity`, computes `amount_cents = our_price × quantity × 100`, and passes `quantity` to `_fulfill_booking()` |
| B-7 | LOW | `run_api_server.py` | `GET /bookings/<id>` used `record.get("confirmation_number") or record.get("confirmation")` — no booking path ever writes `confirmation_number` so the first lookup always returned None. Also missing `location_city`, `quantity`, and `failure_reason` from the response | Standardized to `record.get("confirmation")`. Added `location_city`, `quantity`, `failure_reason` to the response |

---

## Session 11 Fixes — Post-Booking Email Audit

| # | Severity | File | Bug | Fix |
|---|---|---|---|---|
| PE-1 | CRITICAL | `run_api_server.py` | `book_direct` (autonomous wallet path) sent zero customer emails — no `booking_confirmed` on success, no `booking_failed` on failure. Customers paying via wallet received no receipt, no confirmation number, no cancellation link | Added `send_booking_email("booking_confirmed", ...)` after successful fulfillment (includes cancel_url if `BOOKING_SERVER_HOST` is set); added `send_booking_email("booking_failed", ...)` in the failure path |
| PE-2 | HIGH | `run_api_server.py` | Both `booking_initiated` and `booking_confirmed` emails displayed `slot.get("our_price")` — the per-person price from the slot record. When `quantity > 1`, the email showed the per-person amount while the card was charged the full total. e.g. 2 tickets at $50 each: email shows "$50.00 charged" while Stripe captured $100.00 | In `_fulfill_booking_async`, override `our_price` in `slot_for_email` copy with `amount_total / 100` (the Stripe session total — already `our_price × quantity`) before passing to any email call. In `book_direct` success email, pass `{**slot, "our_price": amount_cents / 100}` |
| PE-3 | MEDIUM | `run_api_server.py` | `cancel_url` in `booking_confirmed` email was constructed as `f"{os.getenv('BOOKING_SERVER_HOST', '')}/cancel/{id}?t=..."`. If `BOOKING_SERVER_HOST` is unset, the result is `/cancel/abc?t=...` — a truthy string, so the email template rendered a broken relative-path href instead of the fallback text "Reply to this email." | Only build `cancel_url` if `BOOKING_SERVER_HOST` is non-empty; otherwise pass `""` so the email template falls back to the reply-to-email message |

---

## Session 12 Fixes — Cancellation Process Audit

| # | Severity | File(s) | Bug | Fix |
|---|---|---|---|---|
| C-1 | CRITICAL | `run_api_server.py` | `DELETE /bookings/{id}` (agent/API-initiated cancel) sent zero customer emails. Customer's money was refunded but they received no notification — no confirmation of cancellation, no refund notice, no paper trail. In a dispute this leaves us with no evidence the cancellation was communicated | Added `send_booking_email("booking_cancelled", ...)` after `_save_booking_record`. Refund description reflects actual Stripe outcome (refunded vs pending) |
| C-2 | CRITICAL | `run_api_server.py` | `NameError` in `self_serve_cancel` when a booking was already cancelled before the page loaded. `refund_issued` was defined only inside `if request.method == "POST" and not already_done:` — if that block was skipped (booking already cancelled on GET or duplicate POST), the `if already_done:` rendering block referenced the undefined variable, causing Python `NameError` → HTTP 500. A customer clicking their cancel link a second time got a server error page instead of a gentle "already cancelled" page | Initialized `refund_issued = False` unconditionally before the POST block |
| C-3 | MEDIUM | `run_api_server.py` | `self_serve_cancel` OCTO detection checked `supplier_id in octo_platforms` only, missing the `or platform == "octo"` branch that `DELETE /bookings/{id}` uses. Diverging OCTO detection logic between two paths that do the same operation | Added `or record.get("platform", "") == "octo"` to match the DELETE path |
| C-4 | HIGH | `run_api_server.py` | `self_serve_cancel` always marked the booking `"cancelled"` regardless of whether the Stripe refund succeeded. If Stripe failed, the record was marked cancelled (wrong — customer hasn't been refunded), but the confirmation page and email showed the accurate "our team will process your refund" text. Inconsistency: `DELETE /bookings` correctly uses `"cancellation_refund_failed"` on Stripe failure; self_serve_cancel did not. The mismatch means `cancellation_refund_failed` records set by the self-serve path would never exist — bypassing any future monitoring or retry logic for failed refunds | Added `stripe_ok_self = stripe_result.get("success", True)` check; writes `"cancelled"` on success, `"cancellation_refund_failed"` on failure — same as DELETE path |
| C-8 | MEDIUM | `send_booking_email.py`, `run_api_server.py` | `_build_cancelled_html` always said "the operator has cancelled your booking" in both HTML and plaintext — used for all cancellation scenarios including customer self-serve. A customer who clicked "Confirm Cancellation" received an email implying someone else cancelled on them. In a chargeback dispute where the customer denies authorizing the cancellation, an email saying "the operator cancelled" is not useful evidence | Added `cancelled_by_customer: bool = False` to both `_build_cancelled_html` and `send_booking_email`. Self-serve path passes `cancelled_by_customer=True` → hero text: "We've processed your cancellation for {service}". Bokun webhook and DELETE path use default (False) → "the operator has cancelled your booking" |

### Cancellation design gaps — fixed in Session 13

| # | Gap | Status |
|---|---|---|
| A-1 | No wallet credit-back on any cancellation path | **FIXED** — `credit_wallet()` added to all 3 paths |
| A-15 | `cancellation_refund_failed` records had no retry path | **FIXED** — `reconcile_bookings.py` Job 3 retries Stripe + emails customer on success |

---

## Session 13 Fixes — Architectural Gap Closure

| # | Severity | File(s) | Gap/Bug | Fix |
|---|---|---|---|---|
| A-1 | CRITICAL | `run_api_server.py` | No wallet credit-back on cancellation. All 3 paths (`DELETE /bookings`, `self_serve_cancel`, `bokun_webhook`) refunded Stripe but never returned funds to the wallet that was debited for a wallet booking. Wallet customers never received a refund when their booking was cancelled | Added `credit_wallet()` call after Stripe refund in all 3 cancellation paths. Non-fatal: logs failure but doesn't block the rest of the cancel flow |
| A-2 | HIGH | `run_api_server.py` | `_make_receipt()` (used by `/execute/guaranteed`) stored only 8 fields — omitted `customer_name`, `customer_phone`, `wallet_id`, `payment_intent_id`, `slot_id`, `supplier_id`, `supplier_reference`, `start_time`, `location_city`, `business_name`. Cancellation paths couldn't access wallet/customer/service data from these records | Expanded `_make_receipt()` to 20 fields; added `customer`, `payment`, `slot` kwargs; call site in `/execute/guaranteed` passes the full context |
| A-3 | HIGH | `execution_engine.py` | When `_cancel_octo()` failed after payment failure (rollback path), the failure was only logged. No retry queued. Orphaned OCTO booking at supplier with no payment | Added `_queue_failed_octo_cancel()` — when `_cancel_octo()` returns False, writes entry to Supabase Storage `cancellation_queue/` for pickup by retry scheduler |
| A-4 | HIGH | `execution_engine.py` | `_cancel_octo()` in `execution_engine` sent `Octo-Capabilities: octo/pricing` header on DELETE — same D-2 bug; previously fixed in `run_api_server.py` and `retry_cancellations.py` but missed here | Removed the header |
| A-5 | HIGH | `intent_sessions.py` | Intent sessions stored only in `.tmp/intent_sessions.json` — wiped on every Railway redeploy. Active agent intents lost on every deploy | Migrated to Supabase Storage (`bookings/intent_sessions.json`); `.tmp/` kept as local fallback |
| A-6 | CRITICAL | `reconcile_bookings.py` | `reconciliation_required` records were flagged but never acted on. A booking silently cancelled by the supplier (no webhook, no OCTO DELETE) would sit in this state forever — customer never refunded | Added Job 2: two-cycle guard (waits ≥35 min before acting), then issues Stripe refund + wallet credit-back + cancellation email. Status updated to `"cancelled"` or `"cancellation_refund_failed"` |
| A-9 | MEDIUM | `run_api_server.py` | Slot discovery only ran when triggered manually or by external cron. Railway deployment had no automatic re-fetch — inventory would go stale indefinitely | Added `_run_slot_discovery()` job to APScheduler: runs `fetch_octo_slots.py` + `aggregate_slots.py` every 4 hours. First run 10 min after startup |
| A-10 | MEDIUM | `run_api_server.py` | No way to test the full booking pipeline without executing a real booking | Added `POST /api/test/book-dry-run`: runs 8 checks (slot lookup, pricing, wallet balance, booking URL parse, OCTO config, OCTO connectivity, Stripe, email config) with no charges or supplier calls. Returns per-check pass/fail with error detail |
| A-13 | MEDIUM | `market_insights.py` | Market snapshot saved only to `.tmp/insights/market_snapshot.json` — wiped on redeploy; no cross-instance sharing | `build_market_overview()` now also writes to Supabase Storage `bookings/market_snapshot.json`. `get_market_snapshot()` reads Supabase first, falls back to local file |
| A-14 | MEDIUM | `send_sms_alert.py` | SMS subscribers and send log stored in `.tmp/` — wiped on redeploy; subscribers lost on every Railway deploy | `load_subscribers()` / `save_subscribers()` and `load_sent_log()` / `save_sent_log()` now use Supabase Storage as primary, `.tmp/` as fallback |
| A-15 | CRITICAL | `reconcile_bookings.py` | `cancellation_refund_failed` records had no automatic retry. A failed Stripe refund sat in this terminal state forever — customer permanently unrefunded with no escalation | Added Job 3: scans `cancellation_refund_failed` records, retries `_refund_stripe_once()`, marks `"cancelled"` + emails customer on success; increments `refund_retry_count` on failure for monitoring |

---

## Session 14 Fixes — Audit Follow-Up

| # | Severity | File(s) | Bug | Fix |
|---|---|---|---|---|
| B-1 | CRITICAL | `execution_engine.py` | `_compute_confidence()` referenced `slot_count` — a variable that is never defined in this scope. Caused a `NameError` on every confidence calculation, crashing `execute()`, the intent monitor, and `/execute/guaranteed` completely | Fixed: renamed to `n` (the existing variable `n = len(matching)` in scope) |
| B-2 | HIGH | `run_api_server.py` | Wallet credit-back in `cancel_booking` (DELETE path) and `self_serve_cancel` (POST path) ran unconditionally — before `stripe_ok` was checked. If Stripe fails, the booking status is `cancellation_refund_failed` and Job 3 in `reconcile_bookings` retries; on success Job 3 also calls `credit_wallet`. Result: wallet credited twice | Fixed: `stripe_ok` computed before wallet credit-back block; credit-back now gated on `if stripe_ok` |
| B-3 | HIGH | `reconcile_bookings.py` | In `_act_on_reconciliation_required()`, `_wallet_credit_back()` was called unconditionally even when Stripe refund failed. For a mixed scenario this would credit the wallet while the Stripe refund was still pending, risking double-credit when Job 3 retried | Fixed: `_wallet_credit_back()` now gated on `if stripe_result.get("success")` |
| B-4 | HIGH | `intent_sessions.py` | `execute_intent()` on success updated the intent session status to `"completed"` but wrote no booking record to Supabase Storage. Intent-booked slots were not cancellable via `DELETE /bookings/{id}`, self-serve cancel, or reconciliation | Fixed: on success, writes a full booking record to `bookings/{bk_id}.json` in Supabase Storage and `.tmp/bookings/` local fallback |
| B-5 | MEDIUM | `run_api_server.py` | `book_with_saved_card()` booking record omitted `customer_name`, `customer_phone`, `business_name`, `location_city`, `start_time`, `currency`, `payment_method`. Cancellation paths couldn't display service/customer data or identify wallet vs Stripe for this booking path | Fixed: added all 7 missing fields to the booking record |
| B-6 | HIGH | `run_api_server.py` | `_find_booking_by_confirmation()` performed a full linear scan of the entire Supabase Storage bookings bucket — O(n) with pagination. At volume this degrades to seconds per supplier cancel webhook | Fixed: `_save_booking_record()` now writes a `by_confirmation/{code}.json` index on every booking save; lookup tries O(1) index first, falls back to scan for pre-index records. `execute_intent()` writes the same index |
| B-7 | MEDIUM | `run_api_server.py` | No startup signal if Supabase Storage was misconfigured — server started silently using `.tmp/` fallback, all state wiped on next deploy with no warning | Fixed: `_check_supabase_on_startup()` writes a test sentinel on startup and logs clearly if unreachable; `/health` now reports `supabase_storage`, `last_slot_discovery`, `inventory_slot_count`, `scheduler_running` |
| B-8 | MEDIUM | `intent_sessions.py` | `_fire_callback()` was fire-and-forget — one network failure silently dropped the event permanently; agent never knew its booking completed | Fixed: on failure, callback queued to Supabase `callback_queue/`; APScheduler retries every 2 min with backoff (2→10→30→120 min from fired_at), 4 max retries, 6h TTL |
| B-9 | CRITICAL | `run_api_server.py` | `_run_slot_discovery()` APScheduler job only ran steps 1 (fetch) and 2 (aggregate). Steps 3 (compute_pricing) and 4 (sync_to_supabase) were never called — meaning the Supabase DB that `/slots` reads from was never updated by the automated pipeline. Agents saw stale or empty inventory unless the manual refresh endpoint was triggered | Fixed: `_run_slot_discovery()` now runs all 4 steps: fetch → aggregate → compute_pricing → sync_to_supabase |
| B-10 | LOW | `run_mcp_remote.py` | `_safe_slot()` missing `location_state` and `spots_total` fields; `get_supplier_info()` returned hardcoded stale slot count; `book_slot` docstring omitted `quantity` parameter | Fixed: added missing fields, live slot count from `/health`, quantity documented |
| B-11 | HIGH | `run_mcp_remote.py` | `search_slots` had 86.3% Smithery uptime — Railway cold starts (10-30s) exceeded the 3s connect timeout causing failures on ~1 in 7 calls. No stale cache fallback meant every cold-start failed hard | Fixed: connect timeout 3s→8s, retries 2→3 with 1.5s backoff, cache TTL 60s→300s, stale cache served for up to 30 min on API error instead of returning error |

---

## Session 15 Fixes — Smithery Reconnection + End-to-End Audit

| # | Severity | File(s) | Bug | Fix |
|---|---|---|---|---|
| B-12 | CRITICAL | `smithery.yaml` | `startCommand` had `type: http` but no `command` — Smithery's `run.tools` infrastructure had no way to start `run_mcp_remote.py`, causing the connection to drop entirely | Fixed: added `command: python`, `args: ["tools/run_mcp_remote.py"]` |
| B-13 | HIGH | `smithery.yaml` | `/api/book` and `/bookings/<id>` both require `X-API-Key` — Smithery-hosted `run_mcp_remote.py` had no way to pass it (env var unset on `run.tools`), making booking and status-check silently fail with 401 | Fixed: added `env` mapping in `smithery.yaml` configSchema; users configure `lmd_api_key` once when installing from Smithery |
| B-14 | MEDIUM | `smithery.yaml` + `run_mcp_remote.py` | Smithery configSchema set `BOOKING_API_URL` from optional config field — if user left it blank, Smithery would inject empty string, overriding the Railway default and breaking all API calls | Fixed: `run_mcp_remote.py` uses `(os.getenv("BOOKING_API_URL") or "https://web-production-dc74b.up.railway.app")` — empty string falls back to default |
| B-15 | HIGH | `run_mcp_remote.py` | `get_supplier_info()` hardcoded 7-supplier list; 7 of 14 active suppliers invisible to agents calling the Smithery-hosted path (Boka Bliss, EgyExcursions, Íshestar, Marvel Egypt, REDRIB, TourTransfer, Vakare missing) | Fixed: all 14 suppliers added to static list |
| B-16 | MEDIUM | `run_mcp_remote.py` | `get_supplier_info()` had no caching — fetched `/health` on every call; under burst agent load this was a live network round-trip per invocation | Fixed: 1-hour cache (`_SUPPLIER_INFO_CACHE`) |
| B-17 | LOW | `run_mcp_remote.py` | `_SLOTS_CACHE` was unbounded — unique (city, category, hours_ahead, max_price) combinations accumulated without eviction | Fixed: max 100 entries; oldest (by expiry) evicted when at capacity |

---

## Session 16 Fixes — Booking Flow Hardening

| # | Severity | File(s) | Bug | Fix |
|---|---|---|---|---|
| B-18 | CRITICAL | `run_api_server.py` | `_get_live_supplier_directory()` fetched Supabase with `limit=10000` but PostgREST caps every response at 1000 rows regardless. Slots are alphabetically ordered: first 1000 covered only 5 suppliers completely + 64 EgyExcursions rows. 8 suppliers (EgyExcursions, Hillborn, Íshestar, Marvel Egypt, O Turista, Pure Morocco, Ramen Factory, REDRIB, TourTransfer, Vakare) were either absent or partially represented in `get_supplier_info` live lookups | Fixed: paginated loop using `limit=1000 + offset` — breaks when a page has fewer rows than PAGE_SIZE; ensures all 14 suppliers are returned |
| B-19 | CRITICAL | `run_api_server.py` | `create_checkout()` did not store `booking_url`, `platform`, or `currency` in the booking record — only in Stripe session metadata. Stripe metadata values are capped at 500 characters and silently truncate. A `booking_url` JSON blob for a 5-option OCTO product exceeds this limit; the webhook reading the truncated blob calls `json.loads()` on invalid JSON, crashes, cancels the payment hold, and permanently fails the booking. Customer sees checkout_expired with no retry path | Fixed: `booking_url`, `platform`, `currency` now stored in the Supabase Storage booking record at checkout creation. Webhook reads from record first, falls back to Stripe metadata for sessions created before this deploy |
| B-20 | HIGH | `run_api_server.py` | Stripe webhook updated `wh_record_key` to `"processing"` but left the customer-facing booking record at `"pending_payment"` before spawning the fulfillment thread. Agents polling `GET /bookings/<id>` for up to 45 seconds after payment saw `pending_payment` — indistinguishable from an unpaid booking. No way to tell if a customer had paid but fulfillment was in flight | Fixed: before spawning the fulfillment thread, `_save_booking_record(pending_booking_id, {..., "status": "fulfilling", "payment_status": "paid"})` so agents immediately see the payment has landed |
| B-21 | MEDIUM | `run_api_server.py` | `GET /bookings/<id>` response omitted `checkout_url` and `payment_status`. If an agent lost the `checkout_url` from the original `/api/book` response (e.g. tool call result dropped, context cleared), it had no way to recover it by polling — the customer couldn't be sent back to complete payment | Fixed: `checkout_url` and `payment_status` added to `get_booking` response dict |

---

## Session 17 Fixes — Smithery Uptime + run_mcp_remote Correctness

| # | Severity | File(s) | Bug | Fix |
|---|---|---|---|---|
| R-1 | CRITICAL | `run_mcp_remote.py` | `search_slots` had 75.7% uptime (24.3% failure rate). Railway free tier sleeps containers after 15 min idle; cold starts take 10-30s, exhausting the 3×8s retry window. With no stale cache on first call, the tool returned `[{"error": "..."}]` counted as failure by Smithery | Added `_keep_railway_warm()` async background task: pings `/health` every 10 min to prevent container sleep. Started via `asyncio.create_task()` on the first `search_slots` invocation |
| R-2 | HIGH | `run_mcp_remote.py` | `hours_ahead=72.0` (Python float default) sent as `"72.0"` string to Railway's `/slots`. Flask's `type=int` conversion silently falls back to the default 168h when given a float string, ignoring the agent's requested time window | Fixed: `int(hours_ahead)` before building the params dict |
| R-3 | MEDIUM | `run_mcp_remote.py`, `smithery.yaml` | `get_booking_status` docstring and smithery.yaml description listed wrong status values (`pending`, `confirmed`) — real values are `pending_payment`, `fulfilling`, `booked`, `failed`, `cancelled`. Agents using the docstring to interpret status couldn't distinguish unpaid from confirmed | Updated docstrings and smithery.yaml to list all correct status values; added note about `checkout_url` and `payment_status` fields |

---

---

## Session 18 Fixes — Booking Conversion: Zero Payments Root Cause

**Root cause investigation**: Stripe API confirmed zero `checkout.session.completed` events — no customer has ever completed payment. All 21+ booking records stuck at `pending_payment` are explained by: (a) no email sent to customer at checkout creation, so if the AI agent fails to surface the URL the customer never sees it; (b) `our_price` not saved to pending record, so agents polling `get_booking_status` see `price_charged: null` and may retry, creating duplicate sessions.

| # | Severity | File(s) | Bug | Fix |
|---|---|---|---|---|
| B-22 | HIGH | `run_api_server.py` | `create_checkout()` did not save `our_price` or `price_charged` to the pending booking record. Agents polling `GET /bookings/<id>` after calling `book_slot` saw `price_charged: null, price_per_person: null` — indistinguishable from a broken/failed booking, triggering retries that created duplicate Stripe sessions for the same slot | Fixed: `our_price` and `price_charged` (our_price × quantity) now saved in `_save_booking_record` at checkout creation. Added `price_per_person` to `GET /bookings/<id>` response |
| B-23 | HIGH | `run_api_server.py` | `book_slot` response returned only `{ success, checkout_url, booking_id, status, expires_at }` — no price, service name, start time, or quantity. Agents had no context to verify the created booking without a follow-up `get_booking_status` call, which itself returned null prices (B-22). Missing `action_required` instruction meant agents could drop the checkout_url without surfacing it | Fixed: added `service_name`, `start_time`, `location_city`, `quantity`, `price_per_person`, `total_price`, `currency`, `action_required` to the success response |
| B-24 | CRITICAL | `run_api_server.py`, `send_booking_email.py` | No email sent to customer when `book_slot` is called. If an AI agent fails to surface the `checkout_url` to the human (tool result dropped, context cleared, agent doesn't relay it), the customer never sees the payment link. All 21 booking sessions expired without any customer being notified. This is the primary reason zero bookings have converted | Fixed: added `checkout_created` email type to `send_booking_email.py` — branded HTML email with prominent "Complete Booking →" CTA linking to `checkout_url`, booking summary, 24h expiry warning. Sent immediately after Stripe session creation in `create_checkout()`; non-fatal (email failure never blocks the booking) |

---

## Totals

| Session | Bugs Fixed |
|---|---|
| Sessions 1–3 (batch commits) | 41 |
| Session 4 (simplify) | 5 |
| Session 5 (deep Bokun audit) | 8 |
| Session 6 (slot inventory audit) | 2 |
| Session 7 (vendor coverage audit) | 2 |
| Session 8 (structural resolution fix) | 1 |
| Session 9 (MCP agent visibility) | 6 |
| Session 10 (booking flow audit) | 7 |
| Session 11 (post-booking email audit) | 3 |
| Session 12 (cancellation audit) | 4 |
| Session 13 (architectural gap closure) | 11 |
| Session 14 (audit follow-up + reliability + agent visibility + Smithery uptime) | 12 |
| Session 15 (Smithery reconnection + end-to-end audit) | 6 |
| Session 16 (booking flow hardening) | 4 |
| Session 17 (Smithery uptime + run_mcp_remote correctness) | 3 |
| Session 18 (booking conversion — zero payments root cause) | 3 |
| **Total** | **118** |
