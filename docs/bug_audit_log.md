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

## Totals

| Session | Bugs Fixed |
|---|---|
| Sessions 1–3 (batch commits) | 41 |
| Session 4 (simplify) | 5 |
| Session 5 (deep Bokun audit) | 8 |
| Session 6 (slot inventory audit) | 2 |
| **Total** | **56** |
